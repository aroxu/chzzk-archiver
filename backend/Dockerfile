FROM node:22-alpine AS web
WORKDIR /src/web
COPY web/package*.json ./
RUN npm install
COPY web/ ./
RUN npm run build

FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends aria2 ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml ./
COPY backend/app ./backend/app
RUN pip install --no-cache-dir .
COPY --from=web /src/web/dist ./web/dist
ENV PYTHONPATH=/app/backend ARCHIVER_DATABASE_URL=sqlite:////data/archiver.db ARCHIVER_RECORDINGS_DIR=/recordings ARCHIVER_WEB_DIST=/app/web/dist
VOLUME ["/data", "/recordings"]
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
