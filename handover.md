# 视频字幕提取与智能梳理 —— 项目 Handover

> 交接时间：2026-08-28 23:0x
> 当前状态：**生产可用**（CloudBase 云托管常驻容器，公网可访问）
> 一句话：输入 B 站视频/番剧/选集链接 → 自动提取字幕（无字幕则语音转写）→ AI 生成「梳理结果 / 技术提取 / 中英文对照」→ 单集或整批查看、四分区六格式导出。

---

## 1. 快速上手

### 线上地址
- 生产（CloudBase 云托管，公网直连、无令牌）：
  `https://video-subtitle-304233-9-1475154132.sh.run.tcloudbase.com`
- 健康检查：`GET /api/health` → `{"status":"ok","has_server_key":false}`

### 本地运行（当前主力方式）

一键启动（推荐）：
```bash
cd video-subtitle-agent-server
./run_local.sh              # 默认 8000 端口，前台运行，Ctrl+C 停止
PORT=9000 ./run_local.sh    # 换端口
./stop_local.sh             # 停止
```
脚本自动完成：Python 依赖自检 → `ffmpeg` 检查 → **加载 `.env`** → 端口占用检查 → 启动 uvicorn。

手动启动：
```bash
cd video-subtitle-agent-server
pip3 install -r requirements.txt     # 本机 pip 装大包易 OOM(exit 137)，见「坑」章节
set -a; . ./.env; set +a             # 关键：uvicorn 不会自动读 .env，必须手动注入
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

**坑：uvicorn / FastAPI 不会自动加载 `.env`**。不注入环境变量时 `DASHSCOPE_API_KEY` 为空，
表现为「服务能起、页面能开，但 AI 梳理降级为规则清洗、语音转写不可用」。
用 `/api/health` 的 `has_server_key` 可快速判断 Key 是否真的生效。

本地模式要点：
- `REDIS_URL` 留空 → store 自动降级 **MemoryBackend**（单进程内存）。重启服务任务状态即清空，
  但浏览器 localStorage 缓存仍可查看/导出已完成任务。
- 需要系统级 `ffmpeg`（本机 `/usr/local/bin/ffmpeg` 8.1.1 已验证可用）。
- 任务失败时 `status` 为 `"error"`（**不是** `failed`）；前端已正确处理（提示错误并重置），
  外部脚本轮询时要记得覆盖这个状态值。

本地链路实测结论（2026-08-29）：
- 上传自有音频 → ffmpeg 处理 → DashScope 上传 → ASR 提交 → 轮询返回，**全链路跑通**。
- **fun-asr 配额可用**：`paraformer-v2` 报无配额后自动回退 `fun-asr` 并成功提交，
  多模型回退机制在真实环境验证生效（详见「语音识别」章节）。
- B 站解析：给定**具体视频链接**的 `view` 接口可用；但本机 IP 对 B 站**排行榜/搜索**接口被风控
  （返回 `-352` / HTTP 412）。若解析或下载报风控，在页面填入 B 站 Cookie 即可绕过。
- 注意 `/api/resolve` 对失效 BV 号会返回 `error: "'data'"`（`KeyError`），
  这是链接本身无效（B 站 `view` 返回 `-404 啥都木有`），不是服务故障——换有效链接即可。

### 部署（本机无需 Docker，云端构建）
```bash
# 登录（环境 API Key，非交互、仅限该环境）
cloudbase login --cloudbase-api-key <Key> -e dev-d2gldfbb91a93f3e6

# 部署（非交互必须用 stdin 喂回车，否则卡在灰度确认）
printf '\n' | cloudbase cloudrun deploy --source . -s video-subtitle \
  --port 8000 -e dev-d2gldfbb91a93f3e6 --wait --force
```
- 环境 ID：`dev-d2gldfbb91a93f3e6`，服务名：`video-subtitle`，端口 `8000`。
- 当前运行模式 `alwaysScale`（常驻），不会 scale-to-0 中断批量任务。

---

## 2. 架构与技术栈

```
浏览器(static/index.html, 单文件 SPA)
   │  REST
