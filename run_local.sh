#!/usr/bin/env bash
# 本地前台启动（Ctrl+C 停止）。
# 直接复用 run_daemon.sh（含虚拟环境自举与 .env 注入）。
# 想后台运行/关终端不中断，用 ./start_local.command 更省事。
exec "$(dirname "$0")/run_daemon.sh"
