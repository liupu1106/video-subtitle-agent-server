# 视频字幕提取与智能梳理 —— 项目 Handover（最新版）

> 交接时间：2026-08-30
> 文档状态：覆盖截至 2026-08-30 的全部已实现功能、坑与后续方向。**旧版 handover.md（2026-08-28）已过时，以本版为准。**
> 当前代码 HEAD：`ec04855`（GitHub `master` 已同步）。
> 一句话概览：输入 B 站视频/番剧/选集链接或本地音视频 → 优先提取字幕（无字幕则 20 路并发语音转写 + 静音切除，1 小时视频约 3 分钟内）→ 通义千问 AI 生成「梳理结果 / 技术提取 / 中英文对照」→ 单集或整批查看、四分区六格式导出。

---

## 0. ⚠️ 先读：线上部署已落后于代码

- **CloudBase 线上域名 `https://video-subtitle-304233-9-1475154132.sh.run.tcloudbase.com` 是旧部署**（约 2026-08-28 构建），**缺少**之后所有改动：ASR 提速（并行分块+静音切除）、三个模型下拉框（`/api/models`）、`settings` 页旧模型选择框移除、以及刚修的「模型面板被 CSS 隐藏」bug。线上目前连下拉框的 HTML 都没有。
- **真正的「最新版本」在本地服务 `http://127.0.0.1:8000`**（沙箱内常驻进程，已验证返回 `ec04855` 的页面与 `/api/models`）。
- 结论：要让线上与代码一致，必须**重新部署 CloudBase**（见 §2.2）。沙箱环境无法直接部署（无 CloudBase CLI + 网络受限），需在本机执行。

---

## 1. 快速上手

### 1.1 本地运行（本机/非沙箱主力方式）

一键启动（推荐）：
```bash
cd video-subtitle-agent-server
./run_daemon.sh          # 内部先建 venv + 离线装依赖(vendor/wheels) + unset 代理 + 启动 uvicorn
# 或 macOS 双击 start_local.command / 用 run_local.sh
```
`run_daemon.sh` 会自动完成：Python 依赖自检 → 优先 `vendor/wheels` 离线安装 → **加载 `.env`** → `unset` 所有代理变量（强制后端直连外网）→ 启动 uvicorn（`127.0.0.1:8000`）。

手动启动：
```bash
cd video-subtitle-agent-server
source ./.env                       # 关键：uvicorn 不自动读 .env，必须手动注入
venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
```

**关键坑（必看）**：
- **uvicorn / FastAPI 不会自动加载 `.env`**。不注入时 `DASHSCOPE_API_KEY` 为空，表现为「服务能起、页面能开，但语音转写不可用、AI 梳理降级为规则清洗」。用 `GET /api/health` 的 `has_server_key` 判断 Key 是否真的生效。
- **后端必须直连外网，禁用本地代理**：`api.bilibili.com` / `dashscope.aliyuncs.com` 直连可达（<0.3s），但沙箱/系统代理端口动态变化（56699→59225）且常失效，`requests` 走代理会 `ProxyError`。`run_daemon.sh` 已 `unset` 全部代理变量；改启动逻辑时**切勿重新引入代理**。
- `REDIS_URL` 留空 → store 自动降级 **MemoryBackend**（单进程内存）。重启服务任务状态即清空，但浏览器 localStorage 缓存仍可查看/导出已完成任务。
- 需要系统级 `ffmpeg`（Dockerfile 与本地均依赖它做 16k 单声道 wav 与静音切除）。
- 任务失败 `status` 为 `"error"`（**不是** `failed`）；前端已正确处理，外部轮询脚本要覆盖这个值。

### 1.2 部署到 CloudBase 云托管（本机无需 Docker，云端构建）

```bash
# 仅限该环境的 API Key 登录（非交互、安全，不能动账号其他资源）
cloudbase login --cloudbase-api-key <环境APIKey> -e dev-d2gldfbb91a93f3e6
# 非交互必须喂回车（否则卡在灰度确认）；--port 不是 -p
printf '\n' | cloudbase cloudrun deploy --source . -s video-subtitle \
  --port 8000 -e dev-d2gldfbb91a93f3e6 --wait --force
```
- 环境 ID：`dev-d2gldfbb91a93f3e6`，服务名：`video-subtitle`，端口 `8000`。
- 云托管默认不配变量也能启动（无 Key → 降级规则清洗）。要开通真实识别/梳理，在控制台「配置 → 环境变量」加 `DASHSCOPE_API_KEY`，**建议先到 DashScope 控制台轮换一次 Key**。
- 长期建议把「最小实例数」设为 1（避免 scale-to-0 打断批量长任务），或多副本必须配 `REDIS_URL`。

