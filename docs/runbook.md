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
   text, no quotes. Each file must be readable by the container's non-root
   user (see [Refresh the Substack cookie](#refresh-the-substack-cookie) for
   why and how):
   - `secrets/telegram_bot_token`: the token from BotFather
   - `secrets/anthropic_api_key`: the Anthropic API key
   - `secrets/substack_cookie`: see [Refresh the Substack cookie](#refresh-the-substack-cookie)
3. Copy the template and fill in the non-secret values:
   `cp .env.example .env`, then set `TELEGRAM_CHAT_ID`, `CLAUDE_MODEL` and
   `SUBSTACK_PUBLICATION_URL`. `RUN_TIME` (default `08:30`) and `TZ` (default
   `Europe/London`) are optional. Compose refuses to start if a required value
   is unset and names the missing one.

   `SUBSTACK_PUBLICATION_URL` is the **publication address**:
   `https://<subdomain>.substack.com`, or your custom domain (for example
   `https://example.com`). It is **not** your profile URL
   (`https://substack.com/@name`). With a profile URL the agent refuses to
   start: the container exits with `Configuration error: SUBSTACK_PUBLICATION_URL
   must be https://<subdomain>.substack.com or a custom domain ...` in
   `docker compose logs humblebrag`. A well-formed address that Substack cannot
   find (a typo, or a publication your account does not own) starts fine but
   alerts `Substack says the publication is unavailable: SUBSTACK_PUBLICATION_URL
   is wrong or unreachable ... or the cookie is logged out`. Check the URL
   first, then the cookie.
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
4. Write it to the secret file with no extra text, and make it readable:
   `printf '%s' '<the value>' > secrets/substack_cookie && chmod 644 secrets/substack_cookie`

   Use `644`, not `600`. Compose mounts the file into the container with the
   same owner and permissions it has on `andor`, and the agent runs as a
   non-root user (uid `10001`), not as you. A file that only you can read
   (`600`) is unreadable to that user, and the agent stops at startup with
   `SUBSTACK_COOKIE_FILE points to a file that cannot be read
   (PermissionError)`. The same applies to the other files in `secrets/`.

   A `644` file is readable by every account on `andor`, so also lock the
   folder: `chmod 700 secrets`. Other accounts then cannot reach the files at
   all, while the container still reads them (Docker mounts them as root, so
   the folder's permissions do not affect the container).
5. Restart so the new value is read: `docker compose up -d --force-recreate`
   (then `docker compose exec -T humblebrag python -m humblebrag.smoke` if you
   want proof the bot is back)
6. Check the logs for a clean startup (no `Substack session check failed`).
   The agent checks the session at startup and then once a day.

If you suspect the cookie leaked, log out of all sessions in Substack
(which invalidates it) before creating a new one.

## The agent never publishes

The agent only ever creates (and updates) Substack **drafts**. When you tap
**Approve** in Telegram it puts the post into Substack as a draft and sends you
the editor link. You read it there and press Publish yourself. Nothing is
published, and nothing reaches Substack even as a draft, without your Approve.

The agent used to try to publish too, but Substack's publish call answered
`403` while draft creation worked. Why is still unknown; it is being looked
into in [SA-42](https://stuartmunn.atlassian.net/browse/SA-42). Until that is
settled, publishing stays a manual step in the editor.

## Edit `seed.md` by hand

For a quick add, use Telegram instead: `/seed <text>` appends a seed and
`/seeds` lists the unused ones. To drop an unused seed, send `/unseed <number>`
using its number from `/seeds`; the bot replies with the seed's text, so you
can spot a wrong pick (add it again with `/seed`). It refuses a seed that a
pending draft has reserved: Bin or Approve that draft first.

To edit the file itself, copy it out of the container, edit it, and copy it back:

    docker compose cp humblebrag:/data/seed.md ./seed.md
    $EDITOR seed.md
    docker compose cp ./seed.md humblebrag:/data/seed.md

Rules (the agent never rewrites lines other than the one it ticks):

- One seed per line: `- [ ] An unused seed`, `- [x] A used seed (used 2026-11-17)`,
  `- [x] A dropped seed (skipped 2026-11-20)`, `- [ ] An invented seed (generated)`.
  Seeds are used oldest-first. `(skipped ...)` is what `/unseed` writes; the
  seed is kept for history, is never picked or listed, and still counts as
  known when the agent invents a new seed. `(generated)` is kept if it was
  there, in front of the date.
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

## Restore from a backup

Use this if `humblebrag.db` or `seed.md` is lost or damaged. The agent backs
both up every day after 03:00 into the data volume, one folder per local day:
`/data/backups/<date>/` (for example `/data/backups/2026-11-19/`), keeping the
newest 14. Each folder holds `humblebrag.db` and, if `seed.md` existed that
day, `seed.md`.

Restoring puts the files back as they were at that backup. Anything made since
is lost: drafts, seeds added with `/seed`, and seeds used or skipped.

1. Stop the agent so nothing writes while you restore:
   `docker compose stop humblebrag`
2. List the available days and pick one:
   `docker compose run --rm --no-deps --entrypoint ls humblebrag -1 /data/backups`
3. Restore it (set `DAY` to the folder name). This keeps the current files as
   `*.before-restore` in `/data` so you can undo it, clears any stale SQLite
   journal files, then copies the backup in. It runs as the container user, so
   the restored files have the right owner:

       docker compose run --rm --no-deps -e DAY=2026-11-19 --entrypoint sh humblebrag -c '
         set -e; cd /data
         test -f "backups/$DAY/humblebrag.db"
         [ ! -f humblebrag.db ] || cp humblebrag.db humblebrag.db.before-restore
         [ ! -f seed.md ] || cp seed.md seed.md.before-restore
         rm -f humblebrag.db-journal humblebrag.db-wal humblebrag.db-shm
         cp "backups/$DAY/humblebrag.db" humblebrag.db
         [ ! -f "backups/$DAY/seed.md" ] || cp "backups/$DAY/seed.md" seed.md
         ls -l /data'

   If that day's folder has no `seed.md`, the current `seed.md` is left as it
   is. To restore only one of the two files, delete the other `cp` line.
4. Start the agent: `docker compose up -d humblebrag`, then check
   `docker compose ps` shows `healthy` and the logs have a clean startup.
5. In Telegram, send `/seeds` and check the list is what you expect from that
   day. Then check pending drafts:
   - A draft that was pending at the backup but has since been approved or
     binned shows as pending again. If you already approved it, find the
     Substack draft first, because approving again may create a second one.
   - `seed-reservations.json` is not part of the backup. If tapping **Approve**
     on a restored draft alerts that no seed is reserved, **Bin** it instead.

To undo a restore, stop the agent and copy the `*.before-restore` files back
over `humblebrag.db` and `seed.md` the same way. The backups themselves are
never changed by a restore.

The volume's full name is the repo folder name plus `_humblebrag-data` (for
example `substack-agent_humblebrag-data`; see `docker volume ls`). When you want
to rehearse a restore, copy that volume to a new one and run the steps in a
separate compose project (`docker compose -p <name> ...`) rather than on the
live volume. (Compose warns that the copied volume "was not created by Docker
Compose"; that is harmless.) Never start the agent itself in a rehearsal: it would poll
Telegram with the live bot token.
