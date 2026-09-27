#!/usr/bin/env python3
"""Daily session digest helper (stdlib only, no LLM).

The cron agent drives three subcommands; everything mechanical lives here so the
(weak) cron model only has to dispatch subagents and copy file paths.

  digest.py pending            write one transcript per pending session, print slim JSON
  digest.py commit SID RESULT  parse the subagent's RESULT file, append entry, record in state DB
  digest.py report             print the Telegram text for this run, or [SILENT]
  digest.py transcript SID     print one transcript to stdout (debugging)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
STATE_DB = HERMES_HOME / "state.db"
DIGEST_STATE_DB = HERMES_HOME / "digest_state.db"
DIGEST_DIR = HERMES_HOME / "knowledge" / "session-digests"
LOG_FILE = HERMES_HOME / "logs" / "session_digest.log"
WORK_DIR = HERMES_HOME / "cache" / "session-digest"
RUN_FILE = WORK_DIR / "run.json"

JOB_ID = "6d986a839a80"          # this digest's own cron job; its sessions are never digested
MIN_MESSAGES = 5
MAX_SESSIONS_PER_RUN = 20         # leftovers are picked up by the next run
LOOKBACK_DAYS = 14
PING_MAX_ITEMS = 5
PING_SUMMARY_CHARS = 200
# No truncation: after the last compaction the transcript is bounded by the source
# session's context window. Warn only, so outliers are visible in the log.
WARN_TRANSCRIPT_CHARS = 400_000   # ~100k tokens
END_MARKER = "=== END OF TRANSCRIPT ==="

TOOL_PLACEHOLDER = ">>> Tool call <<<"

# Runtime injections stored as role='user'. Relabelled so the summarizer does not
# mistake them for user speech. Checked with re.match (prefix only).
SYSTEM_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\[ASYNC DELEGATION"), "system (delegation result)"),
    (re.compile(r"\[(IMPORTANT|SYSTEM): Background process"), "system (background process)"),
    (re.compile(r"\[(IMPORTANT|SYSTEM): You are running as a scheduled cron job"), "system (cron job)"),
    (re.compile(r'\[(IMPORTANT|SYSTEM): The user has invoked the "'), "system (skill invocation)"),
    (re.compile(r"\[OUT-OF-BAND USER MESSAGE"), "system (out-of-band user message)"),
    (re.compile(r"\[The user sent an image but"), "system (image fetch failed)"),
    (re.compile(r"Gateway message origin"), "system (gateway metadata)"),
    (re.compile(r"\[(SYSTEM|System)( note)?:"), "system"),
]
# In-place compaction summaries; may be prefixed by a "[PRIOR CONTEXT ...]" block.
COMPACTION_PREFIXES = ("[CONTEXT COMPACTION", "[PRIOR CONTEXT")

SECTION_RE = re.compile(r"^[\s*#]*(SUMMARY|KEYWORDS|NOTEWORTHY|REASON)[\s*]*:[\s*]*(.*)$", re.I)


def log(msg: str) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a") as f:
        f.write(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}\n")


def state_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def digest_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DIGEST_STATE_DB)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS digested (
               session_id  TEXT PRIMARY KEY,
               digested_at TEXT NOT NULL,
               digest_file TEXT NOT NULL,
               noteworthy  INTEGER NOT NULL DEFAULT 0,
               keywords    TEXT)"""
    )
    return conn


def fmt_time(ts: float | None, fmt: str) -> str:
    return datetime.fromtimestamp(ts).strftime(fmt) if ts else "?"


def transcript_path(sid: str) -> Path:
    return WORK_DIR / f"{sid}.txt"


# --- pending ----------------------------------------------------------------

def find_pending() -> list[sqlite3.Row]:
    """Ended sessions not yet digested. No time cursor: anything missed (open at run
    time, over the per-run cap) is still found next run, within LOOKBACK_DAYS."""
    with digest_conn() as d:
        done = {r[0] for r in d.execute("SELECT session_id FROM digested")}
    since = (datetime.now() - timedelta(days=LOOKBACK_DAYS)).timestamp()
    own = f"cron_{JOB_ID}_%"
    with state_conn() as s:
        rows = s.execute(
            """SELECT id, title, source, started_at, last_activity_at, message_count
                 FROM sessions
                WHERE ended_at IS NOT NULL
                  AND message_count >= ?
                  AND last_activity_at > ?
                  AND id NOT LIKE ?
                  AND COALESCE(parent_session_id, '') NOT LIKE ?
                ORDER BY last_activity_at""",
            (MIN_MESSAGES, since, own, own),
        ).fetchall()
    return [r for r in rows if r["id"] not in done]


