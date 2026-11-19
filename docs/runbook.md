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
4. Deploy: `./scripts/deploy.sh`. It builds and starts the container, waits
   for it to be `healthy`, then runs the [smoke test](#smoke-test). (The
   script starts with `git pull --ff-only`, which does nothing on a fresh clone.)
5. Check Telegram for the message `Sir Humblebrag reporting for duty`. That
   message means the bot token, chat id and network all work. You can also
   look at `docker compose ps` (`healthy`) and
   `docker compose logs --tail 50 humblebrag` (`Configuration loaded`,
   `Telegram bot polling`).
6. In Telegram, send `/help` to the bot. Only the chat in `TELEGRAM_CHAT_ID`
   gets an answer; any other chat is ignored and logged.
7. Add a first seed with `/seed <text>` (or see
   [Edit `seed.md` by hand](#edit-seedmd-by-hand)). With no unused seeds the
   agent invents one on the next run. `/now` makes a draft straight away.

Never commit `.env` or `secrets/`.

## Update the image

The image is built on `andor` from the repo (there is no registry), so an
update is one command, run in the repo checkout:

    ./scripts/deploy.sh

It does, in order, and stops at the first failure with `DEPLOY FAILED: ...`:

1. `git pull --ff-only` (a pull that cannot fast-forward stops the deploy
   before anything is rebuilt).
2. `docker compose up -d --build`. Compose rebuilds the image and recreates the
   container. It waits up to 45 seconds for a Substack check in flight, then
   stops it.
3. Waits up to 2 minutes (`DEPLOY_HEALTH_TIMEOUT`, in seconds) for the
   container to report `healthy`.
4. Runs the [smoke test](#smoke-test).

The manual equivalent is `git pull --ff-only && docker compose up -d --build`.

The `/data` volume survives updates: seeds, drafts and the schedule are kept.
A restart never hands anything to Substack (that needs Approve), and the daily
run is claimed once per day, so restarting around 08:30 cannot double-run it.

To roll back a bad version, check out the previous commit and rebuild directly
(not with the script, whose `git pull` fails on a detached HEAD):
`git checkout <previous commit> && docker compose up -d --build`, then run the
[smoke test](#smoke-test). Return to `main` with `git checkout main` before the
next deploy.

## Smoke test

`docker compose exec -T humblebrag python -m humblebrag.smoke` sends one
Telegram message, `Sir Humblebrag reporting for duty`, to the configured chat,
using the same token and chat id as the running agent. Exit code 0 means it was
sent. It does nothing else: no run, no draft, nothing reaches Substack.
`./scripts/deploy.sh` runs it after every deploy; you can also run it by hand at
any time to check the bot is reachable.

If it fails, the cause is in `docker compose logs humblebrag` (the log line
`Smoke test failed`). Common causes: a wrong `TELEGRAM_CHAT_ID`, a revoked
bot token in `secrets/telegram_bot_token`, or no outbound network from `andor`.
It cannot alert you on Telegram, because Telegram is what failed.

## Refresh the Substack cookie

You get this alert when the session has expired:
`Substack session expired, refresh cookie`. Until it is fixed, Approve cannot
hand the draft to Substack (it stays pending, nothing is lost).

1. In a browser, log in to Substack as the publication's author.
2. Open the developer tools, go to Application (Chrome) or Storage (Firefox),
   Cookies, `https://substack.com`.
3. Copy the **value** of the `substack.sid` cookie. (Pasting the whole
   `name=value; name2=value2` string from a request header also works.)
4. Write it to the secret file with no extra text, and keep it private:
   `printf '%s' '<the value>' > secrets/substack_cookie && chmod 600 secrets/substack_cookie`
5. Restart so the new value is read: `docker compose up -d --force-recreate`
   (then `docker compose exec -T humblebrag python -m humblebrag.smoke` if you
   want proof the bot is back)
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

Approve makes one Substack call to create a draft and saves its ID at once.
The agent never calls Substack's publish endpoint: it creates and updates
drafts only, and you publish from the editor link it sends. If a step after the
draft was created fails, you are alerted with the step name, the Substack draft
ID and the editor link, e.g.
`Substack draft <id> exists (<link>) for draft <n>, but step '...' failed: ...`.

1. Open the link (or Substack, Posts, Drafts) and check the draft is there.
2. The local draft may still be pending with its buttons active. Tapping
   **Approve** again updates the same Substack draft (it never creates a
   second one) and completes the handover.
3. Or publish the Substack draft yourself, then tap **Bin** on the pending
   local draft. Bin does not touch Substack, so delete any draft you do not
   want there.
4. If the alert said the seed was not marked used, mark it by hand in
   `seed.md`.
5. If the failure was an expired session, fix that first (see
   [Refresh the Substack cookie](#refresh-the-substack-cookie)).

If creating the draft itself failed, nothing exists on Substack: the local
draft stays pending, and you can tap **Approve** again, **Redo** or **Bin**.
