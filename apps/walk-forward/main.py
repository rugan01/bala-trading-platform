"""
Walk-forward live paper runner and profile orchestrator.
"""

import argparse
import logging
import signal
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pytz

from config import Config
from interfaces import PositionManager
from models import DayContext, InstrumentRef, RunnerProfile
from runner_profiles import registered_profile_ids, resolve_profiles
from runtime import build_runtime

IST = pytz.timezone("Asia/Kolkata")

logger = logging.getLogger(__name__)


class WalkForwardValidator:
    """
    Main orchestrator. Lifecycle per trading day:
      1. Initialize: fetch prev-day OHLC, compute CPR, warm up SuperTrend
      2. Monitor: poll 15m candles every minute, process at candle boundaries
      3. Signal: detect 2nd-touch on TC/BC, enter paper trade
      4. Manage: track SL, T1, trail per candle
      5. Close: force-close at 23:00, send day summary
    """

    def __init__(
        self,
        profile: RunnerProfile,
        dry_run: bool = False,
        test_duration_minutes: float | None = None,
        ignore_session_window: bool = False,
    ):
        self.profile = profile
        self.dry_run = dry_run
        self.test_duration_minutes = test_duration_minutes
        self.ignore_session_window = ignore_session_window

        self.runtime = build_runtime(
            profile=self.profile,
            dry_run=dry_run,
            include_all_intraday_candles=ignore_session_window,
        )
        
        self.data_provider = self.runtime.data_provider
        self.strategy = self.runtime.strategy
        self.notion = self.runtime.journal_sink
        self.telegram = self.runtime.alert_sink
        self.log_file = Config.LOG_DIR / f"{self.profile.profile_id}_{date.today().isoformat()}.log"
        self.logger = logging.getLogger(f"walk_forward.{self.profile.profile_id}")
        self.position_manager: PositionManager | None = None
        self.instrument: InstrumentRef | None = None
        self.cpr = None

        self._last_processed_ts: datetime | None = None
        self._day_trades: list = []
        self._closed_trade_seen_count: int = 0
        self._running = True

    # ──────────────────────────────────────────────────────────────────────────
    # Initialization
    # ──────────────────────────────────────────────────────────────────────────

    def initialize_day(self):
        """Run before session opens (around 16:50 IST)."""
        self.logger.info("=" * 60)
        self.logger.info(f"Walk-Forward Validator starting — {date.today()} [{'DRY RUN' if self.dry_run else 'PAPER'}]")
        if self.test_duration_minutes:
            self.logger.info(
                "Bounded test mode enabled — duration=%s minutes | ignore_session_window=%s",
                self.test_duration_minutes,
                self.ignore_session_window,
            )
        self.logger.info("=" * 60)

        if not self.dry_run:
            try:
                Config.validate()
            except EnvironmentError as e:
                self.logger.error(str(e))
                sys.exit(1)

        # Step 1: Find current SILVERMIC instrument
        self.logger.info("Step 1/3: Finding SILVERMIC instrument key...")
        self.instrument = self.data_provider.resolve_instrument()

        # Step 2: Fetch previous day's OHLC and compute CPR
        self.logger.info("Step 2/3: Fetching previous day OHLC for CPR levels...")
        prev_ohlc = self.data_provider.get_prev_day_ohlc(self.instrument)
        day_context = DayContext(
            instrument=self.instrument,
            prev_day_ohlc=prev_ohlc,
            session_date=date.today(),
            metadata={"strategy_id": self.strategy.strategy_id},
        )

        # Step 3: Load warm-up candles and initialize indicators
        self.logger.info("Step 3/3: Loading warm-up candles for SuperTrend initialization...")
        warmup = self.data_provider.get_warmup_candles(
            self.instrument,
            n=Config.ST_WARMUP_BARS,
        )

        # Initialize selected strategy (SuperTrend warm-up happens inside for V3)
        self.strategy.initialize(day_context, warmup)
        self.cpr = self.strategy.get_state_snapshot().get("cpr")
        if self.cpr is None:
            raise RuntimeError("Selected strategy did not expose CPR day-start context")

        # Initialize trade manager
        self.position_manager = self.runtime.position_manager_factory(
            self.instrument.trading_symbol
        )
        self._day_trades = []
        self._closed_trade_seen_count = 0

        # Send day-start Telegram alert
        self.telegram.send_day_start(self.cpr)

        self.logger.info(f"Initialization complete. Monitoring: {self.instrument.trading_symbol}")
        self.logger.info(f"Session: 17:00–23:00 IST | CPR: {self.cpr.summary()}")

    # ──────────────────────────────────────────────────────────────────────────
    # Main loop
    # ──────────────────────────────────────────────────────────────────────────

    def run(self):
        """Main polling loop. Runs until 23:30 IST or SIGTERM."""
        self.initialize_day()

        self.logger.info("Entering monitoring loop...")
        test_end = None
        if self.test_duration_minutes:
            test_end = datetime.now(IST) + timedelta(minutes=self.test_duration_minutes)
            self.logger.info("Bounded test will auto-stop at %s", test_end.strftime("%H:%M:%S IST"))

        while self._running:
            now = datetime.now(IST)

            if test_end and now >= test_end:
                self.logger.info("Bounded test duration reached — shutting down")
                break

            # Exit if past 23:30
            if now.hour == 23 and now.minute >= 30:
                self.logger.info("23:30 IST reached — shutting down")
                break

            # Only process during session window
            if self.ignore_session_window or self._is_session_active(now):
                self._tick(now)
            else:
                if now.hour < 17:
                    self.logger.debug(f"Session not started yet ({now.strftime('%H:%M')}). Waiting...")

            time.sleep(30)  # Poll every 30 seconds

        self._end_of_day()

    def _tick(self, now: datetime):
        """Called every 30s during session. Processes candles at 15m boundaries."""
        if self.instrument is None or self.position_manager is None:
            raise RuntimeError("Walk-forward runtime is not initialized")

        # Fetch latest intraday candles
        candles = self.data_provider.get_intraday_candles(self.instrument)
        if not candles or len(candles) < 2:
            return

        # The last candle in the list is the currently-forming one — skip it.
        # Process the second-to-last (the latest CLOSED candle).
        closed_candles = candles[:-1]
        latest = closed_candles[-1]
        ts = latest.timestamp

        # Skip if already processed
        if self._last_processed_ts and ts <= self._last_processed_ts:
            return

        self._last_processed_ts = ts
        self.logger.info(
            f"Processing candle @ {ts.strftime('%H:%M')} | "
            f"O={latest.open} H={latest.high} L={latest.low} C={latest.close}"
        )

        # Force-close check (at or after 23:00)
        if now.hour >= Config.FORCE_CLOSE_H and now.minute >= Config.FORCE_CLOSE_M:
            if self.position_manager.has_open_position():
                self.position_manager.force_close_all(latest.close, ts)
                self._capture_closed_trade()
            return

        # Process candle through signal detector
        signal = self.strategy.on_candle(latest)

        # Update any open trade first
        if self.position_manager.has_open_position():
            self.position_manager.update(latest, self.strategy.get_state_snapshot())

            # Capture if trade just closed
            if not self.position_manager.has_open_position():
                self._capture_closed_trade()

        # Open new trade on signal (if no active trade and daily limit not hit)
        elif signal:
            if self.position_manager.can_enter():
                self.position_manager.enter(signal, ts)
            else:
                self.logger.info("[Main] Signal detected but daily trade limit reached — skipping")

    def _capture_closed_trade(self):
        """Record a just-closed trade in day's list."""
        if not self.position_manager:
            return

        new_trades, self._closed_trade_seen_count = self.position_manager.pop_new_closed_trades(
            self._closed_trade_seen_count
        )
        if not new_trades:
            return

        self._day_trades.extend(new_trades)
        for trade in new_trades:
            net = trade.net_pnl() or 0.0
            self.logger.info(
                f"[Main] Captured closed trade #{trade.trade_num} for summary | "
                f"{trade.direction} {trade.trading_symbol} | Net P&L=₹{net:.0f}"
            )

    # ──────────────────────────────────────────────────────────────────────────
    # End of day
    # ──────────────────────────────────────────────────────────────────────────

    def _end_of_day(self):
        """Force-close any remaining open trade and send day summary."""
        now = datetime.now(IST)

        if self.position_manager and self.position_manager.has_open_position():
            self.logger.info("[EOD] Open trade found — force closing")
            # Use last known LTP, fall back to fetching candle
            quote = self.data_provider.get_latest_quote(self.instrument) if self.instrument else None
            close_price = quote.ltp if quote else 0.0
            self.position_manager.force_close_all(close_price, now)
            self._capture_closed_trade()

        # Day summary Telegram
        date_str = date.today().strftime("%d %b %Y")
        self.telegram.send_day_summary(self._day_trades, date_str)

        self.logger.info(f"Walk-Forward Validator session complete — {date_str}")
        self.logger.info(f"Log saved: {self.log_file}")

    # ──────────────────────────────────────────────────────────────────────────
    # Helpers
    # ──────────────────────────────────────────────────────────────────────────

    def _is_session_active(self, now: datetime) -> bool:
        session_start = now.replace(
            hour=Config.SESSION_START_H, minute=Config.SESSION_START_M, second=0, microsecond=0
        )
        session_end = now.replace(
            hour=Config.SESSION_END_H, minute=Config.SESSION_END_M, second=0, microsecond=0
        )
        return session_start <= now <= session_end

    def _shutdown(self, signum, frame):
        self.logger.info(f"Received signal {signum} — initiating graceful shutdown...")
        self._running = False

    def request_shutdown(self):
        self._running = False


