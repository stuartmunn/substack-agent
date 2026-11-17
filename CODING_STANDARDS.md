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