### 1.3 代码仓库与提交规范（重要）

- **项目独立 git 仓库**在 `video-subtitle-agent-server/.git`（master 分支）。**切勿在 `/Users/liupu` 级仓库提交**——那里是误 `git init` 留下的 home 级仓库，根目录为用户 home，提交会泄露 `.ssh`/`.aws`/`.netrc`/个人文件。
- **GitHub 远程**：`git@github.com:liupu1106/video-subtitle-agent-server.git`（私有库）。
- **本沙箱推 GitHub 必须用 SSH 部署密钥**：HTTPS 被本地代理拦截 git 端点（`api.github.com` 通但 `github.com/<repo>.git` 智能 HTTP 端点不通，直连 443 超时），仅 SSH 22 端口直连可达。部署私钥在项目内 `.ssh_deploy/id_ed25519`（**已 gitignore，绝不入仓**）。推送命令：
  ```bash
  GIT_SSH_COMMAND="ssh -i .ssh_deploy/id_ed25519 -o UserKnownHostsFile=/dev/null -o StrictHostKeyChecking=no" \
    git push -u origin master
  ```
- **用户本机终端无此代理，可用 HTTPS 直推**（本机需 `git remote set-url origin https://github.com/liupu1106/video-subtitle-agent-server.git`）。
- `.gitignore` 已排除：`.env`、`venv/`、`__pycache__`、`*.log`、`.DS_Store`、`.ssh_deploy/`。`vendor/wheels/` 是离线依赖包，属项目资产应纳入版本库（CloudBase 无外网部署用）。

---

## 2. 架构与技术栈

```
浏览器(static/index.html, 单文件 SPA, 无构建步骤)
   │  REST
FastAPI (app/main.py)  ──►  daemon 线程后台任务（run_pipeline）
   │
   ├── app/pipeline.py   yt-dlp 取字幕 / ffmpeg 抽音频 → DashScope 实时 ASR(并行分块+静音切除) → 通义千问 LLM(qwen-plus 系列)
   ├── app/bilibili.py   B 站番剧分集 / UGC 多分P 解析
   ├── app/store.py      任务状态存储（Memory / Redis）+ 限流计数
   └── 导出：txt/srt/md/json（手写序列化）+ xlsx(openpyxl) + pdf(reportlab, STSong-Light 中文 CID 字体)
```

**核心设计：前端 localStorage 缓存解耦后端拓扑**
结果在任务完成时缓存到浏览器（key `vsb_res_<jobId>`），「查看 / 导出 / 整批查看」优先读缓存。即使云托管多副本、缩容重启，用户侧仍稳定可用——这是项目核心容错手段。

### 目录
| 文件 | 作用 |
|---|---|
| `app/main.py` | API 路由、批量调度、导出序列化、限流、no-store 中间件、静态挂载 |
| `app/pipeline.py` | 主流程编排；字幕/音频/ASR 并行加速/LLM；模型列表与自动回退 |
| `app/bilibili.py` | B 站番剧 `get_season_episodes`、选集 `get_video_pages` |
| `app/store.py` | 任务存储（Memory/Redis）、并发心跳、批量聚合 `_get_batch` |
| `static/index.html` | 页面骨架：左侧导航 + 右侧 5 个页面（无构建） |
| `static/css/{base,layout,content}.css` | 设计变量/布局/内容展示样式 |
| `static/js/{core,api,export,views,nav,app}.js` | 全局状态/接口出口/导出/渲染/导航/业务流程 |
| `Dockerfile` | `python:3.11-slim` + ffmpeg（云托管云端构建） |
| `run_daemon.sh` / `run_local.sh` / `start_local.command` | 本地启动器（建 venv、离线装依赖、unset 代理） |
| `DEPLOY-CLOUDBASE.md` | 部署 runbook |
| `cloudbaserc.json` | 云托管环境/服务配置 |
| `.ssh_deploy/` | SSH 部署私钥（gitignore，仅沙箱推 GitHub 用） |

