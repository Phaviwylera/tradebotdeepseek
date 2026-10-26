# syntax=docker/dockerfile:1
#
# Two build shapes from one file:
#
#   docker build -t polymarket-bot .                  # runtime only (~350 MB)
#   docker build -t polymarket-bot --build-arg INSTALL_ML=true .   # + xgboost/lightgbm
#
# The ML wheels are only needed to train a model or to load an artifact that was
# trained with them. The default build stays small enough for a 512 MB host.

FROM python:3.11-slim AS runtime

ARG INSTALL_ML=false

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    LIVE=false \
    LEDGER_PATH=/data/ledger.db \
    MODEL_PATH=/data/model.joblib \
    API_PORT=8099

WORKDIR /app

# libgomp1 is required by the gradient-boosting wheels; ca-certificates keeps
# TLS to the Polymarket endpoints working on slim images.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements-ml.txt ./
RUN python -m pip install --upgrade pip \
    && if [ "$INSTALL_ML" = "true" ]; then \
         python -m pip install -r requirements-ml.txt; \
       else \
         python -m pip install -r requirements.txt; \
       fi

COPY . .

RUN useradd --create-home --uid 10001 bot \
    && mkdir -p /data \
    && chown -R bot:bot /app /data

USER bot

VOLUME ["/data"]
EXPOSE 8099

# Liveness of the read-only status server. The trading loop also writes a
# heartbeat into SQLite; /healthz only proves the process is serving HTTP.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import os,urllib.request;port=os.environ.get('PORT') or os.environ.get('API_PORT') or '8099';urllib.request.urlopen('http://127.0.0.1:%s/healthz' % port, timeout=4)"

CMD ["python", "main.py"]
