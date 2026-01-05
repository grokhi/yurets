from __future__ import annotations

import argparse
import asyncio
import os
import re
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import ApiIdInvalidError


def _required_int(value: str, name: str) -> int:
    try:
        return int(value)
    except Exception as exc:  # pragma: no cover
        raise SystemExit(f"{name} must be an integer") from exc


async def _amain() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m src.telegram_login",
        description=(
            "Interactive Telegram login (user session) for Юрец ФМ. "
            "Creates/updates Telethon .session file."
        ),
    )
    parser.add_argument(
        "--api-id",
        default=os.getenv("YURETS_TELEGRAM_API_ID", ""),
        help="Telegram api_id (or env YURETS_TELEGRAM_API_ID)",
    )
    parser.add_argument(
        "--api-hash",
        default=os.getenv("YURETS_TELEGRAM_API_HASH", ""),
        help="Telegram api_hash (or env YURETS_TELEGRAM_API_HASH)",
    )
    parser.add_argument(
        "--session",
        default=os.getenv("YURETS_TELEGRAM_SESSION", "./telegram_session/yurets_fm.session"),
        help="Session file path (or env YURETS_TELEGRAM_SESSION)",
    )

    args = parser.parse_args()

    if not args.api_id:
        raise SystemExit("Missing --api-id (or env YURETS_TELEGRAM_API_ID)")
    if not args.api_hash:
        raise SystemExit("Missing --api-hash (or env YURETS_TELEGRAM_API_HASH)")

    api_id = _required_int(args.api_id, "api-id")
    api_hash = args.api_hash

    if api_id <= 0:
        raise SystemExit("api-id must be a positive integer")

    # Telegram api_hash is typically a 32-char hex string.
    if not re.fullmatch(r"[0-9a-fA-F]{32}", api_hash or ""):
        raise SystemExit(
            "api-hash does not look valid (expected 32 hex chars). "
            "Double-check you copied it from my.telegram.org > API development tools."
        )

    session_path = Path(args.session)
    session_path.parent.mkdir(parents=True, exist_ok=True)

    client = TelegramClient(str(session_path), api_id, api_hash)

    print("This will ask for your phone number and login code in the terminal.")
    print(f"Session file: {session_path}")

    try:
        await client.start()  # interactive user login
    except ApiIdInvalidError as exc:
        raise SystemExit(
            "Telegram rejected api_id/api_hash (ApiIdInvalidError).\n"
            "Most common causes:\n"
            "- You copied api_id/api_hash from the wrong place (must be from my.telegram.org > API development tools)\n"
            "- api_id is not an integer, or api_hash has extra spaces/quotes\n"
            "Fix the values and re-run this command."
        ) from exc

    me = await client.get_me()
    username = getattr(me, "username", None)
    uid = getattr(me, "id", None)
    print(f"Authorized. user={username!r} id={uid!r}")

    await client.disconnect()


def main() -> None:
    asyncio.run(_amain())


if __name__ == "__main__":
    main()
