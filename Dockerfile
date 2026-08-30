# 视频字幕提取 —— 容器镜像
# 内置 ffmpeg（云函数环境的痛点是没有它，导致音频格式/取流受限）。
FROM python:3.11-slim

# 系统依赖：ffmpeg 用于转 16k 单声道 wav + 超长裁剪；证书保证 HTTPS 抓取。
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY static ./static

# 用 uvicorn 提供 ASGI 服务；监听全部网卡以便容器外访问。
ENV HOST=0.0.0.0
ENV PORT=8000
EXPOSE 8000

CMD ["sh", "-c", "uvicorn app.main:app --host ${HOST} --port ${PORT}"]
