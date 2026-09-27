# hermes-session-digest

Scripts that power the daily `6d986a839a80` cron digest job in this Hermes installation.

## Scripts

### `extract_sessions.py`

Mechanical companion for the digest cron. Three subcommands:

```bash
python3 extract_sessions.py --pending
# JSON to stdout: list of sessions awaiting digest (those ended since the last run).

python3 extract_sessions.py --write-entry="<entry_md>" --date=YYYY-MM-DD
# Append a markdown entry to that date's file at ~/.hermes/knowledge/session-digests/.
# Use '=' syntax for both flags.

python3 extract_sessions.py --record --sid=<sid> --noteworthy=<0|1> --digest-file=<abs_path> --keywords="kw1,kw2"
# Mark the session as digested in ~/.hermes/digest_state.db. Idempotent UPSERT.
```

Pure Python 3.11 stdlib. No external dependencies. No LLM calls.

### `extract_session_text.py`

Per-session transcript filter. Reads `~/.hermes/state.db`, writes a summarizer-friendly plain-text transcript to stdout:

- `--- user ---` / `--- assistant ---` sections for real conversation.
- Runtime injections re-labeled: `--- system (delegation result) ---`, `--- system (background process) ---`, `--- system (cron job) ---`, `--- system (skill invocation) ---`, `--- system (out-of-band user message) ---`, `--- system (image fetch failed) ---`, `--- system (gateway metadata) ---`, generic `--- system ---`.
- Tool calls/results collapsed to `>>> Tool call <<<`. Consecutive runs compressed to `>>> Tool call <<<  (xN)`.
- Context-compaction summaries: when a message containing `[CONTEXT COMPACTION` is seen, the buffer is discarded and replaced with the summary body. The last summary wins; everything after it accumulates.
- Triple-write dedupe (Hermes writes some rows 2-3×; identical `(timestamp, role, content)` rows are dropped).

```bash
python3 extract_session_text.py <session_id> > /tmp/transcript-<sid>.txt
```

Output is a plain-text transcript intended to be read by a fresh-context LLM subagent that produces a summary + keywords.

## Cron integration

The cron job `6d986a839a80` ("Daily Session Digest") invokes these scripts. See `~/.hermes/cron/jobs.json` for the current prompt (which carries the full procedure inline — no skill loader).

## Path convention

The cron prompt hardcodes the absolute path to this project:

```
/home/hermes/.hermes/projects/hermes-session-digest/extract_session_text.py
```

If the project moves, update the cron prompt and re-test before the next 05:00 run.

## License

Internal tooling for the Hermes Alphazero installation. Not for redistribution.
