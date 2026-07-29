# The web app, built once and served as static files (phase 14).
#
# Not `vite dev` in a container. The dev server rebuilds on change, watches the
# filesystem and serves unminified modules — all useful on a laptop and all
# wrong for something a person installs. What ships is the build output behind
# nginx, which also gives the `/api` proxy a home: the browser then sees one
# origin, so the SSE stream needs no CORS negotiation to stay open, exactly as
# the Vite proxy arranges in development.

FROM node:22-alpine AS builder

WORKDIR /src

COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund

# The generated client lives outside the frontend directory, and the build
# imports it through the `@contracts` alias. Copied before the source so a
# contract change rebuilds the app, which is the point of generating it.
COPY contracts ../contracts
COPY frontend ./
RUN npm run build


FROM nginx:1.27-alpine AS runtime

COPY --from=builder /src/dist /usr/share/nginx/html
COPY docker/frontend.nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 3000
