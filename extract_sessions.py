#!/usr/bin/env python3
"""
Session Digest Helper (v2, Aug 2026)

Mechanical companion to the session-digest skill. Three modes (selected by first arg):

  --pending                                          List sessions awaiting digest (JSON to stdout)
  --write-entry '<md>' --date YYYY-MM-DD             Append entry to that date's file
  --record <sid> --noteworthy <0|1> --digest-file F  Mark session digested (idempotent)

The LLM (agent) owns: summary text, keywords, noteworthy flag, reason, Telegram copy.
This script owns: SQLite queries, markdown file I/O, state-db writes.

Pure stdlib (Python 3.11+). No LLM calls. No external HTTP.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
STATE_DB = HERMES_HOME / "state.db"
DIGEST_STATE_DB = HERMES_HOME / "digest_state.db"
DIGEST_DIR = HERMES_HOME / "knowledge" / "session-digests"
LOG_FILE = HERMES_HOME / "logs" / "session_digest.log"

MIN_MESSAGES = 5
MAX_MESSAGES_PER_RUN = 20
MAX_TRANSCRIPT_CHARS = 6000


def log(msg: str) -> None:
    """Best-effort logging. Never raises."""
    ts = datetime.now().isoformat(timespec="seconds")
    line = f"[{ts}] {msg}"
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _extract_text(content) -> str:
    """Extract text from a message content field (handles Anthropic-style content arrays)."""
    if not content:
        return ""
    if isinstance(content, str):
        if content.startswith("["):
            try:
                parsed = json.loads(content)
                parts = []
                for p in parsed:
                    if isinstance(p, dict):
                        parts.append(p.get("text", ""))
                    elif isinstance(p, str):
                        parts.append(p)
                return "\n".join(parts)
            except json.JSONDecodeError:
                return content
        return content
    return str(content)


def init_digest_state_db() -> sqlite3.Connection:
    DIGEST_STATE_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DIGEST_STATE_DB))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS digested (
            session_id TEXT PRIMARY KEY,
            digested_at TEXT NOT NULL,
            digest_file TEXT NOT NULL,
            noteworthy INTEGER NOT NULL DEFAULT 0,
            keywords TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_digested_at ON digested(digested_at)"
    )
    conn.commit()
    return conn


def get_digested_ids(conn: sqlite3.Connection) -> set[str]:
    cur = conn.execute("SELECT session_id FROM digested")
    return {row[0] for row in cur.fetchall()}


def get_last_digest_ts(conn: sqlite3.Connection) -> float:
    """Unix timestamp of last digest run, or 0 if never."""
    row = conn.execute("SELECT MAX(digested_at) FROM digested").fetchone()
    if not row or not row[0]:
        return 0.0
    try:
        return datetime.fromisoformat(row[0]).timestamp()
    except (ValueError, TypeError):
        return 0.0


def extract_session_context(session_id: str) -> dict | None:
    """Read first user msg, last assistant msg, tool list, role counts from state.db."""
    if not STATE_DB.exists():
        return None
    conn = sqlite3.connect(str(STATE_DB))
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(
            """
            SELECT content FROM messages
            WHERE session_id = ? AND role = 'user' AND active = 1
            ORDER BY id ASC LIMIT 1
            """,
            (session_id,),
        )
        first_user = cur.fetchone()
        first_user_text = _extract_text(first_user["content"] if first_user else None)

        cur = conn.execute(
            """
            SELECT content FROM messages
            WHERE session_id = ? AND role = 'assistant' AND active = 1
            ORDER BY id DESC LIMIT 1
            """,
            (session_id,),
        )
        last_assistant = cur.fetchone()
        last_assistant_text = _extract_text(last_assistant["content"] if last_assistant else None)

        cur = conn.execute(
            """
            SELECT tool_name FROM messages
            WHERE session_id = ? AND role = 'tool' AND active = 1
            ORDER BY id ASC
            """,
            (session_id,),
        )
        tools_used = []
        for row in cur.fetchall():
            tname = row["tool_name"]
            if tname and tname not in tools_used:
                tools_used.append(tname)

        cur = conn.execute(
            """
            SELECT role, COUNT(*) as cnt FROM messages
            WHERE session_id = ? AND active = 1
            GROUP BY role
            """,
            (session_id,),
        )
        role_counts = {r["role"]: r["cnt"] for r in cur.fetchall()}
    finally:
        conn.close()

    if not first_user_text and not last_assistant_text:
        return None

    return {
        "first_user_message": (first_user_text or "")[:MAX_TRANSCRIPT_CHARS],
        "last_assistant_message": (last_assistant_text or "")[:MAX_TRANSCRIPT_CHARS],
        "tools_used": tools_used,
        "role_counts": role_counts,
    }


def find_sessions(since_ts: float, until_ts: float, exclude_ids: set[str]) -> list[dict]:
    if not STATE_DB.exists():
        log(f"ERROR: state.db not found at {STATE_DB}")
        return []
    conn = sqlite3.connect(str(STATE_DB))
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(
            """
            SELECT id, title, source, started_at, last_activity_at,
                   message_count, display_name
            FROM sessions
            WHERE last_activity_at > ?
              AND last_activity_at < ?
              AND message_count >= ?
              AND last_activity_at IS NOT NULL
              AND ended_at IS NOT NULL
            ORDER BY last_activity_at ASC
            """,
            (since_ts, until_ts, MIN_MESSAGES),
        )
        rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
    return [r for r in rows if r["id"] not in exclude_ids]


def cmd_pending() -> int:
    """List sessions awaiting digest as JSON to stdout."""
    state_conn = init_digest_state_db()
    digested_ids = get_digested_ids(state_conn)
    last_ts = get_last_digest_ts(state_conn)
    if last_ts > 0:
        since_ts = last_ts
    else:
        since_ts = (datetime.now() - timedelta(days=2)).timestamp()
    until_ts = datetime.now().timestamp()

    sessions = find_sessions(since_ts, until_ts, digested_ids)
    if len(sessions) > MAX_MESSAGES_PER_RUN:
        log(f"WARNING: {len(sessions)} pending, capping at {MAX_MESSAGES_PER_RUN}")
        sessions = sessions[:MAX_MESSAGES_PER_RUN]

    enriched = []
    for s in sessions:
        ctx = extract_session_context(s["id"])
        if ctx is None:
            ctx = {
                "first_user_message": "",
                "last_assistant_message": "",
                "tools_used": [],
                "role_counts": {},
            }
        s_out = dict(s)
        if s.get("started_at"):
            s_out["started_at"] = datetime.fromtimestamp(s["started_at"]).isoformat(timespec="seconds")
        if s.get("last_activity_at"):
            s_out["last_activity_at"] = datetime.fromtimestamp(s["last_activity_at"]).isoformat(timespec="seconds")
        s_out["end_date"] = (
            datetime.fromtimestamp(s["last_activity_at"]).strftime("%Y-%m-%d")
            if s.get("last_activity_at") else None
        )
        s_out.update(ctx)
        enriched.append(s_out)

    # JSON ONLY on stdout. Logs go to stderr via Python logging if needed.
    print(json.dumps({"sessions": enriched, "count": len(enriched)}, ensure_ascii=False, indent=2))
    return 0


def cmd_write_entry(entry: str, date_str: str) -> int:
    """Append a markdown entry to the date's digest file."""
    DIGEST_DIR.mkdir(parents=True, exist_ok=True)
    digest_file = DIGEST_DIR / f"{date_str}.md"
    if not digest_file.exists():
        digest_file.write_text(f"# Session digest — {date_str}\n\n")
    existing = digest_file.read_text()
    if existing and not existing.endswith("\n\n"):
        if existing.endswith("\n"):
            existing += "\n"
        else:
            existing += "\n\n"
    digest_file.write_text(existing + entry + "\n")
    print(str(digest_file.absolute()))
    log(f"wrote entry to {digest_file.name}")
    return 0


