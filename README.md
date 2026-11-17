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

## Environment variables
Secrets may be supplied as `<NAME>_FILE` (path to a file, e.g. a Docker
secret) instead of directly. Never commit `.env` or `secrets/`.

| Variable | Purpose |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Telegram bot token (secret) |
| `TELEGRAM_CHAT_ID` | The only chat allowed to approve, redo or bin |
| `ANTHROPIC_API_KEY` | Anthropic API key (secret) |
| `CLAUDE_MODEL` | Anthropic model used to write posts |
| `SUBSTACK_EMAIL` | Substack account email |
| `SUBSTACK_PASSWORD` | Substack account password (secret) |
| `SUBSTACK_PUBLICATION_URL` | Publication to post to |
| `DATA_DIR` | Data volume holding SQLite and `seed.md` (default `/data`) |
