# Tessera container image.
# BASE can be any Debian/Ubuntu-family image with Python 3.11+ and the venv module (default: the official slim image).
ARG BASE=python:3.12-slim
FROM ${BASE}

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    TESSERA_DATA_DIR=/data TESSERA_HOST=0.0.0.0 TESSERA_PORT=8710 \
    PATH=/opt/venv/bin:$PATH

RUN useradd --create-home --uid 10001 tessera \
 && python3 -m venv /opt/venv
WORKDIR /app
COPY pyproject.toml README.md LICENSE NOTICE ./
COPY tessera ./tessera
RUN /opt/venv/bin/pip install --no-cache-dir ".[mysql,s3,mongodb,delta,kafka,mqtt,iceberg,sftp]" \
 && mkdir -p /data && chown tessera:tessera /data

USER tessera
VOLUME ["/data"]
EXPOSE 8710
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s CMD python3 -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/ready' % os.environ.get('TESSERA_PORT', '8710'))"
CMD ["tessera", "serve", "--host", "0.0.0.0"]
