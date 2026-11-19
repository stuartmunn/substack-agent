#!/usr/bin/env bash
# Deploy Sir Humblebrag III on this machine: pull main, rebuild, wait for the
# container to be healthy, then run the smoke test (a Telegram message saying
# "Sir Humblebrag reporting for duty"). Exits non-zero at the first problem.
#
#   ./scripts/deploy.sh
#
# Optional environment:
#   DEPLOY_HEALTH_TIMEOUT  seconds to wait for "healthy" (default 120)
#   DEPLOY_POLL_SECONDS    seconds between health checks (default 5)
set -euo pipefail

SERVICE=humblebrag
HEALTH_TIMEOUT="${DEPLOY_HEALTH_TIMEOUT:-120}"
POLL_SECONDS="${DEPLOY_POLL_SECONDS:-5}"

cd "$(dirname "${BASH_SOURCE[0]}")/.."

step() { printf '==> %s\n' "$*"; }
fail() { printf 'DEPLOY FAILED: %s\n' "$*" >&2; exit 1; }

step "Updating the code (fast-forward only)"
git pull --ff-only || fail "git pull --ff-only did not succeed"

step "Building and starting ${SERVICE}"
docker compose up -d --build || fail "docker compose up failed"

step "Waiting up to ${HEALTH_TIMEOUT}s for ${SERVICE} to be healthy"
waited=0
status=unknown
while [ "$waited" -lt "$HEALTH_TIMEOUT" ]; do
    status=unknown
    container="$(docker compose ps -q "$SERVICE" 2>/dev/null || true)"
    if [ -n "$container" ]; then
        status="$(docker inspect --format '{{.State.Health.Status}}' "$container" 2>/dev/null || echo unknown)"
    fi
    [ "$status" = healthy ] && break
    sleep "$POLL_SECONDS"
    waited=$((waited + POLL_SECONDS))
done
[ "$status" = healthy ] || fail "${SERVICE} is '${status}' after ${HEALTH_TIMEOUT}s; see: docker compose logs ${SERVICE}"

step "Smoke test: sending the reporting-for-duty message to Telegram"
docker compose exec -T "$SERVICE" python -m humblebrag.smoke \
    || fail "smoke test failed; see: docker compose logs ${SERVICE}"

step "Deployed. Check Telegram for: Sir Humblebrag reporting for duty"
