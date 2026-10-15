# Build context is the REPO ROOT (see docker-compose.yml) so this stage can
# also pick up nginx/vallotton.conf.

FROM node:22-alpine AS build
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-fund --no-audit
COPY frontend/ .
RUN npm run build

FROM nginx:1.27-alpine
COPY nginx/vallotton.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