FastAPI (app/main.py)  ──►  threading 后台任务
   │
   ├── app/pipeline.py   yt-dlp 取字幕 / ffmpeg 抽音频 → DashScope ASR → DashScope LLM(qwen)
   ├── app/bilibili.py   B 站番剧分集 / UGC 多分P 解析
   ├── app/store.py      任务状态存储（Memory / Redis）
   └── 导出：txt/srt/md/json（标准库）、xlsx（openpyxl）、pdf（reportlab CID 中文字体）
```

**关键设计：前端 localStorage 缓存解耦后端拓扑**
结果在任务完成时缓存到浏览器（key `vsb_res_<jobId>`），「查看 / 导出 / 整批查看」优先读缓存。
这样即使云托管多副本、缩容重启，用户侧依然稳定可用——这是本项目的核心容错手段（详见「坑」）。

### 目录
| 文件 | 作用 |
|---|---|
| `app/main.py` | API 路由、批量调度、导出序列化、限流 |
| `app/pipeline.py` | 主流程编排、字幕/音频/ASR/LLM |
| `app/bilibili.py` | B 站番剧 `get_season_episodes`、选集 `get_video_pages` |
| `app/store.py` | 任务存储（内存 / Redis），`_get_batch` 会内联 done 子任务 result |
| `static/index.html` | 页面骨架：左侧导航 + 右侧 5 个页面容器（无构建步骤） |
| `static/css/base.css` | 设计变量、reset、通用组件（表单/按钮/卡片/标签/提示） |
| `static/css/layout.css` | 左侧导航 + 右侧内容区布局、页面切换、窄屏响应式 |
| `static/css/content.css` | 内容展示：梳理(.md)/字幕(.rawseg)/对照(.biling)/技术(.techcard)/整批(.ovsec)/批量列表 |
| `static/js/core.js` | 全局状态、工具、`esc`、结果缓存 `vsb_res_*`、错误提示 |
| `static/js/api.js` | 后端接口唯一出口（process / job / resolve / export） |
| `static/js/export.js` | 序列化与导出（txt/md/srt/xlsx/pdf）、一键导出、整批导出 |
| `static/js/views.js` | 渲染与视图切换（render / renderBatch / 整批查看 / 返回） |
| `static/js/nav.js` | 左侧导航切换 `showPage()`、任务进行中提示点 |
| `static/js/app.js` | 业务流程（提交 / 轮询 / 链接探测）+ 事件绑定 + 初始化 |
| `Dockerfile` | `python:3.11-slim` + ffmpeg |
| `DEPLOY-CLOUDBASE.md` | 部署 runbook |

**前端组织约定（重构后）**

- 界面：左侧固定导航栏 + 右侧内容区；内容拆为 5 个小页面 ——
  `page-new` 新建解析 / `page-jobs` 任务进度 / `page-result` 单视频结果 / `page-batch` 整批查看 / `page-settings` 模型与密钥。
  切换靠 `showPage(id)` 给 `<section class="page">` 加 `.active`（见 `static/js/nav.js`）。
- 脚本按 `core → api → export → views → nav → app` 顺序用普通 `<script>` 加载，
  **刻意不用 ES module**：避免老 Safari 的模块兼容与 `file://` CORS 问题，全部共享全局作用域。
- **坑：`app/main.py` 必须先挂载 `/static` 再挂载 `/`**。因为 `StaticFiles` 挂载在 `/` 会吞掉其余路径，
  若不先注册 `/static`，`/static/css/base.css` 会被解析成 `static/static/css/base.css` 而 404
  （表现为页面能打开但完全没样式、没脚本）。
- 默认模型为 **qwen-max**（`static/index.html` 中 `<select id="model">` 的首个 option）。

