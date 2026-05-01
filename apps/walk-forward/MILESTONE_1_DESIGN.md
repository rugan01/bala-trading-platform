# Historical Milestone 1 Design Note

This file is preserved for design history from the earlier local workspace.
Use the current README and runbook for practical commands.

# Milestone 1 Design Note

This note defines the **first safe refactor step** toward a strategy-agnostic and provider-agnostic walk-forward engine.

The goal of Milestone 1 is **not** to redesign behavior.

The goal is:
- introduce stable interface boundaries
- preserve the current `SILVERMIC V3` paper-trading behavior
- make future strategies/providers easier to plug in

This milestone should be treated as:
- **extraction**
- not **reinvention**

---

## Milestone 1 Goal

Introduce a thin abstraction layer around the current concrete components:

- `UpstoxFeed`
- `SignalDetector`
- `TradeManager`
- `NotionLogger`
- `TelegramAlerter`

without changing:
- strategy rules
- position management behavior
- Notion field mapping
- Telegram message behavior
- run command

After Milestone 1, this should still work the same way:

```bash
cd /path/to/bala-trading-platform/apps/walk-forward
source ../.venv/bin/activate
python main.py
```

---

## Guiding Rule

The first refactor should make the current system easier to reason about, not more clever.

So:
- keep the current working files
- add base interfaces alongside them
- adapt the current concrete classes to those interfaces
- only switch orchestration once parity is obvious

---

## Minimal Core Models

These are the smallest normalized models worth introducing first.

### `InstrumentRef`

Purpose:
- normalized identifier for the instrument being traded

Suggested shape:

```python
@dataclass
class InstrumentRef:
    instrument_key: str
    trading_symbol: str
    expiry: str
    segment: str
    underlying: str
```

### `Candle`

Purpose:
- normalized candle object for strategy and position management

Suggested shape:

```python
@dataclass
class Candle:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    oi: float = 0.0
```

### `Quote`

Purpose:
- normalized latest quote / LTP object

Suggested shape:

```python
@dataclass
class Quote:
    instrument_key: str
    ltp: float
    timestamp: datetime | None = None
```

### `DayContext`

Purpose:
- strategy initialization context for the day/session

Suggested shape:

```python
@dataclass
class DayContext:
    instrument: InstrumentRef
    prev_day_ohlc: dict
    session_date: date
    metadata: dict[str, Any] = field(default_factory=dict)
```

For the current strategy, `metadata` will hold CPR-related context once computed.

---

## Base Interfaces

These are the first interfaces to introduce.

They should be small and practical.

## 1. `MarketDataProvider`

Responsibility:
- instrument resolution
- historical candles
- latest quote/LTP

Suggested interface:

```python
class MarketDataProvider(Protocol):
    def resolve_instrument(self) -> InstrumentRef:
        ...

    def get_prev_day_ohlc(self, instrument: InstrumentRef) -> dict:
        ...

    def get_warmup_candles(self, instrument: InstrumentRef, n: int) -> list[Candle]:
        ...

    def get_intraday_candles(self, instrument: InstrumentRef) -> list[Candle]:
        ...

    def get_latest_quote(self, instrument: InstrumentRef) -> Quote | None:
        ...
```

Current adapter:
- `UpstoxFeed` can be wrapped as `UpstoxMarketDataProvider`

Important note:
- do **not** rewrite the feed internals first
- wrap current behavior first

## 2. `Strategy`

Responsibility:
- initialize session context
- process closed candles
- emit signals

Suggested interface:

```python
class Strategy(Protocol):
    def initialize(self, day_context: DayContext, warmup_candles: list[Candle]) -> None:
        ...

    def on_candle(self, candle: Candle) -> Signal | None:
        ...

    def get_state_snapshot(self) -> dict[str, Any]:
        ...
```

Current implementation:
- `SignalDetector` becomes the first strategy adapter:
  - `SilvermicCprBandV3Strategy`

Why `get_state_snapshot()`:
- useful for debugging and later journaling/telemetry
- not required for trade execution

## 3. `PositionManager`