def configure_process_logging(profile_id: str) -> Path:
    Config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = Config.LOG_DIR / f"{profile_id}_{date.today().isoformat()}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_file),
        ],
        force=True,
    )
    return log_file


def run_single_profile(args: argparse.Namespace, profile: RunnerProfile) -> int:
    configure_process_logging(profile.profile_id)
    validator = WalkForwardValidator(
        profile=profile,
        dry_run=args.dry_run,
        test_duration_minutes=args.test_duration_minutes,
        ignore_session_window=args.ignore_session_window,
    )
    signal.signal(signal.SIGINT, validator._shutdown)
    signal.signal(signal.SIGTERM, validator._shutdown)
    validator.run()
    return 0


def build_worker_command(args: argparse.Namespace, profile_id: str) -> list[str]:
    cmd = [sys.executable, str(Path(__file__).resolve()), "--worker", "--profile-id", profile_id]
    if args.dry_run:
        cmd.append("--dry-run")
    if args.test_duration_minutes is not None:
        cmd.extend(["--test-duration-minutes", str(args.test_duration_minutes)])
    if args.ignore_session_window:
        cmd.append("--ignore-session-window")
    if args.strategy_id:
        cmd.extend(["--strategy-id", args.strategy_id])
    if args.position_plan_id:
        cmd.extend(["--position-plan-id", args.position_plan_id])
    return cmd


