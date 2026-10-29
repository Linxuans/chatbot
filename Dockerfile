# 只跑智能体核心（不跑微信客户端），可部署在任意 Linux 主机 / NAS
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    MODE=core \
    DB_PATH=/data/bot.db \
    CORE_HOST=0.0.0.0 \
    CORE_PORT=8000

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src
COPY run.py .
COPY pyproject.toml .

RUN mkdir -p /data && useradd --create-home --shell /usr/sbin/nologin app \
    && chown -R app:app /app /data
USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz',timeout=4).status==200 else 1)"

CMD ["python", "run.py", "--mode", "core"]