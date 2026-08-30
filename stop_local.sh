#!/usr/bin/env bash
# 停止本地服务。用法： ./stop_local.sh        # 默认 8000 端口
#                    PORT=9000 ./stop_local.sh
cd "$(dirname "$0")"
PORT="${PORT:-8000}"
PIDS=$(lsof -ti tcp:"$PORT" 2>/dev/null)
if [ -z "$PIDS" ]; then
  echo "端口 $PORT 上没有运行中的服务"
  exit 0
fi
echo "停止服务 PID: $PIDS"
kill $PIDS && echo "✅ 已停止"
