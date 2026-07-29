"""Create an application user in the review-records SQLite database.

Run:
    python scripts/create_user.py --username admin --role admin
"""

from __future__ import annotations

import argparse
import sys
from getpass import getpass
from pathlib import Path
from typing import Sequence


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.users import UserAlreadyExistsError, create_user  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create a platform user account.")
    parser.add_argument("--username", required=True, help="Unique login username.")
    parser.add_argument("--role", default="user", help="Account role (default: user).")
    parser.add_argument(
        "--inactive",
        action="store_true",
        help="Create the account in an inactive state.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    password = getpass("Password: ")
    password_confirmation = getpass("Confirm password: ")
    if password != password_confirmation:
        print("Passwords do not match.", file=sys.stderr)
        return 2

    try:
        user = create_user(
            username=args.username,
            password=password,
            role=args.role,
            is_active=not args.inactive,
        )
    except (UserAlreadyExistsError, ValueError) as exc:
        print(f"Could not create user: {exc}", file=sys.stderr)
        return 1

    status = "active" if user["is_active"] else "inactive"
    print(
        f"Created user '{user['username']}' "
        f"with role '{user['role']}' ({status})."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
