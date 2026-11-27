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
- [TST-05] The test that forbids any Substack publish call (and Notes) stays green.
- [TST-06] Tests use temporary directories, never the real `/data` volume.

## Security and secrets
- [SEC-01] No secrets in code, logs, tests, fixtures or commit history.
- [SEC-02] Secrets come from env vars or Docker secrets (`*_FILE`) only.
- [SEC-03] Never log secret values, including in exception messages or repr output.
- [SEC-04] Nothing reaches Substack, even as a draft, without Approve from Telegram. The agent never calls Substack's publish endpoint; publishing is done by Stuart in the Substack editor.
- [SEC-05] The agent never calls Substack's publish endpoint. It creates and updates drafts only. Publishing is done by Stuart in the Substack editor.
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
- [CON-01] State mutated from worker threads (asyncio.to_thread) is guarded by a lock so overlapping calls cannot interleave (from PR #22, 2026-11-18)
- [ERR-17] When a library error is translated into a typed error with "from None" (to keep secrets out of tracebacks), log the original error type and message first so the cause is not lost (from PR #22, 2026-11-18)
- [ERR-18] Every call into a third-party library from a service is wrapped so any exception type becomes the service's typed error, which callers alert on (from PR #22, 2026-11-18)
- [CON-02] Swap shared state in one assignment after building the new value; never clear-then-set where another thread can read the gap (from PR #22, 2026-11-18)
- [OPS-01] A blocking call run in a worker thread cannot be cancelled: the container stop_grace_period must exceed its timeout (from PR #22, 2026-11-18)
- [ERR-19] Startup helpers that run before a long-lived loop handle their own recoverable errors, so a fault there cannot stop the loop from starting (from PR #24, 2026-11-18)
- [ERR-20] Once an irreversible action has succeeded, every follow-up step (notifying, cleanup) is guarded so a failure there is logged and alerted with the result, never raised past the action (from PR #25, 2026-11-18)
- [IDEM-01] After an irreversible external action, remember in memory that it happened, so a failed bookkeeping write cannot let a retry repeat it (from PR #25, 2026-11-18)
- [ERR-21] Cleanup run inside an except block is guarded on its own (logged and alerted), so a cleanup failure never replaces the original error that is being reported (from PR #26, 2026-11-18)
- [CON-03] A cancelled await on asyncio.to_thread does not stop the thread: code that creates something in a thread records the result even on cancellation, so cleanup can undo it (from PR #26, 2026-11-18)
- [ERR-22] Cleanup that must release several resources guards each release on its own, so one failure never skips the others (from PR #26, 2026-11-19)
- [CON-04] A value reported after a write to shared state (such as a count) is computed inside the same lock acquisition as the write, not read back afterwards (from PR #27, 2026-11-19)
- [CON-05] A decision based on shared state and the write that depends on it (e.g. read the claimed day, then set a flag) happen under one lock acquisition (from PR #27, 2026-11-19)
- [OPS-02] Shell scripts run under `set -e` guard every command whose failure the script should report itself (`cmd || fallback`), so a tool error reaches the script's own failure message instead of a raw abort (from PR #30, 2026-11-19)
- [OPS-03] A bounded wait loop checks its condition one last time at the deadline, so the final interval's result is never discarded (from PR #30, 2026-11-19)
- [DB-01] A start-up check that usually finds nothing to do (such as a migration) uses a plain read and takes the write lock only when a change is needed, then rechecks inside the transaction (from PR #31, 2026-11-19)
- [DB-02] Rebuilding an AUTOINCREMENT table restores its `sqlite_sequence` counter even when the table is empty (delete then insert, not update) (from PR #31, 2026-11-19)
- [ERR-23] In a multi-step operation every statement, reads included, runs through the step runner so a failure names the real step (from PR #31, 2026-11-19)
- [SEC-16] SQL values are bound as parameters, never interpolated into the statement text (from PR #31, 2026-11-19)
- [IDEM-02] When an external create call may have succeeded but its reply cannot be read, the error says the resource may exist, and the raw reply is logged (truncated), so a retry is not blind (from PR #32, 2026-11-19)
- [SEC-17] URL validators reject embedded credentials by checking for any userinfo (`@` in the netloc), not just a username, because a password can exist without one (from PR #33, 2026-11-19)
- [SEC-18] Host blocklist checks normalise the host first (lower case, trailing dot stripped) so a DNS-equivalent spelling cannot slip past (from PR #33, 2026-11-19)
- [PY-10] Validators that parse a URL touch every component they rely on (including `.port`) inside the `try`, so malformed input becomes the documented error at load time (from PR #33, 2026-11-19)
- [MEM-01] In-memory per-key bookkeeping (sets and dicts of draft ids) is removed as soon as the durable store records the outcome, so a long-running process does not grow without bound (from PR #38, 2026-11-21)
- [SEC-19] Code that fetches a user-supplied URL resolves the host and refuses any non-public address (loopback, private, link-local) before every request, including each redirect hop, and connects to the vetted address rather than resolving again (from PR #41, 2026-11-21)
- [SEC-20] Content-type checks compare the parsed media type exactly, not a string prefix (from PR #41, 2026-11-21)
- [ERR-24] Blocking calls with no native timeout (DNS lookups) run in a worker and are bounded by the remaining overall deadline, with a cap on stuck workers so a stalling host cannot leak threads (from PR #41, 2026-11-21)
- [GEN-09] A default that exists in config is imported from there, not repeated as a second literal in the module that uses it (from PR #41, 2026-11-21)
- [GEN-10] Handler patterns that must not overlap are defined together from one set of constants, never derived by slicing another pattern, and a test proves they partition the inputs (from PR #42, 2026-11-21)
- [ERR-25] Failure paths shared by several features take the feature name, so logs and alerts say which one failed (from PR #42, 2026-11-21)
- [MEM-02] Bounded in-memory caches never evict an entry that has work in flight or the entry just added, and work that finds its entry gone says so instead of returning silently (from PR #42, 2026-11-21)
- [DOC-01] Numbered workflow instructions put each verification step before the action it gates, including the exceptional path (such as raising a PR at a round limit) (from PR #44, 2026-11-27)
- [TST-09] Test helpers that locate content in a file (a heading, a section) assert with a message naming what is missing, instead of letting a bare `ValueError` surface (from PR #44, 2026-11-27)
