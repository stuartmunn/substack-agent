# Sir Humblebrag III (substack-agent)

## Purpose
A Dockerised Python agent that writes one parody LinkedIn-style Substack post
a day from a seed in `/data/seed.md`, sends it to Stuart on Telegram
(Approve / Redo / Bin) and publishes only on Approve. Substack publishing
always uses `send=False`, so no email is ever sent to subscribers.

## Local run steps
1. Use Python 3.12 and create a virtualenv: `python3.12 -m venv .venv && . .venv/bin/activate`
2. Install with dev tools: `pip install -e ".[dev]"`
3. Copy the env template and fill it in: `cp .env.example .env`
4. Run checks: `ruff check . && ruff format --check . && pytest`

## Run with Docker
1. Put each secret in its own file under `secrets/` (git-ignored):
   `telegram_bot_token`, `anthropic_api_key`, `substack_cookie`.
2. Put the non-secret values (`TELEGRAM_CHAT_ID`, `CLAUDE_MODEL`,
   `SUBSTACK_PUBLICATION_URL`, optionally `RUN_TIME`, `TZ`) in `.env`.
3. `docker compose up -d --build`. The container runs as a non-root user,
   exposes no ports, restarts unless stopped, and keeps state in the
   `humblebrag-data` volume mounted at `/data`.

## Environment variables
Any variable can be supplied as `<NAME>_FILE` (path to a file, e.g. a Docker
secret) instead; if both are set the file wins. Never commit `.env` or
`secrets/`. If a required value is missing the app exits non-zero and names
the missing variables (never their values).

| Variable | Required | Purpose |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | yes | Telegram bot token (secret) |
| `TELEGRAM_CHAT_ID` | yes | The only chat allowed to approve, redo or bin (integer) |
| `ANTHROPIC_API_KEY` | yes | Anthropic API key (secret) |
| `CLAUDE_MODEL` | yes | Anthropic model used to write posts |
| `SUBSTACK_PUBLICATION_URL` | yes | Publication to post to |
| `SUBSTACK_COOKIE` | yes | Substack session cookie (secret) |
| `RUN_TIME` | no | Daily run time, `HH:MM` (default `08:30`) |
| `TZ` | no | IANA time zone (default `Europe/London`) |
