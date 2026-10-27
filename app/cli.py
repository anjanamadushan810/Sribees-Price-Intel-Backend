"""Administration commands, run on the server:

    python -m app.cli create-admin --email you@example.com --name "Full Name"
    python -m app.cli daily                    # sync Online, read every source, suggest
    python -m app.cli daily --no-sync --competitor 1
    python -m app.cli suggest                  # only recompute suggestions
    python -m app.cli scheduler                # the worker: daily run at 05:00 Colombo time

The super admin's password is read from the terminal, never from the command line.
"""

import argparse
import asyncio
import getpass
import json
import logging
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from app.core.db import get_sessionmaker
from app.core.security import MIN_PASSWORD_LENGTH, hash_password
from app.models import User
from app.services import audit, jobs, suggest


async def create_admin(email: str, name: str) -> None:
    password = getpass.getpass("Super admin password: ")
    if len(password) < MIN_PASSWORD_LENGTH:
        sys.exit(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
    if password != getpass.getpass("Repeat: "):
        sys.exit("passwords differ")
    async with get_sessionmaker()() as db, db.begin():
        if await db.scalar(select(User).where(func.lower(User.email) == email.lower())):
            sys.exit("a user with this email exists")
        user = User(email=email.lower(), full_name=name, role="super_admin",
                    password_hash=hash_password(password))
        db.add(user)
        await db.flush()
        await audit.record(db, user_id=None, action="user.create", entity="user",
                           entity_id=user.user_id, after={"email": user.email,
                                                          "role": "super_admin", "via": "cli"})
    print("super admin created; two-factor set-up starts at first sign-in")


async def suggest_only() -> None:
    async with get_sessionmaker()() as db, db.begin():
        print(json.dumps(await suggest.refresh(db)))


COLOMBO = ZoneInfo("Asia/Colombo")
RUN_AT_HOUR = 5  # before the shops open; Online's prices are read fresh first


def seconds_until_next_run(now: datetime) -> float:
    local = now.astimezone(COLOMBO)
    target = local.replace(hour=RUN_AT_HOUR, minute=0, second=0, microsecond=0)
    if target <= local:
        target += timedelta(days=1)
    return (target - local).total_seconds()


async def scheduler() -> None:
    log = logging.getLogger("intel.scheduler")
    while True:
        wait = seconds_until_next_run(datetime.now(COLOMBO))
        log.info("next daily run in %.0f minutes", wait / 60)
        await asyncio.sleep(wait)
        try:
            await jobs.daily(get_sessionmaker())
        except Exception:  # noqa: BLE001 -- logged; tomorrow's run tries again
            log.exception("daily run failed")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    admin = sub.add_parser("create-admin")
    admin.add_argument("--email", required=True)
    admin.add_argument("--name", required=True)
    daily = sub.add_parser("daily")
    daily.add_argument("--no-sync", action="store_true")
    daily.add_argument("--competitor", type=int, action="append")
    sub.add_parser("suggest")
    sub.add_parser("scheduler")
    args = parser.parse_args()

    if args.command == "create-admin":
        asyncio.run(create_admin(args.email, args.name))
    elif args.command == "daily":
        report = asyncio.run(jobs.daily(get_sessionmaker(), sync=not args.no_sync,
                                        competitor_ids=args.competitor))
        print(json.dumps(report, default=str))
    elif args.command == "scheduler":
        asyncio.run(scheduler())
    else:
        asyncio.run(suggest_only())


if __name__ == "__main__":
    main()
