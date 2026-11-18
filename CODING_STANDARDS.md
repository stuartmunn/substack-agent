# Coding Standards

Living rules for Sir Humblebrag III. Self-review every diff against this file
before raising a PR. Each rule is one line, checkable, and has a unique ID.

New rules from PR-Agent go in "Learned from PR-Agent" in the form
`- [ID] rule (from PR #n, YYYY-MM-DD)`.

## General
- [GEN-01] One Jira story per branch and PR; no unrelated changes in the diff.
- [GEN-02] No dead code, commented-out code or unused imports.
- [GEN-03] Functions do one thing and fit on a screen; split them otherwise.
- [GEN-04] Names say what a thing is; no single-letter names outside short loops.
- [GEN-05] No magic numbers or strings; use named constants or config.

## Python
- [PY-01] Public functions, methods and classes have type hints on all parameters and returns.
- [PY-02] No bare `except:`; catch the narrowest exception that makes sense.
- [PY-03] Public modules, classes and functions have a one-line docstring.
- [PY-04] Use `pathlib` for paths and `logging` for output; no `print` in `src/`.
- [PY-05] No mutable default arguments.
- [PY-06] Code is Python 3.12 compatible and passes `ruff check .` and `ruff format --check .`.
- [PY-07] Caller-supplied indexes and handles are bounds-checked and raise the documented error type, not `IndexError` (from PR #10, 2026-11-17)
- [PY-08] Parsers treat trailing metadata that contradicts the format (e.g. on the wrong state) as plain text, never silently dropping it (from PR #10, 2026-11-17)

## Testing
- [TST-01] Every acceptance criterion has at least one test that would fail without it.
- [TST-02] No network calls in unit tests; mock Telegram, Anthropic and Substack.
- [TST-03] Tests are deterministic: no reliance on wall-clock time, randomness or test order.
- [TST-04] Every failure path has a test that asserts `alert()` is called.
- [TST-05] The test that enforces `send=False` on Substack publish stays green.
- [TST-06] Tests use temporary directories, never the real `/data` volume.

## Security and secrets
- [SEC-01] No secrets in code, logs, tests, fixtures or commit history.
- [SEC-02] Secrets come from env vars or Docker secrets (`*_FILE`) only.
- [SEC-03] Never log secret values, including in exception messages or repr output.
- [SEC-04] Nothing publishes without Approve from Telegram.
- [SEC-05] Substack publish always passes `send=False`.
- [SEC-06] Only the configured Telegram chat can approve, redo or bin.
- [SEC-07] Generated content is obvious satire: no real named people or companies, nothing defamatory, no foul language, English.
- [SEC-08] Dependencies are pinned in `pyproject.toml`.
- [SEC-09] Entrypoints catch unhandled exceptions and log them through the redacting logger; no raw traceback reaches stderr, because library errors can contain secrets.

## Error handling and alerting
- [ERR-01] Every failure path calls `alert()`; nothing fails silently.
- [ERR-02] Every external call (HTTP, Telegram, Anthropic, Substack) has an explicit timeout.
- [ERR-03] Retries are bounded and back off; no infinite loops.
- [ERR-04] Alerts say what failed and where, without including secret values.
- [ERR-05] Errors are not swallowed: re-raise or handle, never `pass`.

## Docker
- [DKR-01] Base images are pinned to a specific version, not `latest`.
- [DKR-02] The container runs as a non-root user.
- [DKR-03] No inbound ports are exposed; Telegram uses long polling.
- [DKR-04] Persistent state lives only on the `/data` volume.
- [DKR-05] Secrets are passed via Docker secrets or env, never baked into the image.
- [DKR-06] Required compose variables use `${VAR:?message}` so `docker compose up` fails fast when one is unset (from PR #6, 2026-11-17)

## Learned from PR-Agent
<!-- Format: - [ID] rule (from PR #n, YYYY-MM-DD) -->
- [ERR-06] File reads that can fail catch decoding errors as well as `OSError` and turn them into a clear, secret-free error (from PR #5, 2026-11-17)
- [LOG-01] Every process entrypoint (including helper commands like healthchecks) calls `configure_logging()` first, so all output is JSON on stdout (from PR #7, 2026-11-17)
- [ERR-07] Long-running loops catch and log recoverable per-iteration errors instead of letting them kill the process (from PR #7, 2026-11-17)
- [ERR-08] Startup and cleanup of long-lived resources share one `try`/`finally`, and cleanup only stops what actually started (from PR #8, 2026-11-17)
- [ERR-09] Error text sent to logs or alerts unwraps `ExceptionGroup`s to the leaf exceptions, so the real cause is named (from PR #9, 2026-11-17)
- [ERR-10] Functions that write files create the target's parent directory themselves rather than assuming an earlier call did (from PR #10, 2026-11-17)
- [ERR-11] Read-modify-write of shared state files on the data volume holds a file lock with a bounded wait, and fails with a typed error if it times out (from PR #11, 2026-11-17)
- [PY-09] Parsers of hand-edited text files normalise CRLF line endings before matching (from PR #13, 2026-11-17)
- [TST-07] Format checks over fixtures cover every line type (including bullets), not just plain prose lines (from PR #13, 2026-11-17)
- [TST-08] Limits asserted in tests match the limits stated in the prompt or spec exactly, with no extra slack (from PR #13, 2026-11-17)
- [SEC-10] Content filters cover every syntax that can carry the banned thing (inline, reference-style and HTML images), not just the common one (from PR #14, 2026-11-17)
- [GEN-06] Every constraint a prompt tells the model to obey (links, images, length) is also validated in code on the reply (from PR #14, 2026-11-17)
- [GEN-07] Model output budgets (max tokens) derive from configured limits, not a fixed constant (from PR #14, 2026-11-17)
- [GEN-08] Content-filter patterns are tested against a legitimate near-miss as well as the banned forms (from PR #14, 2026-11-17)
- [SEC-11] Safety wordlist tokenisers split on everything that is not a letter, so quotes, apostrophes and punctuation cannot hide a listed word (from PR #15, 2026-11-18)
- [SEC-12] Untrusted text placed inside delimiter tags in a prompt has any copy of the delimiter neutralised first, and a test proves it cannot close the tag (from PR #15, 2026-11-18)
- [ERR-12] Check-then-act sequences on shared state (generate then reserve, read then write) happen under a single lock acquisition, not two (from PR #16, 2026-11-18)
- [SEC-13] Model-generated text written to a line-based file is checked as printable single-line text, rejecting every control character, not just newlines (from PR #16, 2026-11-18)
- [ERR-13] Blocking waits (file locks, sleeps) inside async code are offloaded with `asyncio.to_thread`, so they cannot stall the shared event loop (from PR #17, 2026-11-18)
- [ERR-14] A function that documents a typed error wraps every OSError path it can hit (directory creation, open, write), not just its main operation (from PR #18, 2026-11-18)
- [SEC-14] Handlers that act on stored per-chat state compare the update's chat with the chat that state belongs to, even behind the global chat guard (from PR #20, 2026-11-18)
- [ERR-15] Best-effort cleanup after a completed action (removing buttons, deleting temp messages) catches broadly and logs, and runs after the user-facing confirmation, so it can never hide success (from PR #21, 2026-11-18)
- [ERR-16] Background tasks started at startup are wrapped so an exception is logged and alerted at once; never rely on awaiting the task at shutdown to surface it (from PR #22, 2026-11-18)
- [SEC-15] Parsing a secret of unknown shape must not guess from characters a real value can contain (e.g. base64 "="); match known names or explicit delimiters (from PR #22, 2026-11-18)
