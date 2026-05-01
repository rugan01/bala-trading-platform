from __future__ import annotations

import argparse
from pathlib import Path

from trading_platform.archive.bootstrap import DEFAULT_DB_PATH, initialize_database


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Trading platform CLI",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_db = subparsers.add_parser(
        "init-db",
        help="Initialize the local archive database",
    )
    init_db.add_argument(
        "--db-path",
        type=Path,
        default=DEFAULT_DB_PATH,
        help="SQLite archive path",
    )

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "init-db":
        initialize_database(args.db_path)
        print(f"Initialized trading platform archive at {args.db_path}")
        return 0

    parser.error(f"Unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
