#!/usr/bin/env bash
#
# Install groundscribe, in one command.
#
#   scripts/install.sh              # SQLite: three containers, nothing to tune
#   scripts/install.sh --postgres   # add PostgreSQL, for concurrent use
#   scripts/install.sh --no-start   # write the configuration, build nothing
#
# What it does, in order: check Docker is there, write a .env with a password if
# there is not one already, build the images, start the stack, wait for the API
# to answer, and print where to go. Nothing else — this is a wrapper around
# `docker compose`, not a second way to configure the system.
#
# It stops at the first failure rather than continuing. A bootstrap that got
# half-way and said nothing leaves a person debugging a stack that was never
# fully built, which is a worse position than not having run it.
#
# Environment:
#   GROUNDSCRIBE_API_PORT   host port for the API (default 8000)
#   GROUNDSCRIBE_WEB_PORT   host port for the web app (default 3000)
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

POSTGRES=""
START=1

usage() {
  sed -n '3,22p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

for argument in "$@"; do
  case "$argument" in
    --postgres) POSTGRES=1 ;;
    --no-start) START="" ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      echo "unknown option: $argument (try --postgres, --no-start, or --help)" >&2
      exit 2
      ;;
  esac
done

api_port="${GROUNDSCRIBE_API_PORT:-8000}"
web_port="${GROUNDSCRIBE_WEB_PORT:-3000}"

# ---------------------------------------------------------------------------
# What has to be here already
# ---------------------------------------------------------------------------

if ! command -v docker >/dev/null 2>&1; then
  echo "docker is not installed. This installs the whole stack in containers;" >&2
  echo "see https://docs.docker.com/get-docker/ and run this again." >&2
  exit 1
fi

if ! docker compose version >/dev/null 2>&1; then
  echo "'docker compose' is not available (the v1 'docker-compose' script is not" >&2
  echo "enough — this file uses compose-spec features it does not have)." >&2
  exit 1
fi

if ! docker info >/dev/null 2>&1; then
  echo "the Docker daemon is not reachable. Start Docker and run this again." >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# One file, read by two things: the application (for the password) and compose
# itself (for ports, profile and database URL). Keeping it to one file means
# there is one place to look when the stack is configured unexpectedly.
touch .env

if ! grep -qs '^GROUNDSCRIBE_PASSWORD=' .env; then
  generated="$(head -c 18 /dev/urandom | base64 | tr -d '/+=' | cut -c1-24)"
  printf 'GROUNDSCRIBE_PASSWORD=%s\n' "$generated" >>.env
  echo "→ wrote a password to .env: ${generated}"
  echo "  (change it there; it is the only credential this installation has)"
fi

profile_args=()
if [[ -n "$POSTGRES" ]]; then
  profile_args=(--profile postgres)
  if ! grep -qs '^GROUNDSCRIBE_DATABASE_URL=' .env; then
    # The service name, not localhost: this URL is resolved from inside the
    # backend container, where `database` is the other container.
    printf 'GROUNDSCRIBE_DATABASE_URL=%s\n' \
      'postgresql+psycopg://groundscribe:groundscribe@database:5432/groundscribe' >>.env
    echo "→ pointed .env at the PostgreSQL service"
  fi
  # Remembered, so a later bare `docker compose up` still starts the database
  # this installation was configured against.
  grep -qs '^COMPOSE_PROFILES=' .env || printf 'COMPOSE_PROFILES=postgres\n' >>.env
else
  echo "→ using SQLite (run with --postgres for the concurrent stack)"
fi

if [[ -z "$START" ]]; then
  echo
  echo "configuration written. Start it with:"
  echo "  docker compose ${profile_args[*]:-} up -d --build"
  exit 0
fi

# ---------------------------------------------------------------------------
# Build and start
# ---------------------------------------------------------------------------

echo "→ building images (first run takes a few minutes)"
docker compose "${profile_args[@]}" build

echo "→ starting the stack"
docker compose "${profile_args[@]}" up -d

# ---------------------------------------------------------------------------
# Wait for it, rather than claiming it is up
# ---------------------------------------------------------------------------

echo -n "→ waiting for the API"
for _ in $(seq 1 90); do
  if curl -fsS "http://127.0.0.1:${api_port}/health" >/dev/null 2>&1; then
    echo " — up."
    cat <<BANNER

  groundscribe is installed.

    web   http://127.0.0.1:${web_port}
    api   http://127.0.0.1:${api_port}   (proxied at /api, so it need not be exposed)

  The password is in .env. Stop the stack with 'docker compose down';
  add '-v' to that only if you mean to delete every artefact it stored.

BANNER
    exit 0
  fi
  echo -n "."
  sleep 2
done

echo
echo "the API did not answer within three minutes. What it said:" >&2
docker compose "${profile_args[@]}" logs --tail 50 backend >&2
exit 1