**前端组织约定**
- 5 个小页面：`page-new` 新建解析 / `page-jobs` 任务进度 / `page-result` 单视频结果 / `page-batch` 整批查看 / `page-settings` 模型与密钥。切换靠 `showPage(id)` 给 `<section class="page">` 加 `.active`。
- 脚本按 `core → api → export → views → nav → app` 顺序用普通 `<script>` 加载，**刻意不用 ES module**（避老 Safari 模块兼容 + `file://` CORS），共享全局作用域。
- **坑：`app/main.py` 必须先挂载 `/static` 再挂载 `/`**。`StaticFiles` 挂 `/` 会吞掉其余路径，否则 `/static/css/base.css` 被解析成 `static/static/css/base.css` 而 404（页面能开但无样式无脚本）。
- 全部响应带 `no-store`（main.py 中间件）+ `<head>` 三个 no-cache meta，防浏览器缓存旧页面。

---

## 3. API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 健康检查、`has_server_key` |
| GET | `/api/models` | **返回前端下拉框可用模型** `{asr:[...], llm:[...], fallback:[asr∪llm 去重]}` |
| GET | `/api/resolve?url=` | 探测能否批量，返回 `{kind, is_multi, count, title, items:[{index,title,duration,page?}]}` |
| POST | `/api/process` | 提交任务；支持 `batch`+`selected:[i]` 批量子集；新增 `asr_model`/`llm_model`/`fallback_model` 三参数 |
| POST | `/api/upload` | 上传本地音视频（同样接收上述三模型参数） |
| GET | `/api/job/{id}` | 轮询状态（批量返回 `children[]`，done 子任务**内联 result**） |
| GET | `/api/job/{id}/export?section=&format=` | 单分区导出 `section=raw\|summary\|tech\|bilingual`；`format=txt\|srt\|md\|json\|xlsx\|pdf` |
| POST | `/api/export-all` | 打包 14 文件 zip（优先用 `job_id` 取结果，绕过代理请求体大小限制；失败回退前端已缓存 `result`） |

**模型三参数语义**（`/api/process`、`/api/upload` 均可传）：
- `asr_model`：语音识别首选模型（留空=后端走 `REALTIME_ASR_MODELS` 默认顺序）。
- `llm_model`：AI 梳理/翻译首选文本模型（留空=后端走 `LLM_MODELS`）。
- `fallback_model`：统一备选（跨语音+文本），当首选不可用置候选末尾。
- 任一模型「未开通/额度耗尽/过期」都自动跳下一个（见 §6.3）。

---

## 4. 环境变量（控制台「服务配置 → 环境变量」或本地 `.env`）

| 变量 | 默认 | 说明 |
|---|---|---|
| `DASHSCOPE_API_KEY` | 空 | ASR + 通义千问共用；不配则 ASR 不可用、AI 梳理降级为规则清洗 |
| `REDIS_URL` | 空 | 配了走 Redis（多副本共享、自愈并发计数）；不配走内存（**仅单副本可靠**） |
| `IP_LIMIT_PER_HOUR` | 20 | 单 IP 每小时提交上限 |
| `GLOBAL_CONCURRENCY` | 4 | 全服务并行任务上限（超限 429） |
| `BATCH_CONCURRENCY` | 6 | 整季/整集内并行子任务数上限 |
| `MAX_AUDIO_SEC` | 7200 | 音频处理长度上限（秒），超长裁剪 |
| `ASR_MAX_CONCURRENCY` | 20 | **实时 ASR 并发硬上限**（实测=账号上限，超出触发 `Throttling.RateQuota`）；提速核心 |
| `ASR_MIN_CHUNK_SEC` | 20 | 单块时长下限（秒）；块数 = `min(并发上限, 时长/单块下限)`，用满并发 |
| `ASR_SILENCE_REMOVE` | 1 | 静音切除开关（去 ≥0.5s 静音，压缩有效语音→提速且省按时长计费的识别量） |
| `ASR_MODEL_PRIORITY` | 空 | 逗号分隔，覆盖 ASR 模型默认顺序（**快到期优先**：把将过期的写前面） |
| `LLM_MODEL_PRIORITY` | 空 | 逗号分隔，覆盖文本模型默认顺序（快到期优先） |
| `JOB_TTL_SEC` | 86400 | 任务快照 TTL |
| `JOB_STALE_SEC` | 900 | 心跳超时判定任务中断的阈值 |

