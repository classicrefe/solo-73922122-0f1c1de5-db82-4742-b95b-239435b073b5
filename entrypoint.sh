#!/bin/sh
set -e

# 建表；数据库为空时灌入演示数据（重复执行保持幂等）
flask --app wsgi:app init-db
flask --app wsgi:app seed

# 双 worker 验证 SQLite 多连接写入；容器内 gunicorn 托管
exec gunicorn --bind 0.0.0.0:8000 --workers 2 --timeout 60 wsgi:app
