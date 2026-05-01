from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[4]
PACKAGE_ROOT = REPO_ROOT / "packages" / "trading_platform"
DATA_ROOT = REPO_ROOT / "data"
ARCHIVE_ROOT = DATA_ROOT / "archive"
REPORTS_ROOT = DATA_ROOT / "reports"
PREMARKET_REPORTS_ROOT = REPORTS_ROOT / "premarket"
LEGACY_ANALYZER_OUTPUT_ROOT = DATA_ROOT / "legacy-analyzers"
ENV_FILE = REPO_ROOT / ".env"

