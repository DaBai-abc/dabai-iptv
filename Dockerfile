FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY build.py publish.py ./

# 家里 iptv-api 容器的产物（result.m3u）挂到这里，当候选池，只读
VOLUME ["/iptv-output"]

# 常驻模式：每天 PUBLISH_HOUR:PUBLISH_MINUTE 跑一次实测并推回 GitHub
CMD ["python", "publish.py"]
