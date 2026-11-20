# Sir Humblebrag III (substack-agent)

## Purpose
A Dockerised Python agent that writes one parody LinkedIn-style Substack post
a day from a seed in `/data/seed.md`, sends it to Stuart on Telegram
(Approve / Redo / Bin). Approve creates a Substack draft and sends Stuart the
editor link. The agent never calls Substack's publish endpoint. It creates and updates drafts only. Publishing is done by Stuart in the Substack editor.

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
3. `./scripts/deploy.sh` (pulls, runs `docker compose up -d --build`, waits for
   healthy, then sends the "Sir Humblebrag reporting for duty" smoke-test
   message to Telegram; see `docs/runbook.md`). The container runs as a non-root user,
   exposes no ports, restarts unless stopped, and keeps state in the
   `humblebrag-data` volume mounted at `/data`.

## Telegram bot
The bot uses long polling (no inbound port) and answers only the chat in
`TELEGRAM_CHAT_ID`. Messages and button presses from any other chat are
ignored and logged. `/start` and `/help` list the commands.

## Seeds (`/data/seed.md`)
A plain checklist you can edit by hand. One seed per line:

    - [ ] An unused seed
    - [x] A used seed (used 2026-11-17)
    - [ ] A seed the agent invented (generated)

Any other line (blank lines, `# comments`, notes) is left exactly as you
wrote it. The agent only rewrites the line it is marking used, and writes the
file atomically (temp file, then rename). A missing file is created empty.

Seeds are used oldest-first. When a draft is made, its seed is *reserved* in
`/data/seed-reservations.json` (so a restart cannot hand it out twice) but
`seed.md` is left alone. Approving the draft ticks the seed with today's date.
Binning the draft just releases the reservation, so the seed is the first
unused line again. Seeds are identified by their text, so avoid duplicate lines.

## Alerts
Every component reports failures through `humblebrag.alerts.alert(event, detail)`.
It sends a plain-text Telegram message starting with ⚠️ and the failed step,
with secrets redacted and the text capped at Telegram's limit. It never
raises: if Telegram is unreachable, the alert is logged at ERROR instead.

## Logs and health
Logs are one JSON object per line on stdout (`timestamp`, `level`, `logger`,
`event`); registered secret values are redacted. The agent touches
`/tmp/humblebrag.heartbeat` every 30s from its event loop, and the Docker
`HEALTHCHECK` fails if the file is missing or older than 60s, so a dead or
hung loop is marked unhealthy within 2 minutes (`docker ps` shows status).

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
| `SUBSTACK_PUBLICATION_URL` | yes | Publication address: `https://<subdomain>.substack.com` or a custom domain (not a profile URL like `https://substack.com/@name`) |
| `SUBSTACK_COOKIE` | yes | Substack session cookie (secret) |
| `RUN_TIME` | no | Daily run time, `HH:MM` (default `08:30`) |
| `TZ` | no | IANA time zone (default `Europe/London`) |