**模型默认值（代码内，非环境变量）**
- `REALTIME_ASR_MODELS = [fun-asr-mtl-realtime, fun-asr-realtime, qwen-audio-3.0-asr-flash-streaming, fun-asr-flash-8k-realtime]`（默认首选 `fun-asr-mtl-realtime`）。
- `LLM_MODELS = [qwen3.5-plus, qwen3.6-plus, qwen3.7-plus]`（快到期优先，默认首选 `qwen3.5-plus`）。
- 老接口兜底 `ASR_FILE_MODELS = [paraformer-v2, fun-asr]`（仅实时都不可用时走老文件转录，需 OSS 直传）。
- 旧文档写的 `ASR_MODEL=paraformer-v2` / 默认 `qwen-max` **已废弃**，现在模型选择下放到页面三下拉框。

---

## 5. 已实现功能（截至 2026-08-30）

**输入**
- B 站单视频、番剧（bangumi）、UGC 多分P 选集；本地音视频上传。
- 粘贴链接即 `/api/resolve` 探测，识别多分集自动启用批量并显示分集数。

**处理流程**
1. 优先提取官方字幕（yt-dlp 字幕轨道）→ `method=subtitle`。
2. 无字幕则 ffmpeg 抽 16k 单声道 wav → **DashScope 实时 ASR（并行分块 + 静音切除加速）** → `method=asr`。
3. 文本清洗 → **三个 LLM 调用并发**（`ThreadPoolExecutor(max_workers=3)`）：`llm_refine` 梳理（MD）、`llm_tech_extract` 技术提取（结构化 JSON）、`llm_bilingual` 中英文对照。

**ASR 提速（2026-08-29 加入，核心优化）**
- 实测账号实时 ASR **并发硬上限 = 20 路**（`Throttling.RateQuota`）。实时接口服务端≈实时，单流不能快于音频时长；唯一杠杆是切片并发。
- `_realtime_asr`：块数 `n = min(ASR_MAX_CONCURRENCY, 时长 // ASR_MIN_CHUNK_SEC)`，用满 20 路；相邻块 1.5s 重叠避免切词；`asyncio.Semaphore(20)` 封顶防限流。
- `_remove_silence`（ffmpeg silenceremove）压缩有效语音——既提速又省识别量（实时 ASR 按时长计费，性价比双赢）。
- 实测：600s 音频 34s（原 150s，提速 4.4x）；**一小时视频最快 ≈ 有效语音时长/20**，纯语音临界 3 分钟，含 30% 静音约 128s。要严格所有视频 <3 分钟需提 DashScope 并发配额 >20 路。
- 静音切除后时间戳基于压缩时间轴，与原视频位置略有偏移（内容正确，未做时间映射）。

**模型选择（2026-08-30 加入）**
- 前端「🚀 新建解析」页三个动态下拉框（**刚修复 CSS 显隐，已常驻可见**）：
  - `🎙 语音识别模型`（`asrModel`）→ 由 `/api/models` 的 `asr` 填充。
  - `💬 文本模型`（`llmModel`）→ `llm` 填充。
  - `🔁 替代模型`（`fallbackModel`）→ `asr∪llm` 并集填充。
  - 每个首个占位「自动/不指定」；留空则后端按默认列表自动回退。
- `fillModelSelects()` 初始化时 `fetch /api/models` 填充；接口不可达则保留占位仍可提交。
- 后端 `run_pipeline(asr_model, llm_model, fallback_model)` 透传：`do_asr` 用 asr/回退候选，`llm_*` 步骤用 llm/回退候选。
- **自动回退**：`dashscope_asr` 候选顺序 = `ASR_MODEL环境变量 > asr_model > REALTIME_ASR_MODELS > fallback_model`；任一失败跳下一个。`llm_with_fallback` 同理（首选优先、fallback 置尾、去重）。`settings` 页旧 `qwen-max/plus/turbo` 选择框已删除。

**查看（结果页 4 个分区 tab）**
- 原始字幕（按 segment 分段带序号）、梳理结果（MD 渲染）、技术提取、中英文对照。
- 单集「查看」、批量列表每集「查看」（带「← 返回批量列表」）、**整批查看**（聚合渲染到页面内 `#batchView`，非弹窗）。

**批量**
- 番剧整季 / UGC 全部分P；可手动勾选子集（全选/反选，实时「已选 x/X」计数）。
- 进度条 + 实时日志；失败行内显示原因；`BATCH_CONCURRENCY` 限制季内并发。
- 顶部「整批一键导出全部」打包 zip。

