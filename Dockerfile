FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY CascadeProjects/windsurf-project/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY CascadeProjects/windsurf-project ./app

ENV PORT=8000 \
    PYTHONPATH=/app/app \
    PYTHONUNBUFFERED=1

EXPOSE 8000

# 单 worker 是必须的：JOBS 在内存中，多 worker 会让轮询/下载请求打到错的进程。
CMD ["sh", "-c", "uvicorn webapp.main:app --host 0.0.0.0 --port ${PORT} --workers 1"]
