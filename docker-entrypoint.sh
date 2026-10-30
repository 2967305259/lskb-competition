#!/bin/bash
set -e

echo "============================================"
echo "  ${TOURNAMENT_NAME:-${EVENT_NAME:-赛事}} 报名管理系统 - Docker 启动"
echo "============================================"

# 如果使用 MySQL，等待数据库就绪
if echo "$DATABASE_URL" | grep -q "mysql"; then
    echo "[INFO] 检测到 MySQL 配置，等待数据库就绪..."
    # 提取 host:port
    DB_HOST=$(echo "$DATABASE_URL" | sed -n 's/.*@\(.*\)\/.*/\1/p')
    DB_HOSTNAME=$(echo "$DB_HOST" | cut -d: -f1)
    DB_PORT=$(echo "$DB_HOST" | cut -d: -f2)
    DB_PORT=${DB_PORT:-3306}

    echo "[INFO] 等待 $DB_HOSTNAME:$DB_PORT ..."
    while ! nc -z "$DB_HOSTNAME" "$DB_PORT" 2>/dev/null; do
        sleep 2
    done
    echo "[INFO] 数据库已就绪！"
fi

echo "[INFO] 启动应用 (环境: ${FLASK_ENV:-production})"

# 使用 Gunicorn 启动（生产模式）
if [ "${FLASK_ENV}" = "production" ]; then
    exec gunicorn --bind 0.0.0.0:${PORT:-19198} \
        --workers 4 \
        --timeout 120 \
        --access-logfile - \
        --error-logfile - \
        app:app
else
    # 开发模式
    exec python app.py
fi