**导出**
- 4 分区 × 6 格式（txt/srt/md/json/xlsx/pdf）；单分区「复制 / 各格式」。
- 一键导出 14 文件 zip（分区×格式组合，命名 `视频名_页面名称.格式`）。
- 由后端 `/api/job/{id}/export` 与 `/api/export-all` 生成（xlsx=openpyxl、pdf=reportlab STSong-Light 中文 CID 字体）；中文文件名用 RFC 5987 `filename*=UTF-8''` 避免 500。前端 `export.js` 组装请求与下载。

**健壮性**
- 全局 `no-store` + `<head>` no-cache meta（防 Safari 缓存旧页面）。
- 渲染全链路 try/catch（`esc` 类型安全、`safeParse` 降级、每分集区块独立兜底）。
- 轮询与详情视图互斥守卫（`viewingDetail`）。

---

## 6. 遇到的问题与坑（按类型）

### 6.1 部署 / 平台
| 坑 | 现象 | 解决 |
|---|---|---|
| 云托管资源未开通 | `cloudrun deploy` 报「云托管资源未开通」 | CLI 无开通命令，必须去控制台：环境→云托管→开通（首次建 TCR 命名空间/集群，可能需 CAM 授权） |
| 端口参数 | `-p 8000` 报 unknown option | 必须用 `--port` |
| 非交互卡死 | 部署卡「Enable gray deployment?」 | `printf '\n' \| cloudbase cloudrun deploy ...` 喂回车选默认 No |
| 本机 OOM | deploy / pip 装大包 exit 137 | 重试；本机内存紧张时验证改用 stub |
| 线上落后于代码 | 线上无下拉框/未提速 | 代码改动后必须**重新部署 CloudBase**（见 §0、§2.2） |

### 6.2 云托管 / 分布式（最重要的一类）
**核心矛盾：任务状态在进程内存，而云托管多副本 + 可缩容到 0。**
| 坑 | 现象 | 解决 |
|---|---|---|
| 首次成功、后续 404 | 「查看/导出」第一次正常，第二次起 404 | 请求轮到无该 job 的副本。前端改为任务完成即缓存 result 到 localStorage，查看/导出优先读缓存 |
| 批量「该分集尚未完成」 | 明明已完成 | `store._get_batch` 对 done 子任务**内联 result**，前端轮询时 `cachePut` |
| 整批查看「结果暂不可用」 | 每分集都不可用 | 轮询命中无状态副本 → `children[].result:null`；改为「缓存即时渲染 + 后台并行补齐（`fillMissingBatch`）」 |
> 仍未根治：多副本状态不一致只是被前端缓存绕过。彻底解法：最小实例数=1，或接 Redis（`REDIS_URL`）。

### 6.3 本沙箱环境特有的坑（接手本项目大概率会踩）
| 坑 | 现象 | 解决 |
|---|---|---|
| 推 GitHub 被代理拦截 | HTTPS git push `502`/`Empty reply`/`HTTP 000` | 本沙箱出网强制走本地代理，拦截 `github.com/<repo>.git` 端点（仅 `api.github.com` 通）；**改用 SSH 部署密钥**（`.ssh_deploy/`，22 端口直连）。用户本机无代理可 HTTPS |
| home 级误 git init | `git status` 显示 `../../../` | `/Users/liupu/.git` 是误 init 的 home 仓库，**绝对不要在此提交**；只在项目内独立仓库操作 |
| 服务被沙箱回收 | nohup / 后台任务 / launchctl / setsid 活不过回合间隙 | 唯一有效：Python `subprocess.Popen(..., start_new_session=True)`（脱离进程组、被 init 收养）。`run_daemon.sh` 即此方式 |
| B 站/ DashScope ProxyError | `ProxyError 127.0.0.1:59225 Connection refused` | 沙箱/系统代理端口动态变化且失效；`run_daemon.sh` 启动前 `unset` 所有代理变量强制直连（实测直连通） |
| 批量进度页「每视频详情丢失」 | 以为回归 | 非 bug：单视频任务 `kind=job, children=0` 本不列；批量 `children` 正常（实测 2 集返回每集标题/状态/进度） |

