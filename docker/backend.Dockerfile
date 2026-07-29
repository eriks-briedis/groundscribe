# The Python half of the stack: the API and the worker (phase 14).
#
# One image for both. They are the same application — the worker runs the same
# stage handlers the API queues work for — and two images would let one run code
# the other had not been given, which is the class of bug that only appears
# after a partial deploy. They differ by `command:` in compose.yaml and by
# nothing else.
#
# Multi-stage so the shipped layer carries no build tooling: uv, the lockfile
# and the compiler headers stay in the builder, and what runs is a virtualenv
# and the source.

FROM python:3.12-slim AS builder

# uv from its own image rather than pip-installed: the version is pinned by the
# tag, and a bootstrap that resolves its own installer is a dependency nobody
# recorded.
COPY --from=ghcr.io/astral-sh/uv:0.9.6 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /src

# Dependencies before source, so editing a stage does not re-resolve the world.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-install-project --no-dev --extra postgres

COPY backend ./backend
COPY alembic.ini ./
# `--no-editable` matters: uv installs the project editable by default, which
# writes this stage's source path into the virtualenv. The runtime stage keeps
# the venv and puts the source somewhere else, so an editable install produces
# an image where `import groundscribe` fails — and it fails first inside
# alembic, which reports it as a migration problem.
RUN uv sync --frozen --no-dev --no-editable --extra postgres


FROM python:3.12-slim AS runtime

# `libpq` is not needed — psycopg[binary] carries its own — so the runtime image
# gets nothing beyond the base. Fewer packages is fewer advisories to read.
# The prompt and config roots are named explicitly, which is what `paths.py`
# says a packaged deployment must do: `repo_root()` walks up from the module's
# own location and is correct for an editable checkout, not for a wheel in
# site-packages. Left to the default, the API starts, answers /health, and then
# fails the first command with a path inside the virtualenv — a 500 that names
# a file nobody wrote.
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    GROUNDSCRIBE_PROMPTS_ROOT=/app/prompts \
    GROUNDSCRIBE_CONFIG_ROOT=/app/config \
    GROUNDSCRIBE_BLOB_ROOT=/var/lib/groundscribe/blobs \
    GROUNDSCRIBE_KEY_ROOT=/var/lib/groundscribe/keys

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY backend ./backend
COPY alembic.ini pyproject.toml ./
# The editable files: a deployment is expected to change these without a rebuild,
# which is why phase 04 put them outside the code in the first place.
COPY prompts ./prompts
COPY config ./config
COPY evaluations ./evaluations
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh

# Not root. The process writes to one directory and reads the rest; nothing it
# does needs the ability to write over its own code.
RUN useradd --system --create-home --uid 10001 groundscribe \
    && mkdir -p /var/lib/groundscribe \
    && chown -R groundscribe:groundscribe /var/lib/groundscribe /app \
    && chmod +x /usr/local/bin/entrypoint.sh
USER groundscribe

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["api"]
