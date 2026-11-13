# Build the web app, then run everything in one small Python image.
FROM node:22-slim AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /app/backend
COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/app ./app
COPY --from=web /web/dist /app/frontend/dist
# Videos, the database and downloaded Whisper models all live on the /data volume.
ENV DATA_DIR=/data HF_HOME=/data/models HOST=0.0.0.0 PORT=8000
VOLUME /data
EXPOSE 8000
CMD ["python", "-m", "app"]
