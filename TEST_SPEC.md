# 验收 & 回归测试规格（TEST_SPEC）

> 代码基线：`ec04855`（2026-08-30，含模型选择面板 CSS 显隐修复）
> 适用项目：`video-subtitle-agent-server`（B 站视频字幕解析 · DashScope ASR + 通义千问梳理 · FastAPI）
> 用途：本文件用于**验收测试**（确认需求已实现）与**回归测试**（每次发布确认未破坏既有功能）。
> 读法：§3 路线图 → §4 功能明细（含验收标准）→ §5 验收用例（步骤+预期，可直接执行）→ §6 回归清单（发布必跑）→ §7 已知限制（避免误报）。

---

## 1. 环境基线（测试前置条件）

| 项 | 值 / 说明 |
|---|---|
| 运行时 | Python 3.13.12（managed），venv 在 `./venv` |
| 启动命令 | `bash run_daemon.sh`（自动建 venv、离线装 `vendor/wheels`、`unset` 代理强制直连） |
| 本地地址 | `http://127.0.0.1:8000`（CloudBase 线上为旧构建，见 §3.3 / handover §0） |
| 必需配置 | `.env` 中 `DASHSCOPE_API_KEY`（否则 ASR 不可用，AI 梳理降级为规则清洗） |
| 关键依赖 | fastapi / uvicorn / redis / python-multipart / yt-dlp / requests / openpyxl / reportlab |
| 外部可达 | `api.bilibili.com`（解析探测）、`dashscope.aliyuncs.com`（ASR/LLM）；均需**直连**（代理已 unset） |
| 状态存储 | `REDIS_URL` 配置走 Redis（多副本共享）；留空降级内存（仅单进程，重启丢任务） |

---

## 2. 测试分级约定

- **P0（阻断级）**：不通过则不可发布。含启动、单视频解析、ASR、导出、模型接口。
- **P1（重要）**：批量、状态查询、上传、限流。
- **P2（细节）**：设置页、缓存、分布式 store 降级。
- **手动/集成**：依赖真实 B 站链接 + 真实 DashScope Key + 外网，无法在 CI 纯离线跑，需人工标注环境。

---

## 3. 路线图（Roadmap）

### 3.1 已实现（里程碑）
| 阶段 | 内容 | 关键提交 |
|---|---|---|
| 初版 | B 站单视频解析 + DashScope ASR + 四页面四格式导出 | — |
| 修复 A | 文件 URL 有效文件校验 | — |
| 修复 B | WebSocket 1011 keepalive 断连（客户端主动 ping + 并发收发 + max_queue） | — |
| 修复 C | 导出失败 + 速度优化（20 路并发 + 静音切除） | `4920b49` |
| 模型管理 | 三下拉框（语音/文本/替代）+ `/api/models` + 自动回退 | `4920b49` |
| 部署/保活 | 项目内独立 git 仓库 + SSH 部署密钥推 GitHub；本地 `start_new_session` 保活 | — |
| 网络 | 代理直连修复（`run_daemon.sh` unset 代理） | — |
| 前端修复 | 模型选择面板 `.selpanel` 被 CSS 隐藏 → 加 `id=modelPanel` 强制 `display:block` | `ec04855` |

### 3.2 进行中
- 无（当前代码冻结于 `ec04855`，等待验收）。

### 3.3 计划中（来自 handover §8，按优先级）
- **P0**：CloudBase 线上重新部署（当前线上是 2026-08-28 旧构建，缺下拉框/提速/CSS 修复）。
- **P1**：
  - 把 `ASR_MODEL_PRIORITY` / `LLM_MODEL_PRIORITY` 写入 `.env.example` 显式占位。
  - 若 DashScope 提并发配额 >20 路，调大 `ASR_MAX_CONCURRENCY`（当前硬上限 20）。
  - 前端把「快到期模型」按权益页顺序默认置顶排序。
- **P2**：
  - 任务状态接入 Redis 持久化（避免内存 store 重启丢失）。
  - 解析进度页每视频实时详情（批量 children 已支持，单任务本就无 children）。
  - 补充 pytest 单测（mock DashScope/Redis）覆盖 `llm_with_fallback`、`_realtime_asr` 切片、`_gen_section_file`。
  - 上传文件大小/格式白名单与病毒扫描（当前仅 `MAX_AUDIO_SEC` 时长校验）。

---

## 4. 已实现功能明细（含验收标准）

### F1 本地启动与服务存活
- `bash run_daemon.sh` 自动建 venv、离线装依赖、启动 uvicorn（127.0.0.1:8000）。
- 启动前 `unset` 所有代理变量，强制后端直连外网。
- **验收标准**：`GET /api/health` 返回 `200`；进程在回合间隙存活（用 `start_new_session` 脱离进程组）。

