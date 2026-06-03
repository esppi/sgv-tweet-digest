#!/usr/bin/env python3
"""Read recent Telegram messages and emit as JSON.

Modes (mutually exclusive):
    --channel USERNAME    Read from a public channel (e.g. the SHOAL_CHANNEL)
    --contact NAME        Match a dialog by participant/title (substring, case-insensitive)
    --search TEXT         Global message search across all dialogs
    (default)             Read from Saved Messages (chat with yourself)

Always emits a JSON list to stdout (one object per message):
    {date, sender, text, full_text, chat_id, chat_title}

Idempotent and read-only — no Telegram side effects.

Run with the Telethon-capable interpreter ($VENV_PYTHON), e.g.:
    "$VENV_PYTHON" "$CLAUDE_SKILL_DIR/scripts/telegram/read.py" \\
        --channel "$SHOAL_CHANNEL" --limit 30 --json
"""
import argparse
import asyncio
import json
import os
import sys

# auth.py lives next to this file (scripts/telegram/auth.py)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from auth import make_client, session_lock  # noqa: E402


def _msg_to_dict(msg, chat_id=None, chat_title=None):
    text = msg.text or msg.message or ""
    try:
        sender = msg.sender
        if sender is None:
            sender_name = ""
        elif hasattr(sender, "username") and sender.username:
            sender_name = "@" + sender.username
        else:
            first = getattr(sender, "first_name", "") or ""
            last = getattr(sender, "last_name", "") or ""
            sender_name = (first + " " + last).strip() or str(getattr(sender, "id", ""))
    except Exception:
        sender_name = ""
    return {
        "id": msg.id,
        "date": msg.date.isoformat() if msg.date else None,
        "sender": sender_name,
        "text": text,
        "full_text": text,
        "chat_id": chat_id,
        "chat_title": chat_title,
    }


async def read_channel(client, username, limit):
    entity = await client.get_entity(username if username.startswith("@") else "@" + username)
    title = getattr(entity, "title", username)
    out = []
    async for msg in client.iter_messages(entity, limit=limit):
        out.append(_msg_to_dict(msg, chat_id=entity.id, chat_title=title))
    return out


async def read_contact(client, name, limit):
    """Find dialogs whose title or counterpart name contains `name` (case-insensitive)."""
    needle = name.lower()
    out = []
    async for dialog in client.iter_dialogs():
        title = (dialog.name or "").lower()
        ent = dialog.entity
        first = (getattr(ent, "first_name", "") or "").lower()
        last = (getattr(ent, "last_name", "") or "").lower()
        username = (getattr(ent, "username", "") or "").lower()
        full = f"{first} {last}".strip()
        if needle in title or needle in full or needle in username:
            chat_title = dialog.name or full or username
            async for msg in client.iter_messages(dialog.entity, limit=limit):
                out.append(_msg_to_dict(msg, chat_id=dialog.id, chat_title=chat_title))
            if len(out) >= limit:
                break
    return out[:limit] if limit else out


async def read_search(client, text, limit):
    out = []
    async for msg in client.iter_messages(None, search=text, limit=limit):
        chat = getattr(msg, "chat", None)
        chat_title = getattr(chat, "title", None) or getattr(chat, "first_name", None) or ""
        out.append(_msg_to_dict(msg, chat_id=msg.chat_id, chat_title=chat_title))
    return out


async def read_saved(client, limit):
    out = []
    async for msg in client.iter_messages("me", limit=limit):
        out.append(_msg_to_dict(msg, chat_id=None, chat_title="Saved Messages"))
    return out


async def main():
    p = argparse.ArgumentParser()
    p.add_argument("--channel", help="Public channel username (with or without @)")
    p.add_argument("--contact", help="Dialog/participant name substring")
    p.add_argument("--search", help="Search messages by text")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--json", action="store_true", help="Emit JSON (default behavior anyway)")
    args = p.parse_args()

    with session_lock():
        async with make_client() as client:
            if args.channel:
                out = await read_channel(client, args.channel, args.limit)
            elif args.contact:
                out = await read_contact(client, args.contact, args.limit)
            elif args.search:
                out = await read_search(client, args.search, args.limit)
            else:
                out = await read_saved(client, args.limit)

    json.dump(out, sys.stdout, ensure_ascii=False, default=str)
    sys.stdout.write("\n")


if __name__ == "__main__":
    asyncio.run(main())
