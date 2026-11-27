# SA-55 Spike: max_tokens on Claude calls and the `/reply` cut-off

Date: 2026-11-26. Findings only; no code changed.

## Summary

- The failing call was **`/reply`** (`ReplyGenerator.generate`), capped at
  `REPLY_MAX_TOKENS = 1024` (`src/humblebrag/reply_generator.py:23`).
- Logs, 2026-11-26 13:31 UTC: attempt 1 returned 81 words (limit 80) and was
  rejected as malformed. Attempt 2, the retry, ran about 7.5 s and hit
  `max_tokens`, so `ClaudeClient._reply_text` raised `ClaudeResponseError`. The alert
  reached Telegram.
- A valid reply is at most about 150 words, roughly 200-300 tokens. Hitting 1024
  means the model produced about 3-4x the expected output, so **the cap is probably
  not too low for this flow**. It is more likely that the output was runaway or
  non-reply text (see Hypotheses). Raising the cap alone would hide this.
- Nothing logs `stop_reason` or `usage.output_tokens`, so AC4 is only partly
  answerable from logs. This is the main gap to fix.

## 1. Call sites (AC1-3)

Every call goes through one method, `ClaudeClient.complete()`
(`src/humblebrag/claude_client.py:65`). It sends `model`, `max_tokens`, `system`
and `messages` only. **No `thinking` parameter is set anywhere**, and there is no
env var for max_tokens.

| Flow | Call site | max_tokens | Set where | Model |
|---|---|---|---|---|
| Daily run | `PostGenerator.generate` (`generator.py:195`) | 4096 | derived: `max(4096, POST_MAX_WORDS*3 + 512)`; 600 words gives 2312, so the 4096 floor applies. Constants in `generator.py:15-17`. | `CLAUDE_MODEL` |
| `/now` | same as the daily run (shared pipeline) | 4096 | same | same |
| Redo (with steer) | `generate_checked` -> `PostGenerator.generate` | 4096 | same | same |
| Safety guard, post | `SafetyGuard.check` (`guard.py:185`) | 512 | hard-coded `CLASSIFIER_MAX_TOKENS` (`guard.py:17`) | same |
| Safety guard, reply | `SafetyGuard.check_reply` (`guard.py:201`) | 512 | same constant | same |
| `/reply` | `ReplyGenerator.generate` (`reply_generator.py:184`) | **1024** | hard-coded `REPLY_MAX_TOKENS` | same |
| Generated seed | `SeedProvider._generate_seed` (`seed_provider.py:67`) | 128 | hard-coded `SEED_MAX_TOKENS` | same |
| Promo Note summary | **no separate call**: `note_teaser` is a field in the post JSON | n/a (inside 4096) | n/a | n/a |
| Image prompt | **no Claude call**: fills the `prompts/image_prompt.md` template | n/a | n/a | n/a |

Model: `CLAUDE_MODEL` env var (currently `claude-sonnet-5-5`). Extended thinking is
**off by default in our code** because the parameter is never sent. I did not verify
the API's default for this model; if it applies any default thinking, those tokens
count against `max_tokens` (see Hypotheses).

Telegram vs scheduled (AC3): the scheduled run, `/now` and Redo share one path
(`PostGenerator`, one cap). `/reply` has its **own** generator and cap (1024).
Seeds and the guard are separate again. All flows share a single `ClaudeClient`
(`wiring.py:83`). The only setting they share is the model.

## 2. Log evidence (AC4)

`docker logs substack-agent-humblebrag-1` (andor), 185 lines retained. This is the
only `max_tokens` failure in the retained logs. The alert was
`reply: /reply failed: ClaudeResponseError: Claude's reply was cut off at max_tokens`.
Traceback: `reply.py:251` -> `wiring.py:95 generate_reply` -> `guard.py:255` ->
`reply_generator.py:184` -> `claude_client.py:106`.

Neither `stop_reason` nor the output token count is logged. httpx logs only
`POST /v1/messages 200 OK`. The count therefore cannot be read from logs; it is
inferred as at least 1024.

## 3. Output size per flow (AC5)

Estimated from the prompts and validators. **Not measured against the API**, since
that needs the production key and spends tokens. The follow-up should log real
`usage.output_tokens` first.

