# CLAUDE.md – Sir Humblebrag III (substack-agent)

## What this is
Dockerised Python agent on `andor`. Daily at 08:30 Europe/London it writes one
parody LinkedIn-style Substack post from a seed in `/data/seed.md`, sends it to
Stuart on Telegram (Approve / Redo / Bin) and publishes only on Approve.
Jira project: SA (stuartmunn.atlassian.net). One Jira story = one branch = one PR.

## Related docs
- `CODING_STANDARDS.md` – living coding rules; self-review against it before every PR.
- `docs/runbook.md` – operating the agent on `andor`.

## Stack
- Python 3.12, pyproject.toml, pytest, ruff
- python-telegram-bot (long polling, no inbound ports)
- anthropic SDK (model from CLAUDE_MODEL env var)
- python-substack (pinned) behind `src/humblebrag/publisher.py`
- SQLite + seed.md on the `/data` volume
- Docker + docker compose

## Hard rules
- Never commit secrets. Config comes from env or Docker secrets (`*_FILE`).
- Never log secret values.
- Substack publish must always pass `send=False`. A test enforces this.
- Nothing publishes without Approve from Telegram.
- Every failure alerts Stuart on Telegram via `alert()`. Never fail silently.
- Generated content: obvious satire, no real named people or companies,
  nothing defamatory, no foul language, English.

## Working mode
- Work autonomously. Don't stop to ask unless genuinely blocked
  (missing credentials, a contradiction in the story). Write down assumptions
  in the PR description under "Assumptions".
- Stay within the story's scope. Note anything else found under
  "Follow-ups" in the PR.

## Git workflow
1. Branch from up-to-date `main`: `SA-<n>-<short-slug>` (e.g. `SA-19-seed-parser`).
2. Commit with `git commit -m "SA-<n>: <message>"`. Small, logical commits.
3. Before opening a PR:
   - `ruff check .` and `ruff format --check .` clean
   - `pytest` green
   - Self-review the diff against every item in `CODING_STANDARDS.md`; fix breaches.
4. Open the PR with `gh pr create` using `.github/pull_request_template.md`.
   Title: `SA-<n>: <story title>`.

## Jira status
Claude Code keeps the story's status current (use the Atlassian tools):
- **In Progress** when you start the story (before creating the branch).
- **In Review** when you raise the PR.
- **Done** once the PR is merged.

## PR-Agent review loop
PR-Agent is installed on the repo and reviews automatically when a PR is raised.
1. After raising the PR, wait 3–5 minutes, then poll the PR comments
   (`gh pr view <n> --comments`) until PR-Agent's review appears.
   If nothing after 15 minutes, comment on the PR and stop.
2. For every finding, do one of:
   - Fix it, or
   - Mitigate it (explain how), or
   - Decide it doesn't apply: reply on the comment with a short reason.
3. Push fixes, then comment exactly `/review` (nothing else in the comment)
   to trigger a fresh review. Wait and poll as in step 1.
4. Repeat until PR-Agent's latest review has no unresolved valid findings.
5. Add each valid finding as a new rule in `CODING_STANDARDS.md`
   (in the same PR) unless an existing rule covers it.
6. After 5 review rounds without a clean review, stop, comment a summary on the
   PR, and leave it for Stuart.

## Merge
- Merge only when: CI green, PR-Agent clean (as above), no merge conflicts.
- Squash merge with `gh pr merge --squash --delete-branch`.
- Then pull `main` before starting the next story.
