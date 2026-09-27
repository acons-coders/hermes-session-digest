# Proposal: previous-session context for the summarizer

Status: **proposal, not implemented** (Sep 27 2026).

## Problem

Each session is summarized in isolation. That loses information whenever work on one topic spans more than one session.

The typical case: Nicu starts a topic with the default model, notices it is heading the wrong way, and instead of a long correction he starts a **new session, usually with another model**. Examples from the history:

- "Check guest FTP read access on 10.0.0.6" → "Retest FTP on Proworx 10.0.0.6" (Sep 22)
- "Verify Kimi k3 model availability" → "Switch chat model to kimi-k3" → "Switch Telegram bot model to kimi-k3" (Sep 23)
- "Check if Brave browser is installed" → "Connect Brave Search API with Hermes" → "Check web_search tool availability" (Sep 26/27)

Today these become independent entries. As a result:

1. **The restart itself is invisible.** The digest never says "attempt 1 with model A was abandoned, attempt 2 with model B succeeded". That is useful knowledge for model choice, and it is lost.
2. **Noteworthy lands on the wrong session.** An abandoned first attempt can be flagged because it "ended with an open task". A decision prepared in A and confirmed in B is flagged in B only, or in both.
3. **The second summary lacks context.** A retry often starts with "ok, again: …" or refers to the previous try, and the summarizer can't resolve that.

## Why not merge sessions

`sessions.parent_session_id` does not mean "same topic". On Telegram every new session (`/new` or the idle reset, `end_reason = session_reset`) points to the previous session in the chat. That builds one long thread across unrelated topics (Sep 16 → Sep 27 is a single chain: reboot → pharmacy questionnaire → SSH config → AWS console → …). Merging along that link would glue unrelated topics together. Compaction no longer splits sessions (only 2 `end_reason = compression` cases, both March 2026), and in-session compaction is already handled by the transcript builder.

So the right move is to **give context, not merge**: the summarizer sees the previous session and decides for itself whether this one continues it.

## Proposal

Fully automatic in `digest.py pending`. No new step for the cron model.

1. For each pending session, look up the parent (`parent_session_id`, same `source`).
2. If the parent ended **less than 2 h** before this session started, prepend a block to the transcript:

   ```
   --- previous session (context only, do not summarize) ---
   title:   Check guest FTP read access on 10.0.0.6
   model:   MiniMax-M3   (this session: anthropic/claude-sonnet-4.6)
   ended:   18:40, 4 min before this session started
   summary: <digest summary if already digested, else first user message + last assistant reply, ~500 chars each>
   ```

   With a larger gap the parent is almost always a different topic. Leaving the block out avoids confusing the summarizer.
3. Extend the subagent text with one field:

   ```
   CONTINUES:
   (yes if this session continues or restarts the previous session's topic, else no. If there is no previous session block: no)
   ```

   plus one rule: "Mention the previous session in SUMMARY only if CONTINUES is yes."
4. `digest.py commit` renders `**Continues:** `<parent id>` (restart with <model>)` in the entry when CONTINUES is yes and the model changed, or just `**Continues:** `<parent id>`` otherwise. Optional: a `continues` column in `digest_state.db` for later queries such as "how often was model X abandoned".

## Details

- **Parent and child in the same run.** Often both are pending together, so the parent's digest summary doesn't exist yet when the child's transcript is written. The fallback (first user message + last assistant reply from `state.db`) is enough to recognise "same topic, second try".
- **Cost.** About 300–600 extra tokens per session that has a recent parent. Negligible.
- **Risk: a weak model mixes the topics.** Mitigations: the block is labelled "context only, do not summarize", the rule ties any mention to CONTINUES, and a missing CONTINUES line is treated as `no` rather than as an error, so the old reply format still commits.

## Open points

- Is the 2 h limit right? Check against a few weeks of history (gap distribution for the continuing vs. unrelated pairs) before fixing it.
- Should an abandoned attempt (CONTINUES=yes on the child, model changed) force `noteworthy: no` on the parent? That would need the parent's entry to be rewritten, which `commit` doesn't do today. Proposal: no, keep entries append-only.
