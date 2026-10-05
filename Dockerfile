FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    ROUTER_DB=/data/router.db \
    ROUTER_HOST=0.0.0.0 \
    ROUTER_PORT=8000

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# SQLite 数据目录（挂载卷即可持久化）；首次启动自动建表
RUN mkdir -p /data

EXPOSE 8000

CMD ["sh", "-c", "uvicorn app.main:app --host ${ROUTER_HOST} --port ${ROUTER_PORT}"]
