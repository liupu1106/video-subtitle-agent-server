#!/usr/bin/env bash
# 本地启动器（供 start_local.command / run_local.sh 调用）。
# 首次运行会在项目内创建 ./venv 并安装 requirements.txt，之后直接用 venv 启动。
# 用已知的 managed Python 3.13.12 建 venv（避开 Homebrew 3.14 的潜在兼容问题）。
set -a
cd /Users/liupu/WorkBuddy/2026-08-27-13-16-47/video-subtitle-agent-server || exit 1
if [ -f ./.env ]; then
  . ./.env
fi
set +a

# 优先用 managed Python 3.13.12 建 venv（已知可用），否则退回系统 python3
PYBIN="/Users/liupu/.workbuddy/binaries/python/versions/3.13.12/bin/python3"
if [ ! -x "$PYBIN" ]; then
  PYBIN="python3"
fi

VENV="./venv"
if [ ! -x "$VENV/bin/uvicorn" ]; then
  echo "首次运行：创建虚拟环境并从本地 vendor/wheels 离线安装依赖（无需联网）…"
  if [ -d "$VENV" ]; then
    rm -rf "$VENV"
  fi
  if ! "$PYBIN" -m venv "$VENV"; then
    echo "❌ 创建虚拟环境失败：请确认 $PYBIN 含 venv 模块。"
    exit 1
  fi
  "$VENV/bin/pip" install --upgrade pip >/dev/null 2>&1
  # 优先离线安装：依赖已预置在 vendor/wheels，不联网
  if [ -d vendor/wheels ] && [ "$(ls -A vendor/wheels 2>/dev/null)" ]; then
    if "$VENV/bin/pip" install --no-index --find-links vendor/wheels -r requirements.txt 2>/dev/null; then
      echo "✓ 已离线安装依赖（vendor/wheels）"
    else
      echo "⚠️ 离线安装失败，尝试联网安装…"
      "$VENV/bin/pip" install -r requirements.txt
    fi
  else
    echo "⚠️ 未发现 vendor/wheels，改为联网安装依赖…"
    "$VENV/bin/pip" install -r requirements.txt
  fi
  if [ ! -x "$VENV/bin/uvicorn" ]; then
    echo "❌ 依赖安装失败：请检查 vendor/wheels 或网络后重新双击启动脚本。"
    exit 1
  fi
  echo "✓ 依赖安装完成"
else
  echo "✓ 检测到已就绪的 venv，直接启动（无需安装）"
fi

# 系统工具（ffmpeg 等）路径补全
export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:$PATH"

exec "$VENV/bin/uvicorn" app.main:app --host 127.0.0.1 --port 8000 --log-level info
