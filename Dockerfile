# ============================================================
# 赛事比赛管理系统 - Docker 镜像
# 赛事名称/届数只用于镜像标签，运行时行为由环境变量决定
# ============================================================

FROM python:3.11-slim

ARG EVENT_NAME=冷水坑杯
ARG SEASON_NUMBER=3

LABEL maintainer="lskb"
LABEL description="${EVENT_NAME} #${SEASON_NUMBER} 比赛管理系统"

# 设置环境变量
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FLASK_ENV=production \
    TZ=Asia/Shanghai

WORKDIR /app

# 安装系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    tzdata \
    netcat-openbsd \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

# 复制依赖文件并安装
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir gunicorn==21.2.0

# 复制项目文件
COPY . .

# 创建必要目录
RUN mkdir -p /app/.db /app/app/static/uploads/avatars /app/app/static/uploads/teams

# 暴露端口
EXPOSE 19198

# 启动脚本
COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

ENTRYPOINT ["/docker-entrypoint.sh"]