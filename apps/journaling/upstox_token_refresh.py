#!/usr/bin/env python3
"""
Upstox Access Token Auto-Refresh Script
Uses upstox-totp library for automated TOTP-based OAuth flow.

Supports multiple accounts (BALA, NIMMY).

Usage:
    python upstox_token_refresh.py                    # Refresh BALA (default)
    python upstox_token_refresh.py --account NIMMY   # Refresh NIMMY
    python upstox_token_refresh.py --account ALL     # Refresh all accounts

Requirements:
    Python 3.12+ and:
    pip install upstox-totp python-dotenv

References:
    - https://github.com/batpool/upstox-totp
    - https://upstox-totp.readthedocs.io/
"""

import os
import sys
import argparse
import logging
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv, set_key

# Configure logging
LOG_FILE = os.path.expanduser('~/Library/Logs/upstox_token_refresh.log')
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE)
    ]
)
logger = logging.getLogger(__name__)

SUPPORTED_ACCOUNTS = ['BALA', 'NIMMY']
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ENV_FILE = REPO_ROOT / '.env'


def require_upstox_totp():
    """Import upstox-totp lazily so the CLI can fail with a helpful setup message."""
    if sys.version_info < (3, 12):
        raise RuntimeError(
            "upstox-totp requires Python 3.12+. "
            "Run this script with a 3.12+ interpreter, for example:\n"
            "  ./.venv/bin/python apps/journaling/upstox_token_refresh.py --account ALL\n"
            "or:\n"
            "  python3.13 apps/journaling/upstox_token_refresh.py --account ALL"
        )
    try:
        from upstox_totp import UpstoxTOTP  # type: ignore
    except ModuleNotFoundError as exc:
        raise ModuleNotFoundError(
            "Missing dependency 'upstox-totp'. Install journaling dependencies with:\n"
            "  python3.13 -m pip install -r apps/journaling/requirements.txt\n"
            "or install only the missing package with:\n"
            "  python3.13 -m pip install upstox-totp"
        ) from exc
    return UpstoxTOTP


class TokenRefresher:
    def __init__(self, env_file: str, account: str = 'BALA'):
        self.env_file = env_file
        self.account = account.upper()
        load_dotenv(env_file, override=True)

        # Load account-specific credentials
        prefix = f'UPSTOX_{self.account}_'
        self.api_key = os.getenv(f'{prefix}API_KEY')
        self.api_secret = os.getenv(f'{prefix}API_SECRET')
        self.redirect_uri = os.getenv(f'{prefix}REDIRECT_URI', 'http://localhost:3000/callback')
        self.mobile = os.getenv(f'{prefix}MOBILE')
        self.pin = os.getenv(f'{prefix}PIN')
        self.totp_secret = os.getenv(f'{prefix}TOTP_SECRET')
        self.password = os.getenv(f'{prefix}PASSWORD', '')

        self._validate_credentials()

    def _validate_credentials(self):
        """Validate all required credentials are present."""
        required = {
            f'UPSTOX_{self.account}_API_KEY': self.api_key,
            f'UPSTOX_{self.account}_API_SECRET': self.api_secret,
            f'UPSTOX_{self.account}_MOBILE': self.mobile,
            f'UPSTOX_{self.account}_PIN': self.pin,
            f'UPSTOX_{self.account}_TOTP_SECRET': self.totp_secret,
        }
        missing = [k for k, v in required.items() if not v]
        if missing:
            raise ValueError(f"Missing required environment variables: {', '.join(missing)}")

        # Check for placeholder TOTP secret
        if 'PLACEHOLDER' in (self.totp_secret or ''):
            raise ValueError(f"TOTP secret for {self.account} is not configured. Please provide the TOTP secret.")

        logger.info(f"[{self.account}] All credentials validated successfully")

    def refresh_token(self) -> str:
        """Refresh the access token using upstox-totp library."""
        logger.info(f"[{self.account}] Initiating token refresh via upstox-totp...")
        UpstoxTOTP = require_upstox_totp()

        # Set environment variables that upstox-totp library expects
        os.environ['UPSTOX_CLIENT_ID'] = self.api_key
        os.environ['UPSTOX_CLIENT_SECRET'] = self.api_secret
        os.environ['UPSTOX_REDIRECT_URI'] = self.redirect_uri
        os.environ['UPSTOX_USERNAME'] = self.mobile
        os.environ['UPSTOX_PIN_CODE'] = self.pin
        os.environ['UPSTOX_TOTP_SECRET'] = self.totp_secret
        if self.password:
            os.environ['UPSTOX_PASSWORD'] = self.password

        try:
            # Initialize using environment variables
            upstox = UpstoxTOTP()

            # Get the access token via app_token API
            response = upstox.app_token.get_access_token()

            # Response structure: success=True, data=AccessTokenData(access_token='...')
            if hasattr(response, 'data') and hasattr(response.data, 'access_token'):
                access_token = response.data.access_token
            elif hasattr(response, 'access_token'):
                access_token = response.access_token
            elif isinstance(response, dict):
                access_token = response.get('access_token') or response.get('data', {}).get('access_token')
            else:
                raise Exception(f"Unexpected response format: {response}")

            if not access_token:
                raise Exception(f"No access token in response: {response}")

            logger.info(f"[{self.account}] Successfully obtained new access token")
            return access_token

        except Exception as e:
            logger.error(f"[{self.account}] Token refresh failed: {e}")
            raise

    def update_env_file(self, new_token: str):
        """Update the .env file with new access token."""
        logger.info(f"[{self.account}] Updating {self.env_file} with new token...")

        # Update account-specific token
        set_key(self.env_file, f'UPSTOX_{self.account}_ACCESS_TOKEN', new_token)
        set_key(self.env_file, f'UPSTOX_{self.account}_TOKEN_REFRESHED_AT', datetime.now().isoformat())

        # Also update legacy/default keys for BALA (backward compatibility)
        if self.account == 'BALA':
            set_key(self.env_file, 'UPSTOX_ACCESS_TOKEN', new_token)
            set_key(self.env_file, 'UPSTOX_TOKEN_REFRESHED_AT', datetime.now().isoformat())

        logger.info(f"[{self.account}] Environment file updated successfully")

    def run(self) -> str:
        """Main method to refresh and save the token."""
        new_token = self.refresh_token()
        self.update_env_file(new_token)
        return new_token