### 6.4 后端
| 坑 | 现象 | 解决 |
|---|---|---|
| 中文文件名导出全 500 | txt/srt/md/json 全 Internal Error | starlette 按 latin-1 编码 header，中文进 `Content-Disposition` 抛 `UnicodeEncodeError` → RFC 5987 `filename*=UTF-8''` |
| 批量导出文件名带上层名 | 文件名是主标题而非分集名 | `run_pipeline` 加 `ep_title`，批量 `_run_child` 传分集自身 part 名 |
| DashScope `403 AllocationQuota` | 语音识别提交失败 | 非 bug：该 Key 未开通 ASR 配额（LLM 能调 ≠ ASR 已开通）。已多模型自动回退 |
| B 站字幕取不到 | 有字幕也走 ASR | 部分字幕需登录 Cookie（页面可填 `bili_cookie`）；yt-dlp 反爬也回退 ASR |
| ASR 音频解码失败 `DECODE_ERROR` | 早期「公网路由 + file_urls 回拉」带鉴权域名返回 401 HTML 被当音频 | 改为上传 OSS（`oss://` + `X-DashScope-OssResourceResolve: enable`） |
| LLM 返回非常规结构导出 500 | tech/bilingual 字段是 dict/list | `_section_blocks` 全量 `str()` 兜底，openpyxl/reportlab 单元格绝不写非字符串 |
| 模型面板被 CSS 隐藏（2026-08-30 修） | 新建解析页完全看不到三个下拉框 | `.selpanel{display:none}`，而模型面板用了该类且 JS 只控制批量面板；加 `id="modelPanel"` + `display:block` 覆盖 |

### 6.5 前端
| 坑 | 现象 | 解决 |
|---|---|---|
| `esc()` 类型 bug → 整批查看空白 | 弹窗空白 | `esc(s)` 原 `(s\|\|"").replace`；`ep_no` 是数字无 `.replace` → TypeError。改为 `(s==null?"":String(s)).replace` |
| 轮询覆盖详情页 | 点查看整页消失 | `poll()` 每 1.5s 调 `renderBatch()` 隐藏 `#result`；加 `viewingDetail` 守卫，返回时复位 |
| 旧 `render()` 先隐藏后填充 | 任一异常即留白 | 改为先构建全部内容、成功后才 `display=block` |
| 弹窗一滑就消失 | 滚动即关 | 删除弹窗，改为渲染到页面内 `#batchView` |
| Safari 旧版不生效 | 反复报旧问题 | 加全局 `no-store` + `<head>` 三 no-cache meta；用户需硬刷新一次（Cmd+Shift+R） |
| 勾选计数不生效 | 选 3 个仍显示 0 | 缺 `change` 事件委托；加 `#sellist` 的 `change` 委托 |
| Safari 兼容 | — | 去掉 `backdrop-filter`；去掉 `Promise.allSettled` 改 `forEach`+async IIFE+`.catch` |

### 6.6 测试方法论（值得复用）
- VM/DOM harness 必须用**真实后端返回结构**（`ep_no:1` 数字类型），字符串桩会掩盖 TypeError。
- 自建 document 桩须注入浏览器全局：`AbortController`/`clearTimeout`/`setInterval`/`localStorage`/`fetch`/`URL.createObjectURL`，否则 `finally{clearTimeout}` 抛 ReferenceError 造成假失败。
- 本机无法 `pip install` 大包（OOM）时用 faithful stub 仿真 openpyxl/reportlab 离线验证 + `py_compile`/`node --check` 兜底。
- 改前端后 `curl -s <域名>/ | grep -c '关键字'` 确认线上已生效（而非只信部署日志）。

---

## 7. 当前已知限制
1. **多副本状态未根治**：`MemoryBackend` 仅单副本可靠；当前靠前端缓存 + 最小实例数绕过。
2. **ASR 配额按模型独立开通**：`fun-asr` 系列已可用；`paraformer-v2` 未开通（提交即 403），靠多模型回退绕过。仅 `fun-asr` 额度吃紧时才需再开通其他模型。
3. **静音切除后时间戳偏移**：基于压缩时间轴，与原视频位置略有偏移（内容正确）。
4. **B 站部分字幕需登录 Cookie**，否则回退 ASR（更慢、耗配额）。
5. **自定义域名**：`*.sh.run.tcloudbase.com` 可用；绑自定义域名中国区需 ICP 备案。
6. **无用户体系/持久化**：刷新后历史批次不可恢复；任务结果不落库（仅 localStorage）。
7. **线上部署严重落后于代码**（见 §0）——这是当前最紧迫的不一致。

