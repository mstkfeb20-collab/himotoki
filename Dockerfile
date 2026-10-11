# Build the viewer, then run the API (which also serves the viewer).
FROM node:22-slim AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends git postgresql-client && rm -rf /var/lib/apt/lists/*
WORKDIR /app/backend
COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/ ./
COPY --from=web /web/dist /app/frontend/dist
ENV HIMOTOKI_DATA_DIR=/data HIMOTOKI_FRONTEND=/app/frontend/dist HIMOTOKI_ALLOWED_ROOTS=/repos:/data
RUN useradd --create-home --uid 10001 himotoki && mkdir -p /data && chown himotoki /data
USER himotoki
EXPOSE 8765
# refuses to start without HIMOTOKI_API_TOKEN and HIMOTOKI_ALLOWED_HOSTS (see docs/SECURITY.md)
CMD ["python", "-m", "himotoki.cli", "serve", "--host", "0.0.0.0", "--port", "8765"]