### API
| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 健康检查、`has_server_key` |
| GET | `/api/resolve?url=` | 探测链接可否批量，返回 `items:[{index,title,duration}]` |
| POST | `/api/process` | 提交任务；`batch=true` + `selected:[i,...]` 走批量子集 |
| POST | `/api/upload` | 上传本地音视频 |
| GET | `/api/job/{id}` | 轮询状态（批量返回 `children[]`，done 子任务**内联 result**） |
| GET | `/api/job/{id}/export` | 单分区导出 `?section=raw|summary|tech|bilingual&format=txt|srt|md|json|xlsx|pdf` |
| POST | `/api/export-all` | 打包 14 个文件为 zip（命名 `视频名_页面名称.格式`） |

### 环境变量（控制台「服务配置 → 环境变量」）
| 变量 | 默认 | 说明 |
|---|---|---|
| `DASHSCOPE_API_KEY` | 空 | ASR + qwen 共用；不配则 ASR 不可用、AI 梳理降级为规则清洗 |
| `REDIS_URL` | 空 | 配了走 Redis（多副本共享）；不配走内存（**仅单副本可用**） |
| `IP_LIMIT_PER_HOUR` | 20 | 单 IP 每小时提交上限 |
| `GLOBAL_CONCURRENCY` | 4 | 全服务并行任务上限（超限 429） |
| `BATCH_CONCURRENCY` | 6 | 批量子任务并发 |
| `MAX_AUDIO_SEC` | 7200 | 音频处理长度上限（秒），超长裁剪 |
| `ASR_MODEL` | `paraformer-v2` | 语音识别首选模型（回退链起点） |
| `LLM 模型` | 页面下拉 `qwen-plus` | 也可 `qwen-turbo` / `qwen-max` |

---

## 3. 已实现功能

**输入**
- B 站单视频、番剧（bangumi）、UGC 多分P 选集链接；本地音视频上传。
- 粘贴链接即自动探测（`/api/resolve`），识别到多分集自动启用批量开关并显示分集数。

**处理流程**
1. 优先提取官方字幕（yt-dlp 字幕轨道）→ `method=subtitle`
2. 无字幕则 ffmpeg 抽音频转 16k 单声道 wav → DashScope 语音转写 → `method=asr`
3. 文本清洗 → **三个 LLM 调用并发**（`ThreadPoolExecutor(max_workers=3)`）：
   - `llm_refine` 梳理结果（Markdown）
   - `llm_tech_extract` 技术提取（技术/功能/国产化等结构化 JSON）
   - `llm_bilingual` 中英文对照

**查看（结果页 4 个分区 tab）**
- 原始字幕（**按 segment 分段**，左侧带序号）、梳理结果（Markdown 渲染 + 排版增强）、技术提取、中英文对照
- 单集「查看」、批量列表每集「查看」（带「← 返回批量列表」）
- **整批查看梳理结果 / 整批查看技术提取**：聚合所有分集，渲染在**当前页**（`#batchView`），非弹窗

**批量**
- 番剧整季 / UGC 全部分P；可手动勾选子集（全选 / 反选，含实时「已选 x/X」计数）
- 进度条 + 实时日志；失败行内显示原因（缺 Key 会明确标注）
- 顶部「整批一键导出全部」打包 zip

**导出**
- 4 分区 × 6 格式；单分区可「复制 / TXT / SRT / Excel / PDF」
- 批量：每集导出 + 整批一键导出（14 文件 zip：`视频名_页面名称.格式`）
- 前端本地生成（txt/srt/md/json 手写序列化，xlsx 用 SheetJS，pdf 用 `window.print()`），缓存缺失时回退服务端

**健壮性**
- 全局 `no-store` 响应头 + `<head>` no-cache meta（防 Safari 缓存旧页面）
- 渲染全链路 try/catch（`esc` 类型安全、`safeParse` marked 降级、每分集区块独立兜底）
- 轮询与详情视图互斥守卫（`viewingDetail`），避免相互覆盖

---

## 4. 遇到的问题与踩到的坑（按类型）

