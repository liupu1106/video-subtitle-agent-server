#!/bin/bash
# 直接本地启动 video-subtitle-agent-server（放弃开机自启，改用本地手动启动）
# 双击本文件即可：在你的 GUI 会话里以后台进程运行，关掉终端窗口也保持运行。
# 依赖已预置在 vendor/wheels，首次运行也会离线安装（不联网），之后秒起。
# 重启电脑后不会自动启动 —— 需要再次双击本文件（或手动运行 ./run_local.sh）。

echo "============================================"
echo " video-subtitle-agent 本地启动"
echo "============================================"

PLABEL="com.vsb.subtitle"
PLIST="$HOME/Library/LaunchAgents/${PLABEL}.plist"

# ---------- 1) 放弃开机启动：移除 LaunchAgent ----------
echo ""
echo "【1/3】放弃开机自启策略"
if [ -f "$PLIST" ]; then
  # 若已被 launchd 加载，先卸载（失败不影响后续）
  launchctl bootout "gui/$(id -u)/${PLABEL}" 2>/dev/null
  rm -f "$PLIST"
  echo "  ✓ 已移除 LaunchAgent 配置：$PLIST"
  echo "    今后重启电脑将不再自动启动。"
else
  echo "  - 未发现 LaunchAgent 配置，跳过。"
fi
# 清理已废弃的自启脚本
rm -f "$(dirname "$0")/enable_daemon.command"

# ---------- 2) 停止可能残留的旧实例 ----------
echo ""
echo "【2/3】停止残留进程（避免 8000 端口冲突）"
if pkill -f "uvicorn app.main:app" 2>/dev/null; then
  echo "  ✓ 已停止旧进程，等待 2 秒释放端口…"
  sleep 2
else
  echo "  - 无旧进程，跳过。"
fi

# ---------- 3) 直接本地启动（run_daemon.sh 内含 venv 自举） ----------
echo ""
echo "【3/3】启动服务（后台运行，关终端不中断）"
cd "$(dirname "$0")" || exit 1
nohup bash ./run_daemon.sh >/tmp/vsb_server.log 2>&1 &
disown
echo "  ✓ 已启动，PID=$!"

# ---------- 健康检查（重试，兼容首次安装耗时） ----------
echo ""
echo "等待服务就绪（若需离线安装依赖，最多约 90 秒）…"
READY=0
for i in $(seq 1 45); do
  CODE=$(curl -s -o /dev/null -w "%{http_code}" -m 3 http://127.0.0.1:8000/api/health 2>/dev/null || echo "000")
  if [ "$CODE" = "200" ]; then READY=1; break; fi
  printf "  ."
  sleep 2
done
echo ""
if [ "$READY" = "1" ]; then
  echo "============================================"
  echo " ✅ 服务已就绪：http://127.0.0.1:8000"
  echo "============================================"
  echo " 浏览器打开上面的地址即可使用。"
  echo " 运行日志：tail -f /tmp/vsb_server.log"
else
  echo "============================================"
  echo " ⚠️  健康接口无响应（HTTP=$CODE）"
  echo "============================================"
  echo " 首次运行可能仍在安装依赖，请稍候直接探一下："
  echo "   curl -s http://127.0.0.1:8000/api/health"
  echo " 若一直无响应，查看日志定位："
  echo "   tail -n 50 /tmp/vsb_server.log"
fi
echo "（本窗口可安全关闭）"