### F2 链接探测 `/api/resolve`
- 入参 `url`；返回 `{kind, is_multi, count, title, items}`。
- `kind`：`bangumi`（番剧整季）/ `video`（UGC 多分P）/ `unknown`。
- `items`：`[{index, title, duration, page?}]`，供前端勾选批量子集。
- 异常被 `try/except` 兜底，始终返回 200（探测失败不阻断提交）。
- **验收标准**：合法 B 站 BV/番剧 URL 返回正确 `count` 与每集 `title`；空 URL 返回 `unknown` 且 `count=0`。

### F3 单视频解析 `/api/process`
- 入参：`url, api_key, cookie, asr_model, llm_model, fallback_model, batch, selected`。
- 无 `url` → `400`。`api_key` 缺省回退服务端 `DASHSCOPE_API_KEY`（env `SERVER_API_KEY` 同义）。
- 流程：下载音频 → ASR 转写 → 通义千问梳理（智能校验/技术提取/中英文对照）→ 结构化结果。
- **验收标准**：返回 `200` + `job_id`；`/api/job/{job_id}` 可查到 `status=done` 与 `result`。

### F4 批量整季 / 多分P
- `batch=true` 时按 `is_bangumi`/`get_video_pages` 展开；`selected`（0-based 序号数组）支持勾选子集，缺省=全部。
- 批量任务 `kind=batch`，`children=[child_id,...]`；每集独立子任务并行。
- **验收标准**：提交整季返回 `batch_id` 与 `children` 数 = 集数；进度页列出每集标题/状态/进度（children 非单任务，勿误报"丢失"）。

### F5 DashScope ASR 提速（核心性能）
- 切片并行：块数 `n = min(ASR_MAX_CONCURRENCY, dur // ASR_MIN_CHUNK_SEC)`。
- 并发硬上限 `ASR_MAX_CONCURRENCY=20`（账号 `Throttling.RateQuota` 上限，超出被限流）。
- 相邻块 `overlap_sec=1.5s` 重叠，避免句子在边界截断。
- 静音切除 `ASR_SILENCE_REMOVE=1`（默认开）：ffmpeg 压缩有效语音，省按时长计费且提速。
- `asyncio.Semaphore(max_conc)` 封顶防限流。
- **验收标准（实测）**：10 分钟视频 ≈ 34.2s；1 小时视频（含 ~30% 静音）典型 ≈ 128s（<3 分钟）。需真实 Key + 外网测。

### F6 模型分层 + 自动回退 + 三下拉框
- 语音模型 `REALTIME_ASR_MODELS`：`fun-asr-mtl-realtime` / `fun-asr-realtime` / `qwen-audio-3.0-asr-flash-streaming` / `fun-asr-flash-8k-realtime`。
- 文本模型 `LLM_MODELS`：`qwen3.5-plus` / `qwen3.6-plus` / `qwen3.7-plus`（快到期优先排序）。
- 候选顺序（ASR）：`asr_model(前端)` > `REALTIME_ASR_MODELS` 默认序 > `fallback_model`；任一过期/无 token 自动跳下一个（`_AsrModelUnavailable` 触发）。
- 候选顺序（LLM）：`llm_model(前端首选)` > `LLM_MODELS` > `fallback_model`。
- `GET /api/models` 返回 `{asr, llm, fallback}`（`fallback = asr+llm` 去重并集）。
- 前端「🚀 新建解析」页三下拉框：`asrModel`(🎙 语音识别) / `llmModel`(💬 文本) / `fallbackModel`(🔁 替代)，含「自动」空选项，`fillModelSelects()` 初始化时 `fetch /api/models` 填充。
- **验收标准**：`/api/models` 返回 asr=4、llm=3、fallback=7；页面三下拉框**可见**（CSS 已修复 `display:block`）；提交带 `asr_model` 时日志确认该模型被优先。

### F7 任务状态查询 `/api/job/{job_id}`
- 返回 `{status, progress, result, children?, ...}`。
- `status`：`pending/running/done/failed`。
- **验收标准**：单任务 `children` 缺省/空；批量任务 `children` 为子任务 id 数组。

### F8 查看 / 结果页
- 「查看」页（`page-result`）渲染 `result`：原始字幕、梳理结果、技术提取、中英文对照四区块。
- 单任务与批量子任务均可查看；`localStorage` 缓存 api_key/cookie。
- **验收标准**：解析完成后结果页四区块均有内容（非空白/非 500）。