| Flow | Typical | Worst valid case | Cap | Headroom |
|---|---|---|---|---|
| Post (JSON, 250-600 words + title, subtitle, teaser, scene) | ~600-900 | ~1,100 (600 words is about 800 tokens, plus JSON) | 4096 | ample |
| Guard verdict (`{"verdict","reason"}`) | ~40 | ~120 | 512 | ample |
| Seed (one line) | ~25 | ~60 | 128 | thin if the model adds preamble |
| `/reply` post-length (80-150 words + 3 emoji bullets + 3-6 hashtags) | ~200 | ~300 | 1024 | about 3x |
| `/reply` comment-length (20-80 words) | ~80 | ~150 | 1024 | about 6x |

## 4. Hypotheses for the 1024 overflow

1. **Runaway or off-format output on the retry.** The retry prompt includes the
   previous problem ("it has 81 words, expected 20 to 80"). The model may have
   produced several drafts, word-count commentary or a long explanation. Likely.
2. **Thinking tokens** counted against the cap, if the model thinks by default.
   Unverified. To check: log `usage` and the content block types.
3. **Cap too low for a legitimate reply.** Unlikely given the 150-word limit.

Which of these applies cannot be settled without the response body. Today the
rejected text is discarded.

## 5. Recommendations (AC6)

1. **Observe before changing the number.** In `ClaudeClient.complete()`, log at
   INFO for every call: call label, `stop_reason`, `usage.input_tokens`,
   `usage.output_tokens`, `max_tokens`. On a `max_tokens` stop, include the
   output token count and the first ~200 chars of text in the error and log (no
   secrets). This answers AC4 directly next time.
2. **One config point, per-flow defaults.** Add env vars with defaults, validated
   in `config.py` like `POST_MAX_WORDS`:
   - `CLAUDE_MAX_TOKENS_POST` (default: keep derived value, floor 4096)
   - `CLAUDE_MAX_TOKENS_REPLY` (default 2048)
   - `CLAUDE_MAX_TOKENS_GUARD` (default 512)
   - `CLAUDE_MAX_TOKENS_SEED` (default 256)
   - `CLAUDE_MAX_TOKENS_CEILING` (default 8192): hard upper bound applied to every
     call, so no retry or setting can exceed it. This is the cost ceiling.

   Per flow rather than one global value, because the flows differ by 10x or more
   (a 128-token seed vs a 4096-token post). A single value would over-allow the
   cheap calls. Raising the cap does not cost anything unless the model uses it.
3. **Handle a `max_tokens` stop explicitly, in this order:**
   1. Retry once with the cap doubled, bounded by the ceiling. Do not count the
      attempt against the malformed-output retries.
   2. If it is cut off again, raise `ClaudeResponseError` as now. The existing
      `alert()` path tells Stuart on Telegram. Add the flow name and the output
      token count to the message.
   3. Do not "continue" a truncated reply (assistant prefill): the output is JSON or
      a validated short reply, so partial text is unusable.
4. **Reply flow:** do not append the previous failure text to the retry prompt for
   word-count failures without also restating "reply with the reply text only"
   (to check once the logs show the content). Add a one-line test fixture.
5. **Explicitly send `thinking: {"type": "disabled"}`** (or document the choice) so the
   behaviour does not depend on API defaults.

## 6. Follow-on story (draft, AC7)

**Title:** Log Claude usage and make max_tokens configurable, with a safe retry on cut-off

**As Stuart**, I want Claude token limits to be configurable and visible, so that
replies are not cut off and I can see why when they are.

Acceptance criteria:
1. `ClaudeClient.complete()` logs call label, `stop_reason`, input/output tokens
   and `max_tokens` for every call.
2. Per-flow max_tokens come from env vars (names above) with validated defaults,
   documented in README and `.env.example`, and passed through `docker-compose.yml`.
3. A global ceiling is enforced on every call.
4. On `stop_reason == "max_tokens"`: one retry at 2x (capped), then a Telegram
   alert naming the flow and token count. Never silent.
5. Thinking is explicitly disabled or its budget accounted for.
6. Tests cover: the cap is sent, the retry doubles and is clamped, a second
   cut-off alerts, and the ceiling holds.
7. Reproduce the `/reply` overflow with the new logging and record the cause in
   the PR under "Assumptions".

## Follow-ups noticed

- Only 185 log lines are retained; consider a log-rotation or retention check (the
  runbook may already cover it).
- Alert text for `/reply` does not say which attempt (first or retry) failed.