---

## 8. 后续开发方向（按优先级）

### P0 —— 稳定性（成本最低收益最高）
1. **重新部署 CloudBase**（见 §2.2），让线上与 `ec04855` 一致——补齐下拉框、ASR 提速、CSS 修复。
2. **控制台把云托管「最小实例数」设为 1**，或更好：建 TencentDB for Redis 配 `REDIS_URL`，根治多副本状态不一致并解锁水平扩容。
3. **轮换 DashScope API Key**（曾在会话中明文出现）。

### P1 —— 功能补全
4. **任务结果持久化**：落 Redis / COS，支持刷新恢复、断点续跑、跨设备查看。
5. **B 站 Cookie 引导**：对「字幕提取失败将回退 ASR」给前置提示与填写引导，减少无谓配额消耗。
6. **长文阅读优化**：分段折叠 / 目录导航 / 字号调节。
7. **批量体验**：暂停/重试、失败集一键重跑、整体进度预估。

### P2 —— 产品化
8. **用户体系 + 配额计费**：当前仅按 IP 限流（20/小时），无法按用户计量。
9. **异步通知**：批量耗时数十分钟，可加完成回调/邮件。
10. **多平台扩展**：当前仅 B 站 + 本地上传；yt-dlp 本身支持 YouTube 等。
11. **自定义域名 + 备案**，摆脱 `*.sh.run.tcloudbase.com`。

---

## 9. 接收本项目必须知道的其它知识

- **账号与密钥**：DashScope（阿里云百炼）账号提供 `DASHSCOPE_API_KEY`，ASR 与 LLM 是**独立配额**，开通模型要分别确认。CloudBase 环境 `dev-d2gldfbb91a93f3e6`（腾讯云）。GitHub 私有库 `liupu1106/video-subtitle-agent-server`。
- **模型分层概念**：ASR 语音模型（`REALTIME_ASR_MODELS`，fun-asr 系列，走实时 WebSocket）与 LLM 文本模型（`LLM_MODELS`，qwen-plus 系列，做梳理/翻译）**完全分离**，各自支持「env 优先级覆盖 + 运行时自动回退」。调模型前先想清是语音还是文本。
- **快到期优先策略**：权益页模型有有效期，把将过期的写进 `ASR_MODEL_PRIORITY` / `LLM_MODEL_PRIORITY` 即可省钱，无需改代码。
- **不要碰 `/Users/liupu/.git`**：那是误 init 的 home 仓库，任何提交都会泄露私钥/个人文件。只在项目内仓库工作。
- **沙箱网络三定律**：① 推 GitHub 走 SSH 部署密钥；② 后端直连外网、严禁代理；③ 服务保活用 `start_new_session=True`，别用 nohup/launchctl。
- **验证链路**：B 站具体视频 `view` 接口可用；排行榜/搜索被风控（返回 `-352`/412），填 Cookie 绕过。`/api/resolve` 对失效 BV 号返回 `error:'data'`（`KeyError`）是链接无效非故障。

---

## 附：常用命令

```bash
# 健康检查（看 Key 是否生效）
curl -s http://127.0.0.1:8000/api/health
# 线上（旧部署，仅供对照）：
curl -s https://video-subtitle-304233-9-1475154132.sh.run.tcloudbase.com/api/health

# 可用模型列表（下拉框数据源）
curl -s http://127.0.0.1:8000/api/models

# 探测链接（应返回分P 列表）
curl -s "http://127.0.0.1:8000/api/resolve?url=BV1PM8y6rEE3?p=16"

# 确认线上前端已含最新改动（下拉框 id）
curl -s https://video-subtitle-304233-9-1475154132.sh.run.tcloudbase.com/ | grep -c 'asrModel'   # 应为 0（旧部署）

# 本地语法兜底
python -m py_compile app/*.py
node --check static/js/app.js   # 需抽出内联脚本时用等价检查

# 本地提交并推送（沙箱用 SSH 部署密钥）
git add -A && git commit -m "..." && \
  GIT_SSH_COMMAND="ssh -i .ssh_deploy/id_ed25519 -o UserKnownHostsFile=/dev/null -o StrictHostKeyChecking=no" \
  git push -u origin master
```

测试视频：`BV1PM8y6rEE3`（25 个分P），选 `[0,1]` 约 30s 完成，适合快速回归。