def cmd_record(sid: str, noteworthy: int, digest_file: str, keywords: str = "") -> int:
    """Idempotent UPSERT into digest_state.db."""
    conn = init_digest_state_db()
    conn.execute(
        """
        INSERT INTO digested (session_id, digested_at, digest_file, noteworthy, keywords)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(session_id) DO UPDATE SET
            digested_at = excluded.digested_at,
            digest_file = excluded.digest_file,
            noteworthy = excluded.noteworthy,
            keywords = excluded.keywords
        """,
        (
            sid,
            datetime.now().isoformat(timespec="seconds"),
            digest_file,
            noteworthy,
            keywords,
        ),
    )
    conn.commit()
    print(json.dumps({"ok": True, "session_id": sid, "noteworthy": bool(noteworthy)}))
    log(f"recorded {sid} noteworthy={bool(noteworthy)}")
    return 0


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: extract_sessions.py [--pending | --write-entry ... | --record ...]", file=sys.stderr)
        return 2

    mode = sys.argv[1]

    if mode == "--pending":
        if len(sys.argv) != 2:
            print("--pending takes no other arguments", file=sys.stderr)
            return 2
        return cmd_pending()

    if mode == "--write-entry":
        # The entry text is markdown that typically starts with "## " (heading),
        # so it MUST be passed with --entry="..." syntax; otherwise argparse
        # treats it as a positional argument. Same for --date.
        parser = argparse.ArgumentParser()
        parser.add_argument("--entry", required=True, help="Markdown entry text (use --entry=\"...\" syntax)")
        parser.add_argument("--date", required=True, help="End-date YYYY-MM-DD (use --date=YYYY-MM-DD)")
        args = parser.parse_args(sys.argv[2:])
        return cmd_write_entry(args.entry, args.date)

    if mode == "--record":
        parser = argparse.ArgumentParser()
        parser.add_argument("--sid", required=True, help="Session ID")
        parser.add_argument("--noteworthy", type=int, choices=[0, 1], required=True)
        parser.add_argument("--digest-file", required=True)
        parser.add_argument("--keywords", default="")
        args = parser.parse_args(sys.argv[2:])
        return cmd_record(args.sid, args.noteworthy, args.digest_file, args.keywords)

    print(f"Unknown mode: {mode}. Use --pending, --write-entry, or --record.", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())