### F9 导出（四页面 × 多格式 + 一键全部 zip）
- 章节 `SECTION_LABEL`：`raw=原始字幕` / `summary=梳理结果` / `tech=技术提取` / `bilingual=中英文对照`。
- 每章节格式组合：
  - `raw`：`txt, srt, xlsx, pdf`
  - `summary`：`txt, md, xlsx, pdf`
  - `tech`：`txt, xlsx, pdf`
  - `bilingual`：`txt, xlsx, pdf`
- 单章节导出：`GET /api/job/{job_id}/export?section=&format=` 返回单文件流。
- 一键全部：`POST /api/export-all`（body `{job_id}` 或 `job_id+section`）→ 打包 zip，文件名 `视频名_页面名.格式`。
- **验收标准**：`format` 取 `txt/srt/md/json/xlsx/pdf` 均返回对应 MIME；`/api/export-all` 返回 200 + zip；zip 内每个章节按上表枚举格式齐全。

### F10 上传本地文件 `/api/upload`
- `multipart` 上传音频/视频，走与 URL 相同的 `run_pipeline`（含 `asr_model/llm_model/fallback_model` 透传）。
- **验收标准**：上传合法音视频 → 200 + `job_id`；时长超 `MAX_AUDIO_SEC` 被拒/裁剪提示。

### F11 健壮性（限流 / 并发 / 降级）
- 单 IP 限流 `IP_LIMIT_PER_HOUR=20`；全服务并行 `GLOBAL_CONCURRENCY=4`，超出返回 `429`。
- `REDIS_URL` 配置 → Redis 存储（多副本并发计数自愈）；留空 → 内存降级（仅单进程）。
- **验收标准**：同一 IP 超 20 次/小时返回限流；并行超 4 返回 429。

### F12 配置 / 设置页
- 「⚙ 设置」页（`page-settings`）展示服务端配置/说明（旧 `model` select 已移除，模型选择迁至新建解析页三下拉框）。
- **验收标准**：设置页可打开且无残留旧模型下拉框导致的 JS 报错。

---

## 5. 验收测试用例（可直接执行）

> 标注 `curl` 的为接口级用例，可在有后端的环境下跑；标注「手动」需真实 Key + 外网。

### A. 启动与基础（P0）
- **A1 健康检查**：`curl -s -m5 http://127.0.0.1:8000/api/health` → 期望 `200`。
- **A2 模型接口**：`curl -s http://127.0.0.1:8000/api/models` → 期望 JSON 含 `asr`(4) / `llm`(3) / `fallback`(7)。
- **A3 首页含三下拉框**：`curl -s http://127.0.0.1:8000/ | grep -oE 'id="(asrModel|llmModel|fallbackModel)"'` → 三个 id 均应出现；且 `modelPanel` 的 style 含 `display:block`。

### B. 链接探测（P0/P1）
- **B1 空 URL**：`/api/resolve?url=` → `{kind:"unknown",count:0}`。
- **B2 合法 BV**（手动）：`/api/resolve?url=https://www.bilibili.com/video/BVxxxx` → `kind:"video"`，`count≥1`，`items` 含 `title`。
- **B3 番剧整季**（手动）：`/api/resolve?url=<番剧url>` → `kind:"bangumi"`，`count`=集数，`items` 每集有 `title/duration`。

### C. 单视频解析（P0，手动）
- **C1 提交**：`curl -s -X POST http://127.0.0.1:8000/api/process -H 'Content-Type: application/json' -d '{"url":"<BV>","api_key":"<KEY>"}'` → `200` + `job_id`。
- **C2 查询**：`curl -s http://127.0.0.1:8000/api/job/<job_id>` → 轮询至 `status=done`，`result` 非空。
- **C3 缺 URL**：`curl -s -X POST .../api/process -d '{}'` → `400`。

### D. ASR 提速（P0，手动，需 Key+外网）
- **D1 1 小时视频**：解析后从日志/计时确认总耗时 <3 分钟（典型 ≈128s）。
- **D2 静音切除生效**：`ASR_SILENCE_REMOVE=1`（默认）时有效语音时长应明显小于原始时长（日志可比对）。

### E. 模型选择（P0）
- **E1 前端三下拉框可见**：浏览器打开 `http://127.0.0.1:8000` → 「🚀 新建解析」页显示 🎙/💬/🔁 三个下拉框，且选项来自 `/api/models`。
- **E2 指定语音模型优先**（手动）：提交 `asr_model=fun-asr-mtl-realtime`，后端日志含 `DashScope 实时识别(fun-asr-mtl-realtime)`。
- **E3 回退**（手动）：把首选模型设为已过期模型 → 任务仍成功（自动跳下一个）。

