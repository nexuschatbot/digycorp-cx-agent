# Elite Test Drive Booking — frontend image (AD-G)
# Multi-stage: build the Vite bundle with Node, then serve the static dist/
# with nginx. nginx also proxies /api to the backend over the compose network.

# --- Stage 1: build ---------------------------------------------------------
FROM node:20-alpine AS build
WORKDIR /app

# Install deps from the lockfile first (cached until package*.json changes).
COPY package.json package-lock.json ./
RUN npm ci

# Build the production bundle (tsc && vite build → /app/dist).
COPY . .
RUN npm run build

# --- Stage 2: serve ---------------------------------------------------------
FROM nginx:alpine
# SPA routing + SSE-safe /api proxy (see nginx.conf).
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
