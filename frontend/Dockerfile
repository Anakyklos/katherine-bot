# Immutable base images pinned by digest (snapshot 2026-08-03).
# Update procedure: pick new digests at https://hub.docker.com/_/node and
# https://hub.docker.com/_/nginx, replace the @sha256 references, and verify
# the full CI matrix.
FROM node:22-alpine@sha256:c610fcdfb1d5b4740dd70c284ed3cb16bb857e0f7166196e36a5501df7a3aa32 AS build

WORKDIR /app

# Required public build configuration. The build fails early when any of
# these is missing or malformed instead of publishing a bundle without an
# API/Supabase configuration.
ARG VITE_API_URL
ARG VITE_SUPABASE_URL
ARG VITE_SUPABASE_ANON_KEY

RUN test -n "$VITE_API_URL" && test -n "$VITE_SUPABASE_URL" && test -n "$VITE_SUPABASE_ANON_KEY" \
    || { echo "Missing required VITE_* build argument (VITE_API_URL, VITE_SUPABASE_URL, VITE_SUPABASE_ANON_KEY)"; exit 1; }

RUN case "$VITE_API_URL" in \
        http://*|https://*) ;; \
        *) echo "VITE_API_URL must be an http(s) URL"; exit 1 ;; \
    esac \
    && case "$VITE_SUPABASE_URL" in \
        http://*|https://*) ;; \
        *) echo "VITE_SUPABASE_URL must be an http(s) URL"; exit 1 ;; \
    esac \
    && case "$VITE_SUPABASE_ANON_KEY" in \
        *.*.*) ;; \
        *) echo "VITE_SUPABASE_ANON_KEY must be a JWT"; exit 1 ;; \
    esac

ENV VITE_API_URL=$VITE_API_URL \
    VITE_SUPABASE_URL=$VITE_SUPABASE_URL \
    VITE_SUPABASE_ANON_KEY=$VITE_SUPABASE_ANON_KEY

COPY package.json package-lock.json /app/
RUN npm ci

COPY . /app
RUN npm run build

FROM nginx:1.27-alpine@sha256:65645c7bb6a0661892a8b03b89d0743208a18dd2f3f17a54ef4b76fb8e2f2a10

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 80
