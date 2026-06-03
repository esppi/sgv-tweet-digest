#!/bin/bash
# Usage: send_message.sh <chat_id> <message_text> [send_script_path]
#
# Thin CLI wrapper around scripts/telegram/send.py. Must use a Telethon-capable
# interpreter (Telethon 1.43.1) — an older system telethon can't read the current
# session schema, so the interpreter is pinned via $VENV_PYTHON.
#
# Resolution order:
#   interpreter  : $VENV_PYTHON, else `python3` on PATH
#   send script  : 3rd arg, else $CLAUDE_SKILL_DIR/scripts/telegram/send.py,
#                  else the send.py sitting next to this script (scripts/telegram/)
set -e

VENV_PY="${VENV_PYTHON:-python3}"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -n "$3" ]; then
    SEND_SCRIPT="$3"
elif [ -n "$CLAUDE_SKILL_DIR" ]; then
    SEND_SCRIPT="$CLAUDE_SKILL_DIR/scripts/telegram/send.py"
else
    SEND_SCRIPT="$HERE/telegram/send.py"
fi

"$VENV_PY" "$SEND_SCRIPT" "$1" "$2"
