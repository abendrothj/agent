#!/usr/bin/env bash
set -euo pipefail

REPO_PATH="${REPO_PATH:-$PWD}"
DEPLOY_REF="${DEPLOY_REF:-main}"
ALLOW_DIRTY="${ALLOW_DIRTY:-false}"
SERVICES="${SERVICES:-vault api shadow watchdog sandbox-agent cloudflared}"

cd "$REPO_PATH"

if [[ ! -d .git ]]; then
  echo "Repo path is not a git repository: $REPO_PATH" >&2
  exit 1
fi

if [[ "$ALLOW_DIRTY" != "true" ]] && [[ -n "$(git status --porcelain)" ]]; then
  echo "Working tree is dirty. Commit/stash changes first, or set ALLOW_DIRTY=true." >&2
  exit 1
fi

echo "Fetching latest from origin..."
git fetch --all --prune

echo "Checking out ref: $DEPLOY_REF"
git checkout "$DEPLOY_REF"

CURRENT_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
if [[ "$CURRENT_BRANCH" != "HEAD" ]]; then
  git pull --ff-only origin "$CURRENT_BRANCH"
fi

echo "Deploying Pi brain services: $SERVICES"
docker compose --env-file deployments/pi/.env -f deployments/pi/docker-compose.yml up -d --build $SERVICES

echo "Deployment status"
docker compose --env-file deployments/pi/.env -f deployments/pi/docker-compose.yml ps

echo "Deployed revision:"
git rev-parse HEAD
git log -1 --pretty=oneline
