"""运行时配置，全部通过环境变量覆盖。"""
from __future__ import annotations

import os

# SQLite 数据库文件路径（容器内默认 /data/router.db，本地默认 ./router.db）
DB_PATH = os.environ.get("ROUTER_DB", "./router.db")

# 服务监听地址与端口（供 README / uvicorn 启动命令参考，实际由启动命令行生效）
HOST = os.environ.get("ROUTER_HOST", "0.0.0.0")
PORT = int(os.environ.get("ROUTER_PORT", "8000"))
