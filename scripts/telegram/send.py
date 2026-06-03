#!/usr/bin/env python3
"""Send a Telegram message to a chat by ID.

Usage (run with the Telethon-capable interpreter, $VENV_PYTHON):
    "$VENV_PYTHON" "$CLAUDE_SKILL_DIR/scripts/telegram/send.py" \\
        <chat_id> <message_text> [--topic <thread_id>]

chat_id may be:
    - A user id (positive int, e.g. a personal DM target)
    - A group/supergroup id (negative int, e.g. a delivery group)
    - "me" or "self" for Saved Messages

Exits non-zero on send failure (so callers' `returncode == 0` checks work).
"""
import argparse
import asyncio
import os
import sys

# auth.py lives next to this file (scripts/telegram/auth.py)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from auth import make_client, session_lock  # noqa: E402


def _parse_chat(raw):
    if raw in ("me", "self"):
        return "me"
    try:
        return int(raw)
    except ValueError:
        return raw


async def main():
    p = argparse.ArgumentParser()
    p.add_argument("chat_id", help="Chat id (int or 'me')")
    p.add_argument("message", help="Message text to send")
    p.add_argument("--topic", type=int, default=None,
                   help="Optional forum topic / thread id")
    p.add_argument("--silent", action="store_true",
                   help="Send without notification")
    args = p.parse_args()

    chat = _parse_chat(args.chat_id)
    with session_lock():
        async with make_client() as client:
            kwargs = {}
            if args.topic is not None:
                kwargs["reply_to"] = args.topic
            if args.silent:
                kwargs["silent"] = True
            sent = await client.send_message(chat, args.message, **kwargs)
            print(f"sent id={sent.id} to chat={chat}", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