Responsibility:
- enter and manage paper trades from signals
- decide partial exits, trailing, EOD close

Suggested interface:

```python
class PositionManager(Protocol):
    def can_enter(self) -> bool:
        ...

    def enter(self, signal: Signal, timestamp: datetime) -> None:
        ...

    def update(self, candle: Candle, strategy_state: dict[str, Any] | None = None) -> None:
        ...

    def force_close_all(self, price: float, timestamp: datetime) -> None:
        ...

    def has_open_position(self) -> bool:
        ...

    def pop_new_closed_trades(self, seen_count: int) -> tuple[list[Any], int]:
        ...
```

Current implementation:
- `TradeManager` becomes the first concrete `PositionManager`

Note:
- strategy state is optional because some position managers may need:
  - trail values
  - volatility regime
  - dynamic levels

The current engine uses:
- `SuperTrend(5,1.5)` trail information from the detector

## 4. `JournalSink`

Responsibility:
- create and update trade journal entries

Suggested interface:

```python
class JournalSink(Protocol):
    def create_entry(self, trade) -> str | None:
        ...

    def update_exit(self, trade) -> bool:
        ...
```

Current implementation:
- `NotionLogger`

## 5. `AlertSink`

Responsibility:
- send day start, signal, T1, close, and day summary messages

Suggested interface:

```python
class AlertSink(Protocol):
    def send_day_start(self, context: Any) -> None:
        ...

    def send_signal(self, trade) -> None:
        ...

    def send_t1_hit(self, trade, pnl: float) -> None:
        ...

    def send_trade_closed(self, trade) -> None:
        ...

    def send_day_summary(self, trades: list, date_str: str) -> None:
        ...

    def send_error(self, message: str) -> None:
        ...
```

Current implementation:
- `TelegramAlerter`

---

## First Safe Wiring Change

The first orchestration change should be minimal.

Current:
- `main.py` directly constructs concrete classes

Milestone 1 target:
- `main.py` constructs through a tiny composition layer

Suggested composition function:

```python
def build_runtime(dry_run: bool) -> RuntimeBundle:
    ...
```

Suggested `RuntimeBundle`:

```python
@dataclass
class RuntimeBundle:
    data_provider: MarketDataProvider
    strategy: Strategy
    position_manager: PositionManager
    journal_sink: JournalSink
    alert_sink: AlertSink
```

This is enough to:
- keep `main.py` simple
- make construction swappable later
- avoid a full dependency injection framework

---

## Recommended Extraction Order

### Step 1
- add `models.py` for:
  - `InstrumentRef`
  - `Candle`
  - `Quote`
  - `DayContext`

### Step 2
- add `interfaces.py` for:
  - `MarketDataProvider`
  - `Strategy`
  - `PositionManager`
  - `JournalSink`
  - `AlertSink`

### Step 3
- create adapter wrappers:
  - `upstox_provider.py`
  - `silvermic_v3_strategy.py`
  - `paper_position_manager.py`

Initially those wrappers can delegate almost entirely to existing classes.

### Step 4
- add `runtime.py` with `build_runtime()`

### Step 5
- switch `main.py` to use `build_runtime()`

At this point behavior should still be effectively unchanged.

---

## What Should Not Change In Milestone 1

Do not change:
- `SILVERMIC V3` rules
- signal timing
- SL/T1/trail behavior
- Notion schema
- Telegram message formatting
- log semantics unless necessary
- paper-only safety posture

That discipline is what keeps Milestone 1 safe.

---

## Validation Checklist For Milestone 1

Milestone 1 is complete only if:

1. `python main.py` still runs with the same operator command
2. auto instrument discovery still works
3. day-start Telegram still works
4. synthetic paper smoke test still works
5. Notion create + update still works
6. day summary still includes closed trades

If any of those regress, the extraction is not done yet.

---

## Success Definition

Milestone 1 is successful when:
- the code is **not yet generic**, but the boundaries are now explicit
- future providers and strategies have a place to plug in
- the current SILVERMIC path remains the reference implementation

That is the right foundation for the next step without risking the working engine.