def cmd_pending(_args) -> int:
    rows = find_pending()
    if len(rows) > MAX_SESSIONS_PER_RUN:
        log(f"{len(rows)} pending, processing {MAX_SESSIONS_PER_RUN}; rest next run")
        rows = rows[:MAX_SESSIONS_PER_RUN]

    WORK_DIR.mkdir(parents=True, exist_ok=True)
    WORK_DIR.chmod(0o700)
    for old in WORK_DIR.iterdir():
        old.unlink()

    out = []
    for r in rows:
        path = transcript_path(r["id"])
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        text = render_transcript(r["id"])
        if len(text) > WARN_TRANSCRIPT_CHARS:
            log(f"WARNING: transcript {r['id']} is {len(text):,} chars (> {WARN_TRANSCRIPT_CHARS:,})")
        with os.fdopen(fd, "w") as f:
            f.write(text)
        out.append({"id": r["id"], "title": r["title"] or "(untitled)", "transcript": str(path)})

    RUN_FILE.write_text(json.dumps({"started": datetime.now().isoformat(timespec="seconds"),
                                    "ids": [o["id"] for o in out]}))
    log(f"pending: {len(out)} session(s)")
    print(json.dumps({"count": len(out), "sessions": out}, ensure_ascii=False, indent=2))
    return 0


# --- transcript -------------------------------------------------------------

def label_for(role: str, content: str) -> str:
    if content.startswith(COMPACTION_PREFIXES):
        return "context compaction"
    if role == "user":
        for pat, label in SYSTEM_PATTERNS:
            if pat.match(content):
                return label
    return role


def render_transcript(sid: str) -> str:
    """Active rows in id order = what the model saw last: head turns, the compaction
    summary (if any) standing in for the dropped middle, then the tail."""
    with state_conn() as s:
        meta = s.execute("SELECT title, source, message_count FROM sessions WHERE id = ?",
                         (sid,)).fetchone()
        rows = s.execute(
            """SELECT role, content FROM messages
                WHERE session_id = ? AND active = 1 AND role != 'session_meta'
                ORDER BY id""",
            (sid,),
        ).fetchall()

    lines = [f"# session_id: {sid}\n",
             f"# title:      {(meta and meta['title']) or '(untitled)'}\n",
             f"# source:     {meta and meta['source']}\n\n"]
    tools = 0
    last = None

    def flush_tools() -> None:
        nonlocal tools
        if tools:
            lines.append(f"{TOOL_PLACEHOLDER}  (x{tools})\n\n" if tools > 1 else f"{TOOL_PLACEHOLDER}\n\n")
            tools = 0

    for r in rows:
        content = (r["content"] or "").rstrip()
        if r["role"] == "tool" or (r["role"] == "assistant" and not content):
            tools += 1                   # tool results and tool-call-only assistant turns
            continue
        if not content:
            continue
        label = label_for(r["role"], content)
        if (label, content) == last:     # Hermes sometimes writes a row more than once
            continue
        last = (label, content)
        flush_tools()
        lines.append(f"--- {label} ---\n{content}\n\n")
    flush_tools()
    lines.append(END_MARKER + "\n")
    return "".join(lines)


def cmd_transcript(args) -> int:
    sys.stdout.write(render_transcript(args.sid))
    return 0


# --- commit -----------------------------------------------------------------

def parse_result(text: str) -> dict:
    """Parse the subagent reply. Raises ValueError with a readable reason."""
    sections: dict[str, list[str]] = {}
    current = None
    for line in text.splitlines():
        if line.strip().startswith("```"):
            continue
        m = SECTION_RE.match(line)
        if m:
            current = m.group(1).upper()
            sections[current] = [m.group(2)] if m.group(2).strip() else []
        elif current:
            sections[current].append(line)
    get = lambda k: "\n".join(sections.get(k, [])).strip()

    summary = get("SUMMARY")
    keywords = []
    for kw in re.split(r"[,\n]", get("KEYWORDS")):
        kw = re.sub(r"\s+", "-", kw.strip(" -*`.").lower())
        if kw and kw not in keywords:
            keywords.append(kw)
    flag = get("NOTEWORTHY").lower().strip("*_ .")
    reason = get("REASON").strip()

    if not summary:
        raise ValueError("SUMMARY section missing or empty")
    if not keywords:
        raise ValueError("KEYWORDS section missing or empty")
    if flag not in ("yes", "no"):
        raise ValueError(f"NOTEWORTHY must be yes or no, got {flag!r}")
    noteworthy = flag == "yes"
    if noteworthy and reason in ("", "-"):
        raise ValueError("NOTEWORTHY is yes but REASON is empty")
    return {"summary": summary, "keywords": keywords[:10],
            "noteworthy": noteworthy, "reason": reason if noteworthy else ""}


SOURCE_TAGS = {"telegram": "TG", "cli": "CLI"}


def render_entry(meta: sqlite3.Row, res: dict) -> str:
    tag = SOURCE_TAGS.get(meta["source"], meta["source"])
    flag = f"YES — {res['reason']}" if res["noteworthy"] else "no"
    return (
        f"## {fmt_time(meta['last_activity_at'], '%H:%M')} [{tag}] — {meta['title'] or '(untitled)'}\n"
        f"**Session ID:** `{meta['id']}` ({meta['message_count']} messages)\n"
        f"**Started:** {fmt_time(meta['started_at'], '%Y-%m-%d %H:%M')} · "
        f"**Ended:** {fmt_time(meta['last_activity_at'], '%Y-%m-%d %H:%M')}\n"
        f"**Keywords:** {', '.join(res['keywords'])}\n"
        f"**Noteworthy:** {flag}\n\n"
        f"{res['summary']}\n\n---\n\n"
    )


