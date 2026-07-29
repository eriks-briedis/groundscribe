#!/usr/bin/env bash
#
# What the backend image does when it starts (phase 14).
#
#   entrypoint.sh api      # the FastAPI process
#   entrypoint.sh worker   # the long-lived worker loop
#   entrypoint.sh migrate  # migrations only, then exit
#
# The two services share an image and differ here, which keeps the difference
# between them one word long and visible in compose.yaml.
set -euo pipefail

: "${GROUNDSCRIBE_API_HOST:=0.0.0.0}"
: "${GROUNDSCRIBE_API_PORT:=8000}"
: "${GROUNDSCRIBE_WORKER_POLL:=2}"
: "${GROUNDSCRIBE_WORKER_ID:=worker}"

# The API migrates and the worker waits for it. Two processes racing to run the
# same migration is a deadlock on Postgres and a corrupted file on SQLite, and
# the API is the one a person is waiting on — so it does the work and the worker
# stays out of the way.
migrate() {
  echo "→ migrating the database"
  alembic upgrade head
}

case "${1:-api}" in
api)
  migrate
  exec uvicorn --factory groundscribe.api.asgi:served_app \
    --host "$GROUNDSCRIBE_API_HOST" --port "$GROUNDSCRIBE_API_PORT"
  ;;

worker)
  # `writer worker run` drains the queue once and exits — deliberately, so a
  # crash halfway through a batch keeps what the finished jobs did (plan/09).
  # A long-lived worker polls on top of that rather than instead of it, which is
  # what this loop is. Silent when there was nothing to do, so the log stays
  # readable.
  echo "→ worker ${GROUNDSCRIBE_WORKER_ID} polling every ${GROUNDSCRIBE_WORKER_POLL}s"
  while true; do
    writer worker run --worker-id "$GROUNDSCRIBE_WORKER_ID" 2>&1 |
      grep -v '^ran 0 job(s)$' || true
    sleep "$GROUNDSCRIBE_WORKER_POLL"
  done
  ;;

migrate)
  migrate
  ;;

*)
  # Anything else is run verbatim, so `docker compose run backend writer
  # project metrics` works without the entrypoint needing to know the CLI.
  exec "$@"
  ;;
esac
