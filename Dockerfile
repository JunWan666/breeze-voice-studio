# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HOST=0.0.0.0 \
    PORT=8674 \
    BREEZE_COOKIE_FILE=/data/cookies.json

WORKDIR /app

# 依赖先装，利用层缓存
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py bb_api.py ./
COPY templates ./templates
COPY static ./static

# 用非 root 跑，/data 用来挂载服务端只读登录态（不放也行，会走演示模式）
RUN useradd -r -u 10001 -m appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /app /data
USER appuser

VOLUME ["/data"]
EXPOSE 8674

HEALTHCHECK --interval=30s --timeout=5s --start-period=8s --retries=3 \
    CMD python -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8674/healthz', timeout=3).status == 200 else 1)"

CMD ["python", "app.py"]
