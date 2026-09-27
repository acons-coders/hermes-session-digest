#!/usr/bin/env python3
"""
Extract a summarizer-ready view of one Hermes session.

Pipeline (single forward pass over messages, in display order):

  1. Skip rows whose role is in SKIP_ROLES (session_meta — internal book-keeping).
  2. Skip rows whose content matches a known NOISE pattern (we know certain
     rows are pure overhead).
  3. Collapse tool-related rows (role='tool' and assistant rows whose only
     payload is tool_calls) to a single placeholder line. Consecutive runs of
     placeholders are emitted as one line with a (xN) suffix.
  4. Re-label runtime injections that arrive as role='user' (delegation
     callbacks, cron notices, etc.) by their content prefix.
  5. Treat any row whose content contains the COMPACTION_MARKER as a
     context-compaction summary. When the first such row is seen, the buffer
     so far is discarded and replaced by the summary's content. Any later
     summary similarly replaces the buffer. After the LAST compaction marker,
     normal user/assistant text and tool placeholders continue to accumulate.
     This is the "only the tail after the last summary survives" rule.
  6. Dedupe by (timestamp, role, content). For compaction rows the role is
     normalised to 'compaction' before the key is built, so a single summary
     mirrored as role=user/role=tool/role=assistant only survives once.

Output goes to stdout. Redirect to a file when running:

    python3 extract_session_text.py <session_id> > /tmp/foo.txt

The output is a plain-text transcript: section markers (--- user ---,
--- assistant ---, --- system (...) ---, --- context compaction ---), the
message body, blank line, repeat. Tool placeholders are bare lines.

Result for a short session: full conversation as you'd expect.
Result for a long session: the last compaction summary (covering all
prior context) + everything after it. The summarizer gets the durable
record of what happened, not the verbatim blow-by-blow.
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys

# --- Constants -----------------------------------------------------------

# Tool-related rows collapse to this single line.
TOOL_PLACEHOLDER = ">>> Tool call <<<"

# Roles we treat as "real conversation" (subject to re-label + dedupe).
TEXT_ROLES = {"user", "assistant"}

# Roles we always skip silently (no marker, no body).
SKIP_ROLES = {"session_meta"}

# Runtime/system injections arrive as role='user' but start with a
# distinctive bracket prefix. We re-label them so the summarizer
# doesn't treat delegation callbacks or cron notices as user speech.
# Order matters: more specific patterns first.
SYSTEM_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^\[ASYNC DELEGATION"), "system (delegation result)"),
    (re.compile(r"^\[IMPORTANT: Background process"), "system (background process)"),
    (re.compile(r"^\[IMPORTANT: You are running as a scheduled cron job"), "system (cron job)"),
    (re.compile(r'^\[IMPORTANT: The user has invoked the "'), "system (skill invocation)"),
    (re.compile(r"^\[OUT-OF-BAND USER MESSAGE"), "system (out-of-band user message)"),
    (re.compile(r"^\[The user sent an image but"), "system (image fetch failed)"),
    (re.compile(r"^Gateway message origin"), "system (gateway metadata)"),
    (re.compile(r"^\[(SYSTEM|System)( note)?:"), "system"),
]

# Substring marker for a context-compaction summary. The summary body
# itself is the message content (typically tens of KB of structured
# markdown) and starts with the literal "[CONTEXT COMPACTION" line.
COMPACTION_MARKER = "[CONTEXT COMPACTION"


# --- Helpers -------------------------------------------------------------

class Buffer:
    """A mutable string buffer with line-level append + truncate operations.

    `lines` holds the *raw emitted lines* (already including section
    markers and trailing blank lines). Truncating by a line prefix means:
    drop every line whose index is < prefix_index, then prepend a notice.
    """

    def __init__(self) -> None:
        self.lines: list[str] = []

    def append_lines(self, new: list[str]) -> None:
        self.lines.extend(new)

    def truncate_to(self, replacement_lines: list[str]) -> None:
        """Discard everything and replace with these lines."""
        self.lines = list(replacement_lines)

    def render(self) -> str:
        return "".join(self.lines)


def _normalise_content(content: str | None) -> str:
    """Strip trailing whitespace; collapse internal newlines-only lines."""
    return (content or "").rstrip()


# --- Main ----------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Extract summarizer-ready transcript from one Hermes session.",
    )
    parser.add_argument("session_id", help="Hermes session id (e.g. 20260926_113503_85b3fba1)")
    parser.add_argument(
        "--db",
        default="/home/hermes/.hermes/state.db",
        help="Path to state.db (default: %(default)s)",
    )
    args = parser.parse_args()

    con = sqlite3.connect(args.db)
    cur = con.cursor()

    cur.execute(
        "SELECT title, source, message_count FROM sessions WHERE id = ?",
        (args.session_id,),
    )
    meta = cur.fetchone()
    if meta is None:
        print(f"Session not found: {args.session_id}", file=sys.stderr)
        return 2

    title, source, message_count = meta
    header = [
        f"# session_id: {args.session_id}\n",
        f"# title:      {title or '(untitled)'}\n",
        f"# source:     {source}\n",
        f"# msg_count:  {message_count}\n",
        "# filter:     user/assistant text + system re-labels + last compaction summary\n",
        "#\n",
    ]

    # Pull rows in display order. NULL display_order sorts as 0 in SQLite.
    cur.execute(
        """
        SELECT timestamp, COALESCE(display_order, 0), role, content, tool_calls
          FROM messages
         WHERE session_id = ?
         ORDER BY timestamp ASC, COALESCE(display_order, 0) ASC
        """,
        (args.session_id,),
    )

    buf = Buffer()
    seen: set[tuple[float, str, str]] = set()
    pending_tool_count = 0  # tool placeholders waiting to flush

    kept = 0
    dropped_dup = 0
    skipped_role = 0
    collapsed_tools = 0
    collapsed_tool_runs = 0
    reclassified_system = 0
    compaction_count = 0  # how many times we reset the buffer

    def classify_user_as_system(content: str) -> str | None:
        for pat, label in SYSTEM_PATTERNS:
            if pat.match(content):
                return label
        return None

    def flush_tools() -> None:
        """Emit any pending tool placeholders, with run-compression."""
        nonlocal pending_tool_count, collapsed_tool_runs
        if pending_tool_count == 0:
            return
        if pending_tool_count == 1:
            buf.append_lines([f"{TOOL_PLACEHOLDER}\n"])
        else:
            buf.append_lines([f"{TOOL_PLACEHOLDER}  (x{pending_tool_count})\n"])
            collapsed_tool_runs += 1
        pending_tool_count = 0

    for ts, _order, role, content, _tool_calls in cur:
        if role in SKIP_ROLES:
            skipped_role += 1
            continue

        c = _normalise_content(content)

        # ---------------------------------------------------------------
        # Compaction summaries: regardless of role, the content is a
        # handoff. The buffer is reset to this single message body.
        # ---------------------------------------------------------------
        if COMPACTION_MARKER in c:
            key = (ts, "compaction", c)
            if key in seen:
                dropped_dup += 1
                continue
            seen.add(key)
            # Drop any pending tool placeholders — they belong to the
            # discarded pre-summary section.
            pending_tool_count = 0
            summary_lines = [
                "--- context compaction ---\n",
                f"{c}\n",
                "\n",
            ]
            buf.truncate_to(summary_lines)
            compaction_count += 1
            kept += 1
            continue

        # ---------------------------------------------------------------
        # Empty assistant rows (only payload was tool_calls): placeholder.
        # ---------------------------------------------------------------
        if not c and role in TEXT_ROLES:
            if role == "assistant":
                collapsed_tools += 1
                pending_tool_count += 1
            continue

        # ---------------------------------------------------------------
        # Real text rows (user / assistant, possibly system-injected).
        # ---------------------------------------------------------------
        if role in TEXT_ROLES:
            # Re-label runtime injections.
            sys_label = classify_user_as_system(c)
            effective_role = sys_label or role

            # Dedupe — for system-relabelled rows use 'system' in the key
            # so they collapse with same content even if the original role
            # differs.
            key = (ts, effective_role, c) if sys_label else (ts, role, c)
            if key in seen:
                dropped_dup += 1
                continue
            seen.add(key)

            flush_tools()
            buf.append_lines([f"--- {effective_role} ---\n", f"{c}\n", "\n"])
            kept += 1
            if sys_label:
                reclassified_system += 1
            continue

        # ---------------------------------------------------------------
        # Anything else (role='tool' and unknown): tool placeholder.
        # ---------------------------------------------------------------
        collapsed_tools += 1
        pending_tool_count += 1

    flush_tools()

    sys.stdout.write("".join(header))
    sys.stdout.write(buf.render())
    sys.stdout.write(
        f"# stats: kept={kept}  collapsed_tools={collapsed_tools}  "
        f"tool_runs={collapsed_tool_runs}  dropped_dup={dropped_dup}  "
        f"skipped_role={skipped_role}  reclassified_system={reclassified_system}  "
        f"compactions={compaction_count}\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
