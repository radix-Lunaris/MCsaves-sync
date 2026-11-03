FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MCSYNC_DATA=/data \
    MCSYNC_PORT=8650

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata curl \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd -r -u 10001 -m -d /home/mcsync mcsync

COPY app.py store.py protocol.py auth.py accounts.py ./
COPY static ./static

RUN mkdir -p /data && chown -R mcsync:mcsync /data /app

USER mcsync
EXPOSE 8650
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8650/api/health || exit 1

CMD ["python", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8650", "--proxy-headers", "--forwarded-allow-ips", "*"]
