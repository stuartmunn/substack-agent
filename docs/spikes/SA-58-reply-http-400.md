# SA-58 Spike: why `/reply` got HTTP 400 from the Claude API

Date: 2026-11-27. Spike: the code changes are only the diagnostics (AC1, AC2, AC6),
the empty-text guard (AC8) and the fixture (AC7).

## Summary

- **The link is not the cause.** Every Claude call the agent makes fails with
  HTTP 400 on `CLAUDE_MODEL=claude-sonnet-5-5`: the reply call, the guard call,
  and (by the same code path) the daily post, seed and redo calls.
- **Root cause:** SA-56 added `thinking={"type": "disabled"}` to every request
  (`THINKING_DISABLED`, `claude_client.py`). This model rejects it:

  > invalid_request_error: To turn thinking off on this model, send
  > "thinking": {"type": "between_tools"} instead of {"type": "disabled"}.

- SA-56's tests mock the transport, so they asserted the parameter was sent and
  could not see the API refuse it. Nothing logged the API's reason, which is why
  the alert showed only "HTTP 400".
- The story's assumption that "other `/reply` links have worked since SA-56" did
  not hold in my replays: every call failed, whatever the text. I could not check
  production logs from here, so whether earlier links ran before SA-56 was
  deployed is unverified.
- **Urgent:** if the same build runs on `andor`, the 08:30 daily run fails too.

## AC3: replaying the link through the SA-52 fetcher

Replayed live on 2026-11-27 with `fetch_target`:

| URL | Status | Type | Extracted text |
|---|---|---|---|
| `.../note/c-398035566?r=9gykro` | ok | note | "Why can't people understand that working from home can help people do their best work, having the right environment can make an enormous difference." (25 words) |
| `.../note/c-398035566` | ok | note | identical |

- The text is not empty and holds no JavaScript notice. The page's
  `og:description` carries the whole sentence.
- `?r=` changes nothing (both pages are HTTP 200, 91 KB, same `og:description`).
- So the fetcher, the empty-text suspect and the `?r=` suspect are all ruled out.
- Which call got the 400: the **reply** call (`Flow.REPLY`, `ReplyGenerator`). It
  is the first Claude call, so the guard call is never reached for this link. The
  guard call fails identically when run alone (`flow=guard`, request
  `req_011ChU5CrobYBmMAXDwGS9MB`).

Replay of the full reply flow with both of Stuart's steers and no steer: all
three returned the 400 above (requests `req_011ChU5BwhzwB3ztAxZ4nLFN`,
`req_011ChU5By56kcy8W8LnFRFNK`, `req_011ChU5ByzAqPq3XcN1t5cmQ`).

## AC4: first call or the 2x max_tokens retry?

The **first call**. The new log line carries `max_tokens=1024`, which is the
reply flow's own limit (the retry would be 2048), and the request is refused
before any reply exists, so there is no `max_tokens` stop to retry on. Client
errors are not retried by the SDK either.

## Which `thinking` settings each model accepts

Probed directly with a one-word prompt:

| Model | `thinking` omitted | `disabled` | `between_tools` |
|---|---|---|---|
| `claude-sonnet-5-5` | OK | **400** | OK |
| `claude-opus-5-5` | OK | **400** ("Use thinking.type.adaptive and output_config.effort") | 400 (not supported) |

The accepted "off" value differs by model, so no single explicit value works for
every `CLAUDE_MODEL`. Omitting the parameter works on both and returns a plain
text block.

## Proposed fix story

**SA-59: Stop sending `thinking` on Claude calls.**

1. Remove `THINKING_DISABLED` and the `thinking=` argument from
   `ClaudeClient._request`, and flip the SA-56 test that asserts it to assert the
   request has no `thinking` key.
2. Check that no reply block can be a thinking block: `_reply_text` already
   keeps only `text` blocks, so a thinking block would be ignored, but add a test
   with one in the response.
3. Run one real call per flow in a smoke check (`smoke.py`) after deploy, since
   mocked transports cannot see an API-side rejection.
4. If thinking tokens ever show up in `usage.output_tokens`, tune `max_tokens`
   then (SA-55 background), not before.

Redeploy to `andor` as soon as it merges: the daily run is down until then.

## What this spike changed

- **AC1/AC2:** `ClaudeAPIError` now reads the `error.type` and `error.message`
  from the API's response body. They go into one ERROR log line
  (`flow`, `max_tokens`, `status`, `request`, `type`, `message`) and into the
  exception text, so the Telegram alert shows them. The message is whitespace
  collapsed and capped at 200 characters, and the type too. Only the API's
  `error` object is read, never the request, so our code never logs prompt text.
  The alert text also passes through the redactor. If the API echoes part of a
  prompt inside its message, up to 200 characters of it could show; Anthropic
  400s describe the parameter, not the content, as seen here.
- **AC6:** unit tests cover the logged detail, the 200 character cap, a missing
  body, and that prompt text and the API key are absent from our logs.
- **AC7:** `tests/fixtures/substack/note_working_from_home.html` is a saved copy of
  the note page, replayed with and without `?r=` in `tests/test_fetcher.py`. It
  holds the author's public handle as served by Substack; it is test data and is
  never sent anywhere.
- **AC8:** `/reply` treats an OK fetch whose text is empty or whitespace as
  "couldn't read": no Claude call, and Stuart gets the existing paste-fallback
  message. The fetcher already returns COULDNT_READ for empty text; this guards
  the service in case any other source hands it blank text.

## Follow-ups

- The SDK's own `anthropic._base_client` logger prints the full request body at
  DEBUG. `configure_logging` runs at INFO and holds HTTP loggers at WARNING, so
  this is off in production; keep it that way (do not run with DEBUG on `andor`).
- `CLAUDE_MODEL` can be set to a model with different parameter rules. SA-59's
  smoke check covers a model change if it runs on startup or in the runbook's
  "change model" steps.