def cmd_commit(args) -> int:
    sid, result_file = args.sid, Path(args.result_file)
    with digest_conn() as d:
        if d.execute("SELECT 1 FROM digested WHERE session_id = ?", (sid,)).fetchone():
            print(f"OK {sid} already committed")
            return 0
    try:
        if not result_file.exists():
            raise ValueError(f"result file not found: {result_file}")
        res = parse_result(result_file.read_text())
        with state_conn() as s:
            meta = s.execute(
                "SELECT id, title, source, started_at, last_activity_at, message_count "
                "FROM sessions WHERE id = ?", (sid,)).fetchone()
        if meta is None:
            raise ValueError(f"unknown session id: {sid}")
    except ValueError as e:
        log(f"commit {sid} FAILED: {e}")
        print(f"ERROR: {e}")
        return 1

    DIGEST_DIR.mkdir(parents=True, exist_ok=True)
    digest_file = DIGEST_DIR / f"{fmt_time(meta['last_activity_at'], '%Y-%m-%d')}.md"
    if not digest_file.exists():
        digest_file.write_text(f"# Session digest — {digest_file.stem}\n\n")
    if f"`{sid}`" not in digest_file.read_text():     # re-commit after a crash: no duplicate entry
        with digest_file.open("a") as f:
            f.write(render_entry(meta, res))

    with digest_conn() as d:
        d.execute(
            """INSERT INTO digested (session_id, digested_at, digest_file, noteworthy, keywords)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(session_id) DO NOTHING""",
            (sid, datetime.now().isoformat(timespec="seconds"), str(digest_file),
             int(res["noteworthy"]), ",".join(res["keywords"])),
        )
    # Per-run scratch for `report` (the reason is not stored in the state DB).
    (WORK_DIR / f"{sid}.meta.json").write_text(json.dumps(
        {"title": meta["title"] or "(untitled)", "reason": res["reason"],
         "summary": res["summary"], "noteworthy": res["noteworthy"], "file": str(digest_file)},
        ensure_ascii=False))
    transcript_path(sid).unlink(missing_ok=True)
    result_file.unlink(missing_ok=True)
    log(f"commit {sid} noteworthy={res['noteworthy']} -> {digest_file.name}")
    print(f"OK {sid} noteworthy={'yes' if res['noteworthy'] else 'no'}")
    return 0


# --- report -----------------------------------------------------------------

def cmd_report(_args) -> int:
    if not RUN_FILE.exists():
        print("[SILENT]")
        return 0
    ids = json.loads(RUN_FILE.read_text())["ids"]
    done, failed = [], []
    for sid in ids:
        meta_file = WORK_DIR / f"{sid}.meta.json"
        (done if meta_file.exists() else failed).append(sid)
    notes = [json.loads((WORK_DIR / f"{sid}.meta.json").read_text()) for sid in done]
    notes = [n for n in notes if n["noteworthy"]]
    log(f"report: {len(done)} digested, {len(notes)} noteworthy, {len(failed)} failed"
        + (f" ({', '.join(failed)})" if failed else ""))

    if not notes and not failed:
        print("[SILENT]")
        return 0

    out = []
    if notes:
        out.append(f"🌅 **Daily session digest — {len(notes)} noteworthy** "
                   f"(of {len(done)} digested)\n")
        for n in notes[:PING_MAX_ITEMS]:
            summary = " ".join(n["summary"].split())
            if len(summary) > PING_SUMMARY_CHARS:
                summary = summary[:PING_SUMMARY_CHARS].rsplit(" ", 1)[0] + "…"
            out.append(f"• **{n['title']}**\n  _{n['reason']}_\n  {summary}\n")
        if len(notes) > PING_MAX_ITEMS:
            out.append(f"…and {len(notes) - PING_MAX_ITEMS} more\n")
        files = sorted({Path(n["file"]).name for n in notes})
        out.append("→ " + ", ".join(f"`knowledge/session-digests/{f}`" for f in files))
    if failed:
        out.append(f"\n⚠️ {len(failed)} session(s) failed, will retry next run: "
                   + ", ".join(f"`{s}`" for s in failed))
    print("\n".join(out).strip())
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("pending").set_defaults(fn=cmd_pending)
    c = sub.add_parser("commit")
    c.add_argument("sid")
    c.add_argument("result_file")
    c.set_defaults(fn=cmd_commit)
    sub.add_parser("report").set_defaults(fn=cmd_report)
    t = sub.add_parser("transcript")
    t.add_argument("sid")
    t.set_defaults(fn=cmd_transcript)
    args = p.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
