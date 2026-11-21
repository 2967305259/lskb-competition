# ============================================================
# 赛事比赛管理系统 - Docker 镜像
# 赛事名称/届数只用于镜像标签，运行时行为由环境变量决定
# ============================================================

ARG MYSQL_IMAGE=mysql:8.0
FROM ${MYSQL_IMAGE} AS mysql_client
FROM python:3.12-slim-trixie

ARG EVENT_NAME=冷水坑杯
ARG SEASON_NUMBER=3
ARG APP_VERSION=season-release-v1

LABEL maintainer="lskb"
LABEL description="${EVENT_NAME} #${SEASON_NUMBER} 比赛管理系统"
LABEL org.opencontainers.image.version="${APP_VERSION}"

# 设置环境变量
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FLASK_ENV=production \
    APP_VERSION=${APP_VERSION} \
    TZ=Asia/Shanghai

# 依赖和 MySQL 客户端一起安装，应用代码变动时可复用此层。
RUN --mount=type=bind,from=mysql_client,source=/usr/bin,target=/mysql-tools \
    --mount=type=bind,source=requirements.txt,target=/tmp/requirements.txt \
    --mount=type=secret,id=pip_ca \
    apt-get update && apt-get install -y --no-install-recommends \
    tzdata \
    libncurses6 libssl3t64 libstdc++6 zlib1g \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/* \
    && install /mysql-tools/mysql /mysql-tools/mysqldump /usr/local/bin/ \
    && mysql --version && mysqldump --version \
    && { if [ -s /run/secrets/pip_ca ]; then export PIP_CERT=/run/secrets/pip_ca; fi; } \
    && mkdir /app && cp /tmp/requirements.txt /app/requirements.txt \
    && pip install --no-cache-dir -r /app/requirements.txt gunicorn==21.2.0

WORKDIR /app

# 只打包应用文件与内置素材，运行时数据由挂载目录提供。
RUN --mount=type=bind,target=/source \
    cp /source/*.py /app/ \
    && cp -a /source/app /source/image /app/ \
    && install /source/docker-entrypoint.sh /docker-entrypoint.sh \
    && mkdir -p /app/.db /app/.runtime /app/backups /app/app/static/uploads/avatars \
       /app/app/static/uploads/teams /app/app/static/uploads/screenshots

# 暴露端口
EXPOSE 19198

ENTRYPOINT ["/docker-entrypoint.sh"]