### 4.1 部署 / 平台

| 坑 | 现象 | 解决 |
|---|---|---|
| 云托管资源未开通 | `cloudrun deploy` 报「云托管资源未开通」 | CLI **没有**开通命令，必须去控制台：环境 → 云托管 → 开通（首次会建 TCR 命名空间/集群，可能需 CAM 授权）。`cloudrun list` 返回空表也照样失败 |
| 端口参数 | `-p 8000` 报 unknown option | 必须用 `--port` |
| 非交互卡死 | 部署卡在「Enable gray deployment?」 | `printf '\n' \| cloudbase cloudrun deploy ...` 喂回车选默认 No |
| 本机 OOM | deploy 进程 exit 137 | 重试即可；本机内存紧张时 pip 装大包同样会被杀（exit 137），验证改用 stub |
| EdgeOne 方案被否 | 云函数跑不了长任务 | 最终选 CloudBase 云托管常驻容器，EdgeOne 版无批量能力 |

### 4.2 架构 / 分布式（**最重要的一类**）

**核心矛盾：任务状态存在进程内存，而云托管是多副本 + 可缩容到 0。**

| 坑 | 现象 | 解决 |
|---|---|---|
| 首次成功、后续失败 | 「查看 / 导出」第一次正常，第二次起 404 | 根因不在代码逻辑：请求被轮询到**没有该 job 的副本**。修复走前端——任务完成即缓存 result 到 localStorage，查看/导出优先读缓存 |
| 批量「该分集尚未完成」 | 明明已完成 | 同上；且 `store._get_batch` 现对 done 子任务**内联 `result`**，前端轮询时对每个 done 子任务 `cachePut` |
| 整批查看「结果暂不可用」 | 弹窗每个分集都不可用 | 轮询命中无状态副本 → `children[].result:null`。改为「缓存即时渲染 + 后台并行补齐（`fillMissingBatch`，25s 超时、逐条渐进刷新）」 |

> **仍未根治**：多副本状态不一致只是被前端缓存绕过了。彻底解法见第 6 节（最小实例数=1，或接 Redis）。

### 4.3 后端

| 坑 | 现象 | 解决 |
|---|---|---|
| **中文文件名导致导出全 500** | txt/srt/md/json 全部 Internal Server Error | starlette 按 **latin-1** 编码 header，中文标题进 `Content-Disposition` 抛 `UnicodeEncodeError`。改用 RFC 5987：`filename="ascii降级名"; filename*=UTF-8''<percent-encoded>` |
| 批量导出文件名带更上层名 | 文件名是主标题而非分集名 | `run_pipeline` 加 `ep_title` 参数，批量 `_run_child` 传分集自身 part 名 |
| DashScope `403 AllocationQuota` | 语音识别提交失败 | **非代码 bug**：该 Key 账号未开通语音识别配额。LLM 与 ASR 是**独立配额**（LLM 能调 ≠ ASR 已开通）。已改为**多模型自动回退** |
| B 站字幕取不到 | 有字幕的视频也走 ASR | 部分字幕需登录 Cookie（页面可填 `bili_cookie`）；yt-dlp 签名/反爬也会导致回退 ASR |
| ASR 音频解码失败 | `DECODE_ERROR` | 早期用「本服务公网路由 + file_urls 回拉」，带鉴权域名返回 401 HTML 被当音频解码。改为直传 OSS（`oss://` + `X-DashScope-OssResourceResolve: enable`） |

### 4.4 前端