def send_notification(title: str, message: str, success: bool = True):
    """Send macOS notification."""
    import subprocess
    sound = "Glass" if success else "Basso"
    script = f'display notification "{message}" with title "{title}" sound name "{sound}"'
    subprocess.run(['osascript', '-e', script], capture_output=True)


def already_refreshed_today(env_file: str, account: str = 'BALA') -> bool:
    """Check if token was already refreshed today for the specified account."""
    load_dotenv(env_file, override=True)

    # Check account-specific timestamp
    last_refresh = os.getenv(f'UPSTOX_{account}_TOKEN_REFRESHED_AT')

    # Fallback to legacy key for BALA
    if not last_refresh and account == 'BALA':
        last_refresh = os.getenv('UPSTOX_TOKEN_REFRESHED_AT')

    if not last_refresh:
        return False

    try:
        # Handle quoted values from dotenv
        last_refresh = last_refresh.strip("'\"")
        refresh_date = datetime.fromisoformat(last_refresh).date()
        return refresh_date == datetime.now().date()
    except (ValueError, TypeError):
        return False


def refresh_account(env_file: str, account: str, notify: bool, if_needed: bool) -> bool:
    """Refresh a single account. Returns True on success."""
    logger.info(f"{'='*50}")
    logger.info(f"[{account}] Starting token refresh at {datetime.now()}")
    logger.info(f"{'='*50}")

    # Check if already refreshed today
    if if_needed and already_refreshed_today(env_file, account):
        logger.info(f"[{account}] Token already refreshed today. Skipping.")
        return True

    try:
        refresher = TokenRefresher(env_file, account)
        new_token = refresher.run()

        logger.info(f"[{account}] New token (first 50 chars): {new_token[:50]}...")
        logger.info(f"[{account}] Token refresh completed successfully!")

        if notify:
            send_notification(
                f"Upstox Token Refreshed ({account})",
                "Access token updated successfully",
                success=True
            )
        return True

    except Exception as e:
        logger.error(f"[{account}] Token refresh FAILED: {e}")

        if notify:
            send_notification(
                f"Upstox Token Refresh Failed ({account})",
                str(e)[:100],
                success=False
            )
        return False


def main():
    parser = argparse.ArgumentParser(description='Refresh Upstox access token')
    parser.add_argument(
        '--env-file',
        default=str(DEFAULT_ENV_FILE),
        help='Path to .env file'
    )
    parser.add_argument(
        '--account',
        type=str,
        choices=SUPPORTED_ACCOUNTS + ['ALL'],
        default='BALA',
        help='Account to refresh (BALA, NIMMY, or ALL). Default: BALA'
    )
    parser.add_argument(
        '--notify',
        action='store_true',
        help='Send macOS notification on completion'
    )
    parser.add_argument(
        '--if-needed',
        action='store_true',
        help='Only refresh if not already done today (for wake/interval triggers)'
    )
    args = parser.parse_args()

    if not Path(args.env_file).exists():
        logger.error("Env file not found: %s", args.env_file)
        logger.error("Create it first with: cp .env.example .env")
        return 1

    # Determine which accounts to refresh
    if args.account == 'ALL':
        accounts = SUPPORTED_ACCOUNTS
    else:
        accounts = [args.account.upper()]

    # Refresh each account
    results = {}
    for account in accounts:
        success = refresh_account(args.env_file, account, args.notify, args.if_needed)
        results[account] = success

    # Summary
    logger.info(f"\n{'='*50}")
    logger.info("REFRESH SUMMARY")
    logger.info(f"{'='*50}")
    for account, success in results.items():
        status = "SUCCESS" if success else "FAILED"
        logger.info(f"  {account}: {status}")
    logger.info(f"{'='*50}")

    # Return error code if any failed
    if all(results.values()):
        return 0
    else:
        return 1


if __name__ == '__main__':
    sys.exit(main())
