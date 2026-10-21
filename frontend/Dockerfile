# syntax=docker/dockerfile:1
#
# Two stages: build the React bundle with Node, then serve it with nginx. The
# Node toolchain does not ship to production — the final image is nginx plus a
# directory of static files.
#
#   docker build -t voltmart-frontend frontend/

# ---------------------------------------------------------------------------
# Stage 1: build
# ---------------------------------------------------------------------------
FROM node:20-alpine AS build

WORKDIR /build

# Dependencies in their own layer. `npm ci` installs exactly what the lockfile
# says, which is what makes a workshop build reproducible across accounts.
COPY package.json package-lock.json* ./
RUN if [ -f package-lock.json ]; then npm ci; else npm install; fi

COPY tsconfig.json vite.config.ts tailwind.config.js postcss.config.js index.html ./
COPY public ./public
COPY src ./src

# `npm run build` type-checks before bundling, so a type error fails the image
# build rather than shipping.
RUN npm run build

# ---------------------------------------------------------------------------
# Stage 2: serve
# ---------------------------------------------------------------------------
FROM nginx:1.27-alpine

# Upstream addresses, substituted into the config at container start by
# nginx's own envsubst entrypoint. Overridden per environment by the
# Deployment manifest.
ENV CATALOG_UPSTREAM=http://voltmart-catalog:8080 \
    INVENTORY_UPSTREAM=http://voltmart-inventory:8080 \
    CART_UPSTREAM=http://voltmart-cart:8080 \
    ORDERS_UPSTREAM=http://voltmart-orders:8080 \
    CHECKOUT_UPSTREAM=http://voltmart-checkout:8080 \
    AGENT_UPSTREAM=http://voltmart-support-agent:8080 \
    NGINX_RESOLVER=172.20.0.10

# The stock config would otherwise also listen on 80 and shadow ours.
RUN rm -f /etc/nginx/conf.d/default.conf

COPY nginx.conf.template /etc/nginx/templates/voltmart.conf.template
COPY --from=build /build/dist /usr/share/nginx/html

# Run unprivileged on a high port. nginx:alpine ships an `nginx` user (uid
# 101); the directories it writes to at runtime need to belong to it, and
# binding 8080 instead of 80 removes any need for NET_BIND_SERVICE.
RUN touch /var/run/nginx.pid \
    && chown -R nginx:nginx /var/cache/nginx /var/run/nginx.pid /etc/nginx/conf.d \
    && chown -R nginx:nginx /usr/share/nginx/html
USER nginx

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD wget --quiet --tries=1 --spider http://127.0.0.1:8080/healthz || exit 1

CMD ["nginx", "-g", "daemon off;"]