| 坑 | 现象 | 解决 |
|---|---|---|
| **`esc()` 类型 bug → 整批查看空白** | 弹窗打开但完全空白 | `esc(s)` 原实现 `(s\|\|"").replace(...)`；后端 `ep_no` 是**数字**（`1`），数字没有 `.replace` → TypeError → `paint()` 首行崩溃 → 容器从未赋值。改为 `(s==null?"":String(s)).replace(...)` |
| **轮询覆盖详情页 → 点「查看」整页消失** | 部分完成时点查看，页面没了 | `poll()` 每 1.5s 调 `renderBatch()` 会隐藏 `#result`、重显批量列表。加 `viewingDetail` 守卫：查看中只更新缓存/进度，不重载列表；返回时置 false（否则轮询死冻） |
| **旧 `render()` 先隐藏后填充** | 任一异常即留白 | 改为**先构建全部内容，成功后才 `display=block`**，异常只弹错误不隐藏页面 |
| **弹窗一滑就消失** | 整批查看弹窗滚动即关 | 原因是点击背景（`.ovMask`）即关闭，滚动手势易误触发 → 已**删除弹窗**，改为渲染到页面内 `#batchView` |
| Safari 修改不生效 | 反复报旧问题 | Safari 缓存旧 HTML。已在 FastAPI 加全局 `no-store` 中间件 + `<head>` 三个 no-cache meta；用户需硬刷新一次（Cmd+Shift+R） |
| 勾选计数不生效 | 选 3 个仍显示 0 或全部 | 缺 `change` 事件委托，只在全选/反选时更新。加 `#sellist` 的 `change` 委托 |
| Safari 兼容 | — | 去掉 `backdrop-filter`（Safari 合成透明 bug）；去掉 `Promise.allSettled`（老 Safari <13.1 不支持）改 `forEach` + async IIFE + `.catch` |

### 4.5 测试方法（很值得复用）

- **VM/DOM 模拟 harness 必须用真实后端返回结构**。之前所有 harness 都用字符串 `ep_no:"P1"`，掩盖了数字类型导致的 TypeError；换成线上真实数据（`ep_no:1`）才暴露。
- 自建 document 桩时，**必须注入浏览器全局**：`AbortController`、`clearTimeout`、`setInterval`、`localStorage`、`fetch`、`URL.createObjectURL`，否则 `finally{clearTimeout}` 抛 ReferenceError 造成**假失败**。
- 本机无法 `pip install` 大包（OOM）时，用 faithful stub 仿真 openpyxl/reportlab API 离线验证，并用 `py_compile` + `node --check` 兜底语法。
- 修改前端后，可用 `curl -s <域名>/ | grep -c '关键字'` 确认线上已生效（而非只信部署日志）。

---

## 5. 当前已知限制

1. **多副本状态未根治**：`MemoryBackend` 只在单副本可靠；当前靠前端缓存 + `alwaysScale` 绕过。
2. **ASR 配额按模型独立开通**（2026-08-29 本地实测更新）：当前 Key 下 **`fun-asr` 已可用**，`paraformer-v2` 未开通（提交即 403 AllocationQuota）。因已实现多模型自动回退，**无字幕视频现可正常走 `fun-asr` 转写**，无需额外操作。仅当 `fun-asr` 额度吃紧时，才需到百炼模型广场再开通 `qwen3-asr-flash-filetrans`（每月各赠 36000 秒）作回退备选。
3. **EdgeOne 版不含批量**：云函数超时跑不了长任务。要在 EdgeOne 支持批量需改为「每集单独云函数调用 + KV 任务存储」，属较大改动。
4. **B 站部分字幕需登录 Cookie**，否则回退 ASR（更慢、耗配额）。
5. **自定义域名**：`*.sh.run.tcloudbase.com` 可用；绑自定义域名中国区需 ICP 备案。
6. **无用户体系/持久化**：刷新后历史批次不可恢复（已主动移除历史批次入口）；任务结果不落库。

---

## 6. 继续前进的方向（按优先级）

### P0 —— 稳定性（建议先做，成本最低收益最高）
1. **控制台把云托管「最小实例数」设为 1**，或更好：**建 TencentDB for Redis 并配 `REDIS_URL`**。
   → 这是「多副本状态不一致」唯一的根治方案，能顺带解锁水平扩容。
