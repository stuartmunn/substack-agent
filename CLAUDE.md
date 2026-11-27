# CLAUDE.md – Sir Humblebrag III (substack-agent)

## What this is
Dockerised Python agent on `andor`. Daily at 08:30 Europe/London it writes one
parody LinkedIn-style Substack post from a seed in `/data/seed.md`, sends it to
Stuart on Telegram (Approve / Redo / Bin). Approve puts it into Substack as a
draft and sends Stuart the editor link; Stuart publishes it himself.
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
- The agent never calls Substack's publish endpoint. It creates and updates drafts only. Publishing is done by Stuart in the Substack editor. A test enforces this.
- Nothing reaches Substack, even as a draft, without Approve from Telegram.
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
   - Complete the "Pre-PR review" below.
4. Open the PR with `gh pr create` using `.github/pull_request_template.md`.
   Title: `SA-<n>: <story title>`.

## Pre-PR review
Before running `gh pr create`:
1. Commit all story work (`SA-<n>: ...`) before each round. `--fix` edits have
   no rewind checkpoints; git on the story branch is the safety net.
2. Run `/code-review medium --fix Follow CODING_STANDARDS.md as well as CLAUDE.md.`
   on the story branch. Always pass `medium` explicitly, otherwise the review
   reuses the last level typed. The review reads CLAUDE.md but not other files
   unless pointed at them, hence the trailing instruction.
3. Wait for the findings before going on. The review runs as a background
   subagent, so do not run `gh pr create` until it has reported.
4. For each finding, do one of: fix it, mark it "no change needed" with a
   reason, or skip it with a reason.
5. After fixing, commit the fixes and rerun the review. Stop once a run has
   nothing left to fix, or after 3 rounds. A run that fails or returns no
   result is not clean: rerun it (it counts as a round), and flag it in the PR
   if it stays that way.
6. Commit any `--fix` edits, then rerun `ruff check .`, `ruff format --check .`
   and `pytest`, and self-review the `--fix` diff against `CODING_STANDARDS.md`:
   the fixes land after the earlier checks and are unverified.
7. If the 3-round limit is hit with a finding still open, raise the PR anyway
   and flag that finding in the PR description.
8. Add a "Pre-PR review" section to the PR description listing each finding
   and its outcome.

Note: only a GitHub PR comment of exactly `/review` triggers PR-Agent. In
Claude Code, `/review` is an alias for `/code-review`, so it is not the same
thing.

## Jira status
Claude Code keeps the story's status current (use the Atlassian tools):
- **In Progress** when you start the story (before creating the branch).
- **In Review** when you raise the PR.
- **Done** once the PR is merged.

## PR-Agent review loop
PR-Agent is installed on the repo and reviews automatically when a PR is raised.
1. After raising the PR, wait 3–5 minutes, then poll the PR comments
   (`gh pr view <n> --comments` or `gh api`) until PR-Agent's review appears.
   If nothing after 15 minutes, post a note on the PR, alert Stuart, and stop.
2. For every finding, do one of:
   - Fix it, or
   - Mitigate it (explain how), or
   - Decide it doesn't apply: reply on the comment with a short reason.
3. Push fixes, then comment exactly `/review` (nothing else in the comment)
   to trigger a fresh review. Wait and poll as in step 1.
4. Repeat until PR-Agent's latest review has no unresolved valid findings.
   "Clean" is a judgement call: write the reasoning on the PR for audit.
5. Add each valid finding as a new rule in `CODING_STANDARDS.md` under
   "Learned from PR-Agent" (in the same PR), as
   `- [ID] rule (from PR #n, YYYY-MM-DD)`, unless an existing rule covers it.
6. After 5 review rounds without a clean review, stop, post a summary on the
   PR, and do not merge. Leave it for Stuart.

## Merge
- Merge only when: CI green, PR-Agent's latest review clean (as above), no merge conflicts.
- Squash merge with `gh pr merge --squash --delete-branch`.
- Then pull `main` before starting the next story.
