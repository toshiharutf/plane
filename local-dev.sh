#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

export PLANE_LOCAL_UID="${PLANE_LOCAL_UID:-$(id -u)}"
export PLANE_LOCAL_GID="${PLANE_LOCAL_GID:-$(id -g)}"

if [ "$#" -eq 0 ]; then
  set -- up
fi

docker compose \
  -f docker-compose-local.yml \
  -f docker-compose-local-frontend.yml \
  "$@"
