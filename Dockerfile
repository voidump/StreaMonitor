# syntax=docker/dockerfile:1

# Debian slim, not Alpine: onnxruntime publishes no musl wheels (and no
# sdist), so the previous Alpine base could not install requirements at all.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.lock.txt ./
RUN pip3 install --no-cache-dir -r requirements.lock.txt

COPY *.py ./
COPY streamonitor ./streamonitor

# All runtime state (config, .env, session key, key caches, NSFW model) lives
# in one volume-backed directory; see docker-compose.yml.
ENV STRMNTR_DATA_DIR=/data

RUN useradd --create-home --uid 1000 app \
    && mkdir -p /data /app/downloads \
    && chown -R app:app /data /app/downloads
USER app

EXPOSE 5000

# Reachable = alive: 200 (auth off), 302 and 401 (auth on) all prove the web
# server answers; only a dead connection ("000") fails the check.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s \
    CMD sh -c '[ "$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:5000/login)" != "000" ]'

CMD [ "python3", "Downloader.py"]
