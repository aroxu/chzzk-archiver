# Remote HEVC encoding worker.
#
# Build from the repository root so the backend package is in context:
#   docker build -f worker/Dockerfile -t chzzk-archiver-worker .
FROM python:3.12-slim AS build

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /src
COPY pyproject.toml ./
COPY backend/app ./backend/app
RUN pip install --no-cache-dir .


FROM python:3.12-slim

# ffmpeg supplies libx265 plus the VAAPI/QSV encoders used on Linux hosts.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# The worker only makes outbound connections, so it never needs root. Groups
# 44/109 are video/render on Debian, which /dev/dri passthrough requires.
RUN groupadd --system --gid 1000 worker \
    && useradd --system --uid 1000 --gid worker --no-create-home worker \
    && usermod -aG video,render worker 2>/dev/null || true
USER worker
WORKDIR /home/worker

# Fails fast on a broken ffmpeg or a missing HEVC encoder.
HEALTHCHECK --interval=60s --timeout=20s --start-period=10s --retries=3 \
    CMD ["archiver-worker", "--doctor"]

ENTRYPOINT ["archiver-worker"]

