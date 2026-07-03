import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

RISK_APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RISK_APP))

from mtm_guard import (  # noqa: E402
    PositionSnapshot,
    parse_command,
    pending_confirmation_expired,
    positions_net_pnl,
    user_and_chat_allowed,
)


class MTMGuardControlTests(unittest.TestCase):
    def test_control_requires_allowed_user(self):
        self.assertFalse(
            user_and_chat_allowed(
                chat_id=10,
                user_id=20,
                allowed_user_ids=set(),
                allowed_chat_ids={10},
            )
        )
        self.assertTrue(
            user_and_chat_allowed(
                chat_id=10,
                user_id=20,
                allowed_user_ids={20},
                allowed_chat_ids={10},
            )
        )

    def test_set_loss_normalizes_to_positive_limit(self):
        action, payload = parse_command("/set BALA loss -3000", "NIMMY")
        self.assertEqual(action, "set_loss")
        self.assertEqual(payload, {"account": "BALA", "loss_limit": 3000.0})

    def test_close_defaults_to_service_account(self):
        action, payload = parse_command("/close", "NIMMY")
        self.assertEqual(action, "close")
        self.assertEqual(payload, {"account": "NIMMY"})

    def test_confirmation_expires_after_five_minutes(self):
        now = datetime.now().astimezone()
        fresh = {"created_at": (now - timedelta(seconds=299)).isoformat()}
        stale = {"created_at": (now - timedelta(seconds=301)).isoformat()}
        self.assertFalse(pending_confirmation_expired(fresh, current=now))
        self.assertTrue(pending_confirmation_expired(stale, current=now))
        self.assertTrue(pending_confirmation_expired({}, current=now))

    def test_expiry_stop_uses_only_matching_position_pnl(self):
        positions = [
            PositionSnapshot("NIFTY26JUL25000CE", "NFO", "I", -75, 100, -2200, -2200, 0, "a"),
            PositionSnapshot("NIFTY26JUL25000PE", "NFO", "I", -75, 90, 500, 500, 0, "b"),
        ]
        self.assertEqual(positions_net_pnl(positions), -1700)


if __name__ == "__main__":
    unittest.main()
