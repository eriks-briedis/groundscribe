#!/usr/bin/env bash
#
# Everything groundscribe needs to be usable, in one command.
#
#   scripts/dev.sh              # local only, on 127.0.0.1
#   scripts/dev.sh --lan        # reachable from the LAN
#   HOST=0.0.0.0 scripts/dev.sh # the same thing, said the long way
#
# Three processes, because the system is three processes: an API that queues
# work, a worker that does it, and the web app. Running fewer looks like it
# works right up until a command queues a job nobody drains.
#
# This is a developer convenience, not a deployment. Deployment — images,
# compose services, packaging — is phase 14, and compose.yaml stays an
# intentional placeholder until then.
#
# Environment:
#   HOST       interface to bind (default 127.0.0.1; 0.0.0.0 for the LAN)
#   API_PORT   the FastAPI port (default 8000)
#   WEB_PORT   the Vite port (default 5173)
#   POLL       seconds between worker passes (default 2)
set -euo pipefail

# Job control, so every service starts in its own process group and can be
# stopped with its whole tree. Without it, killing `npm` leaves the node it
# spawned holding the port, and the next run fails to bind.
set -m

# `--lan` before anything else, because "how do I expose this?" should be
# answerable from `--help` rather than from the source of an environment
# variable. Loopback stays the default: a script that put the pipeline on the
# network by default would be the wrong way round.
LAN=""
for argument in "$@"; do
  case "$argument" in
    --lan) LAN=1 ;;
    -h | --help)
      sed -n '2,25p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "unknown option: $argument (try --lan, or --help)" >&2
      exit 2
      ;;
  esac
done

HOST="${HOST:-127.0.0.1}"
[[ -n "$LAN" ]] && HOST=0.0.0.0
API_PORT="${API_PORT:-8000}"
WEB_PORT="${WEB_PORT:-5173}"
POLL="${POLL:-2}"

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

pids=()

stop() {
  trap - EXIT INT TERM
  for pid in "${pids[@]:-}"; do
    # Negative pid: the process group, so uvicorn's and vite's children go too.
    kill -- "-${pid}" 2>/dev/null || true
  done
  wait 2>/dev/null || true
  echo
  echo "stopped."
}
trap stop EXIT INT TERM

# The API refuses to start without a password, which is the point; rather than
# fail a person's first run, write one and say what it is. Anything already in
# the file or the environment is left alone.
if [[ -z "${GROUNDSCRIBE_PASSWORD:-}" ]] && ! grep -qs '^GROUNDSCRIBE_PASSWORD=' .env; then
  generated="$(head -c 18 /dev/urandom | base64 | tr -d '/+=' | cut -c1-24)"
  printf 'GROUNDSCRIBE_PASSWORD=%s\n' "$generated" >> .env
  echo "→ wrote a password to .env: ${generated}"
  echo "  (change it there; it is the only credential this installation has)"
fi

echo "→ migrating the database"
uv run alembic upgrade head >/dev/null

if [[ ! -d frontend/node_modules ]]; then
  echo "→ installing frontend dependencies"
  (cd frontend && npm install --no-audit --no-fund >/dev/null)
fi

echo "→ api"
uv run uvicorn --factory groundscribe.api.asgi:served_app --host "$HOST" --port "$API_PORT" &
pids+=("$!")

# The CLI's worker drains the queue and exits — deliberately, so a crash halfway
# through a batch keeps what the finished jobs did. A long-lived worker polls on
# top of that rather than instead of it, which is what this loop is. Silent when
# there was nothing to do, so the log stays readable.
echo "→ worker (polling every ${POLL}s)"
while true; do
  uv run writer worker run 2>&1 | grep -v '^ran 0 job(s)$' || true
  sleep "$POLL"
done &
pids+=("$!")

echo "→ web"
(cd frontend && npm run dev -- --host "$HOST" --port "$WEB_PORT") &
pids+=("$!")

cat <<BANNER

  groundscribe is up.

    web   http://${HOST}:${WEB_PORT}
    api   http://${HOST}:${API_PORT}   (proxied at /api, so it need not be exposed)

BANNER

if [[ "$HOST" != "0.0.0.0" ]]; then
  echo "  Bound to ${HOST}: this machine only. Run with --lan to serve the network."
  echo
fi

if [[ "$HOST" == "0.0.0.0" ]]; then
  echo "  On the LAN, reachable at:"
  hostname -I 2>/dev/null | tr ' ' '\n' | grep -E '^[0-9]' | while read -r address; do
    echo "    http://${address}:${WEB_PORT}"
  done
  echo
  echo "  The password is in .env. It crosses the network in the clear —"
  echo "  this is plain HTTP — so treat the network itself as the boundary."
  echo
fi

echo "  Ctrl-C stops all three."
echo

# Wait for whichever service exits first, then let the trap take the rest down:
# a half-running stack is worse than a stopped one, because it looks fine.
wait -n
echo
echo "a service exited — shutting the rest down"