### F. 批量（P1，手动）
- **F1 整季批量**：`/api/process` 带 `batch:true` + 番剧 URL → 返回 `batch_id` 与 `children` 数=集数。
- **F2 子集勾选**：`selected:[0,2]` → 仅 2 个子任务。
- **F3 进度页每集详情**：结果/进度页列出每集标题、状态、进度。

### G. 导出（P0）
- **G1 单格式**：`/api/job/<id>/export?section=raw&format=srt` → `Content-Type: application/x-subrip`，内容含 `00:00:00,000 -->` 时间轴。
- **G2 md 仅 summary**：`section=summary&format=md` → `text/markdown`；`section=raw&format=md` → 应 4xx/提示不支持（raw 不支持 md）。
- **G3 一键全部**：`curl -s -X POST .../api/export-all -d '{"job_id":"<id>"}' -o all.zip && unzip -l all.zip` → zip 含各章节按 §4-F9 枚举格式齐全。

### H. 上传（P1，手动）
- **H1 合法音视频**：`curl -F "file=@clip.mp4" .../api/upload` → `200` + `job_id`。
- **H2 超长**：时长 `>MAX_AUDIO_SEC` 的素材 → 被拒或裁剪提示。

### I. 限流（P1）
- **I1 单 IP 超频**：同一 IP 连续 >20 次 `/api/process`（1 小时内）→ 限流响应。
- **I2 并行超限**：并发 >`GLOBAL_CONCURRENCY(4)` → `429`。

---

## 6. 回归测试清单（每次发布必跑）

- [ ] **R1** `/api/health` = 200（服务起得来）。
- [ ] **R2** 首页三下拉框 `asrModel/llmModel/fallbackModel` 可见（CSS 修复未回退）。
- [ ] **R3** `/api/models` 返回 asr=4 / llm=3 / fallback=7（模型列表未改坏）。
- [ ] **R4** 单视频解析端到端成功，`/api/job/{id}` 到 `done`，结果四区块非空。
- [ ] **R5** ASR 1 小时视频 <3 分钟（提速未回退）。
- [ ] **R6** 四章节导出格式齐全（raw/summary/tech/bilingual 各自格式组合见 §4-F9）。
- [ ] **R7** `/api/export-all` 返回可用 zip（一键导出未坏）。
- [ ] **R8** 批量整季 `children` 数 = 集数，进度页显示每集详情。
- [ ] **R9** 缺 `url` 返回 400；空 `resolve` 返回 unknown。
- [ ] **R10** 启动脚本仍 `unset` 代理（直连未回退）。
- [ ] **R11** 指定 `asr_model` 时该模型被优先使用（模型透传未坏）。
- [ ] **R12** 设置页打开无 JS 报错（旧 model select 已清理）。

---

## 7. 已知限制（避免误报为缺陷）

1. **内存 store 重启即丢**：未配 `REDIS_URL` 时任务状态在重启后丢失；导出必须带 `job_id`。
2. **批量 children ≠ 单任务**：单视频任务 `kind=job, children=[]` 本就不列每集详情——这是设计，非 bug。
3. **线上 CloudBase 滞后**：默认域名 `video-subtitle-304233-9-1475154132.sh.run.tcloudbase.com` 为 2026-08-28 旧构建，缺下拉框/提速/CSS 修复；验收应以本地 `127.0.0.1:8000` 为准，或先重部署。
4. **ASR 并发硬上限 20**：账号 `Throttling.RateQuota` 限制，调大 `ASR_MAX_CONCURRENCY` 需先提 DashScope 配额，否则被限流。
5. **代理必须直连**：后端 `unset` 代理；若有人改启动逻辑重新引入 `HTTP_PROXY`，bilibili/dashscope 会 `ProxyError`。
6. **手动/集成用例需真实凭据+外网**：标注「手动」的用例无法纯离线 CI 跑，需人工在已配 Key 的环境执行。

---

## 8. 测试数据 / 账号准备

- **DashScope Key**：`.env` 的 `DASHSCOPE_API_KEY`（ASR + 通义千问共用）；或前端「新建解析」页填写（存 localStorage）。
- **B 站测试素材**：1 个短视频 BV（<5min，验 C/D）、1 个整季番剧 URL（验 F/B3）、1 个多分P UGC（验 B2/F2）。
- **本地文件**：1 段 mp4/音频（验 H）。
- **Redis（可选）**：`REDIS_URL=redis://...` 启多副本验证共享存储；否则内存降级。
- **限流压测**：同一 IP 循环提交 >20 次验 I1；并发 >4 验 I2（注意会真产生 DashScope 账单，建议用 mock Key 或低配额账号）。

---

_文档生成：2026-08-30，基线 `ec04855`。与 handover.md 配套使用；handover 偏"怎么接手/坑"，本文件偏"验什么/怎么验"。_
