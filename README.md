# hermes-session-digest

`digest.py` powers the daily `6d986a839a80` cron job ("Daily Session Digest"). Pure Python 3 stdlib, no LLM calls.

The cron agent only dispatches one summarizer subagent per session. Everything mechanical is done here: picking sessions, building transcripts, parsing replies, rendering entries, updating the state DB and building the Telegram text.

## Subcommands

```bash
python3 digest.py pending
# Picks ended sessions not yet in ~/.hermes/digest_state.db (last 14 days, >=5 messages,
# max 20 per run; excludes this job's own cron sessions and their subagents).
# Writes one transcript per session to ~/.hermes/cache/session-digest/<sid>.txt (mode 600),
# clears leftovers of the previous run, prints {"count", "sessions": [{id, title, transcript}]}.

python3 digest.py commit <sid> <result_file>
# Parses the subagent reply (SUMMARY / KEYWORDS / NOTEWORTHY / REASON), appends the entry to
# ~/.hermes/knowledge/session-digests/<end-date>.md, records the session, deletes temp files.
# Prints "OK ..." or "ERROR: ..." (exit 1). Idempotent: an already recorded session is a no-op,
# an entry already present in the file is not written twice.

python3 digest.py report
# Telegram text for this run: noteworthy sessions + failed ones (retried next run), or [SILENT].

python3 digest.py transcript <sid>
# Print one transcript to stdout (debugging).
```

## Transcript rules

- Only `active = 1` rows, in id order. After in-place compaction that is what the model last saw: the first turns, then the compaction summary standing in for the archived middle, then the tail.
- Rows starting with `[CONTEXT COMPACTION` / `[PRIOR CONTEXT` are labelled `--- context compaction ---`.
- Runtime injections stored as `role='user'` (delegation callbacks, background-process notices, cron / skill preambles, out-of-band messages, gateway metadata, `[SYSTEM: ...]`) are labelled `--- system (...) ---`.
- Tool results and tool-call-only assistant turns become `>>> Tool call <<<`, and runs of them `>>> Tool call <<<  (xN)`.
- Back-to-back identical rows are written once.
- The last line is `=== END OF TRANSCRIPT ===`. The subagent is told to keep reading until it sees it (`read_file` pages at ~100k chars).
- No size cap: after the last compaction a transcript is bounded by the source session's context window. `pending` logs a WARNING above 400k chars (~100k tokens).

## Why no time cursor

The previous version selected sessions with `last_activity_at > MAX(digested_at)`. That lost sessions still open at 05:00 and everything beyond the per-run cap. Now the only filter is "ended and not yet in `digest_state.db`", within a 14-day lookback.

## Files

- State: `~/.hermes/digest_state.db` (table `digested`)
- Output: `~/.hermes/knowledge/session-digests/YYYY-MM-DD.md` (grouped by end date)
- Log: `~/.hermes/logs/session_digest.log`
- Scratch: `~/.hermes/cache/session-digest/` (transcripts, subagent results, `run.json`)

The cron prompt (in `~/.hermes/cron/jobs.json`) hardcodes the absolute path to `digest.py`. If the project moves, update the prompt.

## License

Internal tooling for the Hermes Alphazero installation. Not for redistribution.
