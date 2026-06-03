"""Shared Telethon auth helper for the sgv-tweet-digest scripts.

Single source of truth for API_ID / API_HASH / Telethon version / default
session path. All three values come from the environment so no credentials
are ever committed:

    TELEGRAM_API_ID    application id   (my.telegram.org)
    TELEGRAM_API_HASH  application hash (my.telegram.org)
    TELEGRAM_SESSION   path to the Telethon user-session file
                       (default: $XDG_DATA_HOME/sgv-tweet-digest/session.session,
                        i.e. ~/.local/share/sgv-tweet-digest/session.session)

Usage:
    from auth import make_client, session_lock

    with session_lock():           # cross-process flock, prevents Telegram
        async with make_client() as client:   # session revocation when 2 procs
            async for msg in client.iter_messages("me", limit=10):
                ...

Callers that need a non-canonical session file can pass `session_path=...` to
both helpers — they must pass the same path to both for the lock to actually
protect the session.

Telethon must be importable. Install it into your venv (see requirements.txt:
telethon==1.43.1) and run all Telegram I/O with that interpreter (VENV_PYTHON).
"""
import os
import fcntl
import time
import contextlib
from pathlib import Path

from telethon import TelegramClient  # noqa: E402


def _state_dir():
    """Per-user runtime state dir: $XDG_DATA_HOME/sgv-tweet-digest (or ~/.local/share/...)."""
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "share"
    )
    return os.path.join(base, "sgv-tweet-digest")


def _api_id():
    raw = os.environ.get("TELEGRAM_API_ID")
    if not raw:
        raise RuntimeError(
            "TELEGRAM_API_ID is not set. Create an app at my.telegram.org and "
            "export TELEGRAM_API_ID + TELEGRAM_API_HASH."
        )
    try:
        return int(raw)
    except ValueError:
        raise RuntimeError("TELEGRAM_API_ID must be an integer (got: %r)" % raw)


def _api_hash():
    val = os.environ.get("TELEGRAM_API_HASH")
    if not val:
        raise RuntimeError(
            "TELEGRAM_API_HASH is not set. Create an app at my.telegram.org and "
            "export TELEGRAM_API_ID + TELEGRAM_API_HASH."
        )
    return val


DEFAULT_SESSION = os.environ.get(
    "TELEGRAM_SESSION", os.path.join(_state_dir(), "session.session")
)


def make_client(session_path=None):
    """Return a configured (not-yet-connected) TelegramClient.

    Telethon's SQLiteSession appends `.session`, so we strip the suffix if the
    caller passed a path with it already present. API_ID / API_HASH are read
    from the environment at call time.
    """
    p = session_path or DEFAULT_SESSION
    Path(p).parent.mkdir(parents=True, exist_ok=True)
    base = p[:-len(".session")] if p.endswith(".session") else p
    return TelegramClient(base, _api_id(), _api_hash())


@contextlib.contextmanager
def session_lock(session_path=None, timeout_s=180):
    """Cross-process exclusive lock on a session file.

    Two Telethon clients opening the same session concurrently can cause
    Telegram to revoke the session. This lock serializes access. Lock file
    lives next to the session file with a `.lock` suffix.

    Blocks up to `timeout_s` then raises TimeoutError.
    """
    p = session_path or DEFAULT_SESSION
    Path(p).parent.mkdir(parents=True, exist_ok=True)
    lock_path = p + ".lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    deadline = time.monotonic() + timeout_s
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"session_lock({lock_path}) timed out after {timeout_s}s"
                    )
                time.sleep(0.5)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
