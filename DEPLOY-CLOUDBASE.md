# 部署到腾讯云 CloudBase 云托管（Cloud Run）

本目录是一个常驻容器服务（FastAPI + uvicorn + 内置 ffmpeg），支撑单任务 / 整季批量解析 / 四格式导出。
**云托管会在云端按 `Dockerfile` 构建镜像，本地不需要 Docker。**

## 1. 前置条件
- 一个 CloudBase 环境（控制台创建），且已开通「云托管 / Cloud Run」。
- 记下环境 ID（形如 `video-sub-1gabcde123`，控制台「环境设置」可见）。
- 安装 CLI：`npm i -g @cloudbase/cli`（本机已装 3.8.1）。

## 2. 登录（二选一）

**A. 本机交互登录（推荐，你自己执行最省事）**
```bash
cloudbase login            # 浏览器扫码授权
cloudbase env use <envId>  # 绑定默认环境，之后可省略 -e
```

**B. 给本 agent 一个仅限该环境的 Key（非交互）**
在 CloudBase 控制台 → 该环境 →「登录授权 / 环境 API 密钥」生成一个环境 API Key，
把 `envId` 和 `Key` 发给 agent，由 agent 执行：
```bash
cloudbase login --cloudbase-api-key <环境Key> -e <envId>
```
> 比腾讯云 SecretID/SecretKey 安全：只作用于这一个环境，不能动账号其他资源。

## 3. 部署（云端构建，无需本地 Docker）
```bash
cd video-subtitle-agent-server
cloudbase cloudrun deploy \
  --source . \
  -s video-subtitle \
  -p 8000 \
  -e <envId> \
  --wait --force
```
- `--source .` 把当前目录（含 `Dockerfile`/`app`/`static`，已用 `.dockerignore` 排除 `.env` 等）上传到云端构建。
- `-p 8000` 与容器内 `uvicorn --port 8000` 一致；云托管会把 `PORT` 注入容器，应用自动监听。
- 完成后命令行会输出**默认访问域名**（形如 `https://video-subtitle-<envId>.ap-shanghai.run.tcloudbase.com`）。

## 4. 拿到默认域名
```bash
cloudbase cloudrun detail -s video-subtitle -e <envId>
```

## 5. 控制台补充环境变量（可选，先不配也能跑）
云托管默认不配任何变量也能启动：
- `DASHSCOPE_API_KEY` 留空 → ASR/AI 梳理降级为规则清洗（你选了「部署时不填 Key」）。
- `REDIS_URL` 留空 → 任务状态走内存（**单副本**可用；多副本需配 TencentDB for Redis）。

要开通真实字幕识别/AI 梳理时，在云托管服务「配置 → 环境变量」加 `DASHSCOPE_API_KEY=<你的Key>`，
**建议先到 DashScope 控制台轮换一次 Key**（此前曾在会话中明文出现），新 Key 只走环境变量，绝不进代码/镜像。

其他可调：`IP_LIMIT_PER_HOUR`(默认20) / `GLOBAL_CONCURRENCY`(默认4) / `BATCH_CONCURRENCY`(默认3) / `MAX_AUDIO_SEC`(默认7200)。

## 6. 验证
```bash
# 健康检查（不依赖 Key）
curl https://<默认域名>/api/health

# 提交一个普通视频任务，轮询 /api/job/{id} 看状态
# 番剧链接勾选「整季批量」会返回 batch_id，/api/job/{batch_id} 聚合每集进度
# 完成后 /api/job/{id}/export?format=txt|srt|md|json 下载文件
```

## 7. 注意事项
- **内存后端 + scale-to-0**：整季批量是后台长任务，云托管若缩容到 0 或重调度会中断执行中任务。
  请在云托管「配置」里把**最小实例数设为 1**，避免冷启动打断批量。
- **多副本需 Redis**：若要水平扩容，先建 TencentDB for Redis，把 `REDIS_URL` 配上，否则并发计数会错乱。
- **限流**：`IP_LIMIT_PER_HOUR` 防止刷爆 DashScope 账单；上线前按预期流量调整。