2. **轮换 DashScope API Key**：该 Key 曾在会话中明文出现，建议到 DashScope 控制台轮换。
3. ~~**开通 ASR 模型配额**~~ → **已闭环（2026-08-29 实测）**：`fun-asr` 配额已可用，多模型回退在真实环境验证生效——日志可见「paraformer-v2 无配额，尝试下一模型 → DashScope 语音识别(fun-asr)中」。无字幕视频现已能正常转写，`paraformer-v2` 未开通也不影响。仅在 `fun-asr` 额度吃紧时才需再开通其他模型。

### P1 —— 功能补全
4. **任务结果持久化**：落 Redis / COS，支持刷新后恢复、断点续跑、跨设备查看。
5. **B 站 Cookie 引导**：在页面上对「字幕提取失败将回退 ASR」给出前置提示与 Cookie 填写引导，减少无谓的 ASR 配额消耗。
6. **长文阅读继续优化**：当前阅读栏 860px 居中、sticky 标题；可考虑分段折叠 / 目录导航 / 字号调节。
7. **批量体验**：批量任务的暂停/重试、失败集一键重跑、整体进度预估。

### P2 —— 产品化
8. **用户体系 + 配额计费**：当前仅按 IP 限流（20/小时），无法按用户计量与限流。
9. **异步通知**：批量任务耗时长（25 集需数十分钟），可加完成回调 / 邮件通知（`/api/send` 与 SMTP 代码曾实现过，后按需求移除，可恢复）。
10. **多平台扩展**：当前仅 B 站 + 本地上传；yt-dlp 本身支持 YouTube 等，接上即可。
11. **自定义域名 + 备案**，摆脱 `*.sh.run.tcloudbase.com`。

---

## 7. 可复用的经验沉淀

1. **前端缓存是无状态拓扑的通用解药**：把结果缓存在浏览器（`vsb_res_<id>`），即可让「查看/导出」完全不受多副本、缩容、实例重启影响。比改造后端状态存储便宜一个数量级。
2. **防御式渲染三原则**：① 先构建内容、成功后再显示；② 逐条 try/catch，单条异常不影响整体；③ 工具函数（如 `esc`）对所有类型安全。三者结合可根治「整页空白」类问题。
3. **轮询与详情视图必须互斥**：任何「定时刷新列表 + 可打开详情」的界面，都要有 `viewingDetail` 类守卫，且**返回时务必复位**，否则列表永久停止刷新。
4. **排查「修改不生效」先怀疑浏览器缓存**：线上 md5 与本地一致 + 后端已生效，但仍复现 → 加 `no-store` 并要求硬刷新。
5. **区分「配额/权限」与「代码 bug」**：上传凭证能申请成功 = Key 有效；具体模型调用 403 = 未开通该模型配额。独立配额（LLM vs ASR）是高频误判点。
6. **写 harness 必须用真实数据结构**，尤其注意字段类型（数字 vs 字符串），否则测试会系统性掩盖运行时错误。
7. **CloudBase CLI 速查**：登录 `cloudbase login --cloudbase-api-key <Key> -e <envId>`；部署 `printf '\n' | cloudbase cloudrun deploy --source . -s <服务名> --port <端口> -e <envId> --wait --force`。注意 `--port` 不是 `-p`，非交互必须喂回车。

---

## 附：常用验证命令

```bash
# 健康检查
curl -s https://video-subtitle-304233-9-1475154132.sh.run.tcloudbase.com/api/health

# 探测链接（应返回 25 个分P）
curl -s "https://video-subtitle-304233-9-1475154132.sh.run.tcloudbase.com/api/resolve?url=BV1PM8y6rEE3?p=16"

# 确认线上前端已含最新改动
curl -s https://video-subtitle-304233-9-1475154132.sh.run.tcloudbase.com/ | grep -c 'batchView\|rawseg'

# 本地语法兜底
python -m py_compile app/*.py
node --check <(提取 static/index.html 内联脚本)
```

测试视频：`BV1PM8y6rEE3`（25 个分P），选 `[0,1]` 约 30s 完成，适合快速回归。
