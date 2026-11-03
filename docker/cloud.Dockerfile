# syntax=docker/dockerfile:1.7
# Cloud service: the Node.js records API with the built React dashboard baked
# in, so one container serves both the API and the UI on port 4000.
#
#   docker build -f docker/cloud.Dockerfile -t vetra/cloud .

# --- stage 1: build the dashboard -------------------------------------------
FROM node:20-slim AS dashboard
WORKDIR /build
COPY dashboard/package.json dashboard/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY dashboard/ ./
RUN npm run build

# --- stage 2: install the service's production dependencies -----------------
# better-sqlite3 ships prebuilt binaries for Node 20. The compiler toolchain is
# only the fallback for a platform without one, and never reaches the runtime
# image because only node_modules is copied out of this stage.
FROM node:20-slim AS deps
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 make g++ \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /build
COPY server/package.json server/package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund

# --- stage 3: runtime --------------------------------------------------------
FROM node:20-slim
ENV NODE_ENV=production \
    PORT=4000 \
    VETRA_CLOUD_DB=/data/vetra_cloud.db \
    VETRA_GATEWAY_URL=http://gateway:5001
WORKDIR /app/server
COPY --from=deps /build/node_modules ./node_modules
COPY server/package.json ./
COPY server/src ./src
COPY server/test ./test
# index.js serves ../../dashboard/dist relative to src/, so keep that layout.
COPY --from=dashboard /build/dist /app/dashboard/dist
RUN mkdir -p /data && chown node:node /data
USER node
EXPOSE 4000
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 \
  CMD node -e "fetch('http://127.0.0.1:'+(process.env.PORT||4000)+'/health').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"
CMD ["node", "src/index.js"]