def run_multiple_profiles(args: argparse.Namespace, profiles: list[RunnerProfile]) -> int:
    children: list[subprocess.Popen] = []

    def shutdown_children(signum, frame):
        logger.info("Received signal %s — stopping child runners", signum)
        for child in children:
            if child.poll() is None:
                child.terminate()

    signal.signal(signal.SIGINT, shutdown_children)
    signal.signal(signal.SIGTERM, shutdown_children)

    for profile in profiles:
        cmd = build_worker_command(args, profile.profile_id)
        logger.info("Launching profile %s", profile.profile_id)
        children.append(subprocess.Popen(cmd))

    exit_code = 0
    for child in children:
        code = child.wait()
        if code != 0:
            exit_code = code
    return exit_code


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Walk-forward live paper runner"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run without writing to Notion or sending Telegram messages",
    )
    parser.add_argument(
        "--profile-id",
        action="append",
        default=None,
        help="Runner profile id. Repeat to launch multiple profiles. Defaults to WFV_PROFILE_ID or silvermic_v3_default.",
    )
    parser.add_argument(
        "--list-profiles",
        action="store_true",
        help="Print registered runner profiles and exit.",
    )
    parser.add_argument(
        "--strategy-id",
        default=None,
        help="Optional override for the selected profile strategy id.",
    )
    parser.add_argument(
        "--position-plan-id",
        default=None,
        help="Optional override for the selected profile position plan id.",
    )
    parser.add_argument(
        "--test-duration-minutes",
        type=float,
        default=None,
        help="Run for a bounded number of minutes, then exit automatically.",
    )
    parser.add_argument(
        "--ignore-session-window",
        action="store_true",
        help="Test mode only: process today's live candles even outside configured strategy hours.",
    )
    parser.add_argument(
        "--worker",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args()

    if args.list_profiles:
        for profile_id in registered_profile_ids():
            print(profile_id)
        return

    selected_profile_ids = args.profile_id or [Config.WFV_PROFILE_ID]
    profiles = resolve_profiles(
        profile_ids=selected_profile_ids,
        strategy_id=args.strategy_id,
        position_plan_id=args.position_plan_id,
    )

    if args.worker or len(profiles) == 1:
        run_single_profile(args, profiles[0])
        return

    configure_process_logging("multi_runner_parent")
    raise SystemExit(run_multiple_profiles(args, profiles))


if __name__ == "__main__":
    main()
