# Runbook: operating Sir Humblebrag III on `andor`

Everything is run from the repo checkout on `andor` (the directory holding
`docker-compose.yml`). The service is called `humblebrag`; its state lives in
the `humblebrag-data` Docker volume, mounted at `/data` in the container.

Quick health check: `docker compose ps` should show `humblebrag` as `healthy`
(the heartbeat check fails within 2 minutes if the event loop dies or hangs).

## First deploy

1. Get the code and be on `main`:
   `git clone https://github.com/stuartmunn/substack-agent.git && cd substack-agent`
2. Put each secret in its own file under `secrets/` (git-ignored). No trailing
   text, no quotes:
   - `secrets/telegram_bot_token`: the token from BotFather
   - `secrets/anthropic_api_key`: the Anthropic API key
   - `secrets/substack_cookie`: see [Refresh the Substack cookie](#refresh-the-substack-cookie)
3. Copy the template and fill in the non-secret values:
   `cp .env.example .env`, then set `TELEGRAM_CHAT_ID`, `CLAUDE_MODEL` and
   `SUBSTACK_PUBLICATION_URL`. `RUN_TIME` (default `08:30`) and `TZ` (default
   `Europe/London`) are optional. Compose refuses to start if a required value
   is unset and names the missing one.
4. Build and start: `docker compose up -d --build`
5. Check it came up: `docker compose ps` (wait for `healthy`) and
   `docker compose logs --tail 50 humblebrag`. You should see
   `Configuration loaded` and `Telegram bot polling`.
6. In Telegram, send `/help` to the bot. Only the chat in `TELEGRAM_CHAT_ID`
   gets an answer; any other chat is ignored and logged.
7. Add a first seed with `/seed <text>` (or see
   [Edit `seed.md` by hand](#edit-seedmd-by-hand)). With no unused seeds the
   agent invents one on the next run. `/now` makes a draft straight away.

Never commit `.env` or `secrets/`.

## Update the image

The image is built on `andor` from the repo (there is no registry).

1. `git checkout main && git pull`
2. `docker compose up -d --build`. Compose rebuilds the image and recreates the
   container. It waits up to 45 seconds for a Substack check in flight, then
   stops it.
3. `docker compose ps` shows `healthy` again, and
   `docker compose logs --tail 50 humblebrag` shows `Telegram bot polling`.

The `/data` volume survives updates: seeds, drafts and the schedule are kept.
A restart never publishes anything (publishing needs Approve), and the daily
run is claimed once per day, so restarting around 08:30 cannot double-run it.

If the new version misbehaves, `git checkout <previous commit>` and run
`docker compose up -d --build` again.

## Refresh the Substack cookie

You get this alert when the session has expired:
`Substack session expired, refresh cookie`. Until it is fixed, Approve cannot
publish (the draft stays pending, nothing is lost).

1. In a browser, log in to Substack as the publication's author.
2. Open the developer tools, go to Application (Chrome) or Storage (Firefox),
   Cookies, `https://substack.com`.
3. Copy the **value** of the `substack.sid` cookie. (Pasting the whole
   `name=value; name2=value2` string from a request header also works.)
4. Write it to the secret file with no extra text, and keep it private:
   `printf '%s' '<the value>' > secrets/substack_cookie && chmod 600 secrets/substack_cookie`
5. Restart so the new value is read: `docker compose up -d --force-recreate`
6. Check the logs for a clean startup (no `Substack session check failed`).
   The agent checks the session at startup and then once a day.

If you suspect the cookie leaked, log out of all sessions in Substack
(which invalidates it) before creating a new one.

## Edit `seed.md` by hand

For a quick add, use Telegram instead: `/seed <text>` appends a seed and
`/seeds` lists the unused ones.

To edit the file itself, copy it out of the container, edit it, and copy it back:

    docker compose cp humblebrag:/data/seed.md ./seed.md
    $EDITOR seed.md
    docker compose cp ./seed.md humblebrag:/data/seed.md

Rules (the agent never rewrites lines other than the one it ticks):

- One seed per line: `- [ ] An unused seed`, `- [x] A used seed (used 2026-11-17)`,
  `- [ ] An invented seed (generated)`. Seeds are used oldest-first.
- Any other line (blank lines, `# comments`) is left exactly as you wrote it.
- Seeds are identified by their text, so avoid duplicate lines.
- **Don't change or delete the text of a seed that has a pending draft.** The
  draft's seed is reserved by its text; Approve fails with an alert if the
  line is gone. Bin the draft first, then edit.
- A missing file is recreated empty. A file that is not valid UTF-8 is an
  alerted error, not a silent reset.

Remove the local copy afterwards (`rm seed.md`) so it cannot go stale.

## Read the logs

Logs are one JSON object per line on stdout (`timestamp`, `level`, `logger`,
`event`); secret values are redacted.

- Follow live: `docker compose logs -f --tail 100 humblebrag`
- Last hour: `docker compose logs --since 1h humblebrag`
- Only problems: `docker compose logs --since 24h humblebrag | grep -E '"level": ?"(ERROR|WARNING)"'`
- Alerts that Telegram could not deliver are logged at ERROR as
  `Alert not delivered`. Alerts that did go out appear as `Alert sent`.
- Health: `docker compose ps` (and `docker inspect --format '{{json .State.Health}}' <container>` for the last check).
- A crash on startup (bad or missing config) exits non-zero and names the
  missing variables, never their values: `docker compose logs humblebrag`.

## Recover an orphaned Substack draft

Publishing is three Substack calls: create a draft, prepare it, publish it
(always with email sending off). If a later call fails, the earlier draft is
left on Substack. You are alerted with one of:

- `... Nothing was published; orphaned draft ID: <id>`: the draft exists but
  is **not** live.
- `... the post may be live. Check Substack before retrying. Draft ID: <id>`:
  the publish call was made, so it may be live.

The log line is `Orphaned Substack draft left by a failed publish: <id>`
(or `Draft <id> may be published or orphaned`).

1. In Substack, open the publication dashboard, Posts, and look in both
   **Published** and **Drafts** for the post (match the title, or the ID in
   the draft's URL).
2. If it is **live**: leave it. In Telegram, tap **Bin** on the pending draft
   so it cannot be published a second time. (If the alert said the draft was
   not marked published, do **not** tap Approve again.)
3. If it is only a **draft**: delete it in Substack.
4. Then decide what to do with the local draft, which is still pending with
   its buttons active:
   - tap **Approve** to publish again (this creates a fresh Substack draft;
     the orphan from step 3 is why it must be deleted first, or you will
     have two), or
   - tap **Redo** / **Bin** as normal. Bin returns the seed to the front of
     the queue.
5. If the failure was an expired session, fix that first (see
   [Refresh the Substack cookie](#refresh-the-substack-cookie)).

Nothing is ever emailed to subscribers: publish always uses `send=False`.
