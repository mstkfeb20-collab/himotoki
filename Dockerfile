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
ENV CODEGRAPH_DATA_DIR=/data CODEGRAPH_FRONTEND=/app/frontend/dist CODEGRAPH_ALLOWED_ROOTS=/repos:/data
RUN useradd --create-home --uid 10001 codegraph && mkdir -p /data && chown codegraph /data
USER codegraph
EXPOSE 8765
# refuses to start without CODEGRAPH_API_TOKEN and CODEGRAPH_ALLOWED_HOSTS (see docs/SECURITY.md)
CMD ["python", "-m", "codegraph.cli", "serve", "--host", "0.0.0.0", "--port", "8765"]
