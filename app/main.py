# -*- coding: utf-8 -*-
"""视频字幕提取 —— 容器服务入口。

- /api/health        健康检查（探针 / 负载均衡用）
- /api/process       提交一个 URL 任务（B站 / 通用直链）
- /api/upload        提交用户自有音视频文件（合规主路径，不抓取第三方站点）
- /api/job/{id}      轮询任务状态与结果

限流与并发控制：
- 单 IP 每小时最多提交 IP_LIMIT_PER_HOUR 次
- 全服务并行任务数上限 GLOBAL_CONCURRENCY（超过返回 429）
- DASHSCOPE_API_KEY 由服务端环境变量提供，客户端可不填
"""
import os
import re
import io
import json
import uuid
import zipfile
import concurrent.futures
from urllib.parse import quote as _urlquote

from fastapi import FastAPI, Request, UploadFile, File, HTTPException, Form
from fastapi.responses import JSONResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles

from . import pipeline as pipe
from . import bilibili as bili
from .store import store

app = FastAPI(title="Video Subtitle Agent")


@app.middleware("http")
async def _no_store(req: Request, call_next):
    """所有响应禁用缓存：前端页面与结果随时可能更新，Safari 等浏览器不得缓存旧版本。"""
    resp = await call_next(req)
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


# ---------------------------------------------------------------------------
# 限流 / 并发参数（可用环境变量覆盖）
# ---------------------------------------------------------------------------
IP_LIMIT_PER_HOUR = int(os.environ.get("IP_LIMIT_PER_HOUR", "20"))
GLOBAL_CONCURRENCY = int(os.environ.get("GLOBAL_CONCURRENCY", "4"))
# 整季批量时，同一季内并行处理的集数上限（避免一次性打满并发与账单）
BATCH_CONCURRENCY = int(os.environ.get("BATCH_CONCURRENCY", "6"))
SERVER_API_KEY = os.environ.get("DASHSCOPE_API_KEY", "").strip()

# 单 IP 提交计数：dict[ip] = (窗口起点时间戳, 计数)
def _ip_rate_ok(ip: str) -> bool:
    """单 IP 每小时提交次数上限。计数走 store（Redis），多副本共享同一窗口。"""
    over, _ = store.hit_rate_limit(f"ip:{ip}", IP_LIMIT_PER_HOUR, 3600)
    return not over


def _client_ip(req: Request) -> str:
    fwd = req.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return req.client.host if req.client else "unknown"


@app.get("/api/health")
def health():
    return {"status": "ok", "has_server_key": bool(SERVER_API_KEY)}


@app.get("/api/models")
def api_models():
    """返回前端下拉框可用的模型列表：语音识别(asr) / 文本(llm) / 替代(fallback)。"""
    return pipe.get_available_models()


@app.get("/api/resolve")
def resolve(url: str = ""):
    """探测链接是否可批量处理（番剧整季 / 视频多分P选集）。

    返回 {kind, is_multi, count, title, items}：
    - kind: bangumi（番剧整季）/ video（UGC 视频）/ unknown
    - is_multi: 是否为可批量来源（番剧 >0 集，或视频 >1 个分P）
    - count: 总集数 / 分P 数
    - title: 首个标题（用于前端预览）
    - items: [{index, title, duration, page?}]，供前端勾选批量子集"""
    if not url:
        return {"kind": "unknown", "is_multi": False, "count": 0, "title": "", "items": []}
    try:
        if bili.is_bangumi(url):
            eps = bili.get_season_episodes(url)
            items = [{"index": i, "title": e["title"], "duration": e.get("duration")}
                     for i, e in enumerate(eps)]
            return {"kind": "bangumi", "is_multi": len(eps) > 0,
                    "count": len(eps),
                    "title": (eps[0]["title"] if eps else ""),
                    "items": items}
        pages = bili.get_video_pages(url)
        items = [{"index": i, "title": e["title"], "duration": e.get("duration"),
                  "page": e.get("page")}
                 for i, e in enumerate(pages)]
        return {"kind": "video", "is_multi": len(pages) > 1,
                "count": len(pages),
                "title": (pages[0]["title"] if pages else ""),
                "items": items}
    except Exception as e:
        return {"kind": "unknown", "is_multi": False, "count": 0,
                "title": "", "items": [], "error": str(e)}


@app.post("/api/process")
async def process(req: Request):
    import json
    try:
        payload = json.loads(await req.body() or b"{}")
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    ip = _client_ip(req)
    if not _ip_rate_ok(ip):
        raise HTTPException(status_code=429, detail="提交过于频繁，请稍后再试（每小时上限 %d 次）" % IP_LIMIT_PER_HOUR)

    if store.running_count() >= GLOBAL_CONCURRENCY:
        raise HTTPException(status_code=429, detail="当前任务排队已满，请稍后重试")

    url = (payload.get("url") or "").strip()
    user_key = (payload.get("api_key") or "").strip()
    cookie = (payload.get("cookie") or "").strip()
    model = (payload.get("model") or "").strip() or None
    asr_model = (payload.get("asr_model") or "").strip() or None
    llm_model = (payload.get("llm_model") or "").strip() or None
    fallback_model = (payload.get("fallback_model") or "").strip() or None
    if not url:
        raise HTTPException(status_code=400, detail="缺少 url 参数")

    api_key = user_key or SERVER_API_KEY

    # —— 整集/整季批量（可选）：番剧整季 或 UGC 视频多分P选集，且勾选 batch ——
    if bool(payload.get("batch")):
        import threading
        # 解析批量来源：番剧整季 / 视频选集（多P）
        if bili.is_bangumi(url):
            source, eps = "bangumi", bili.get_season_episodes(url)
        else:
            pages = bili.get_video_pages(url)
            source = "video"
            # 单P视频不能当批量（退化为普通单任务更合适），这里置空使其返回明确提示
            eps = pages if len(pages) > 1 else []
        if not eps:
            raise HTTPException(
                status_code=400,
                detail="该链接不是番剧整季或视频选集（多P）链接，无法批量解析。"
                       "单集/单P视频请直接提交，无需勾选批量。")
        # 支持「勾选子集」：selected 为分集序号数组（0-based），缺省=全部
        sel = payload.get("selected")
        if sel:
            try:
                idxs = [int(x) for x in sel if x is not None]
            except Exception:
                idxs = []
            eps = [eps[i] for i in idxs if 0 <= i < len(eps)]
        if not eps:
            raise HTTPException(
                status_code=400,
                detail="未选择任何分集，请至少勾选一个分集再提交。")
        if store.running_count() >= GLOBAL_CONCURRENCY:
            raise HTTPException(status_code=429, detail="当前任务排队已满，请稍后重试")
        batch_id = uuid.uuid4().hex
        # 父任务不占并发名额（beat=False），仅子任务计入全局并发
        store.create(batch_id, kind="batch", total=len(eps), beat=False)
        children = [{"id": uuid.uuid4().hex, "title": e["title"],
                     "ep_no": e.get("page") or (i + 1)}
                    for i, e in enumerate(eps)]
        store.set_batch_children(batch_id, children)
        # 用信号量限制整季/整集内并发数（避免一次性打满并发与账单）；
        # 子任务用 daemon 线程（与单任务一致），并预先 create 保证聚合可见
        sem = threading.Semaphore(max(1, min(BATCH_CONCURRENCY, len(children))))

        def _run_child(cid, child_url, ep_title):
            with sem:
                pipe.run_pipeline(cid, child_url, api_key, model, cookie, ep_title=ep_title,
                                  asr_model=asr_model, llm_model=llm_model, fallback_model=fallback_model)

        for ch, e in zip(children, eps):
            if source == "bangumi":
                child_url = "https://www.bilibili.com/bangumi/play/ep%s" % e["ep_id"]
            else:
                child_url = "https://www.bilibili.com/video/%s?p=%s" % (e["bvid"], e["page"])
            store.create(ch["id"])
            threading.Thread(target=_run_child, args=(ch["id"], child_url, e["title"]),
                             daemon=True).start()
        return {"batch_id": batch_id, "status": "running",
                "total": len(eps), "jobs": children, "source": source}

    # —— 普通单任务 ——
    job_id = uuid.uuid4().hex
    store.create(job_id)
    import threading
    threading.Thread(
        target=pipe.run_pipeline,
        args=(job_id, url, api_key, model),
        kwargs={"bili_cookie": cookie,
                "asr_model": asr_model, "llm_model": llm_model, "fallback_model": fallback_model},
        daemon=True,
    ).start()
    return {"job_id": job_id, "status": "running"}


@app.post("/api/upload")
async def upload(
    req: Request,
    file: UploadFile = File(...),
    api_key: str = Form(""),
    model: str = Form(""),
    asr_model: str = Form(""),
    llm_model: str = Form(""),
    fallback_model: str = Form(""),
):
    ip = _client_ip(req)
    if not _ip_rate_ok(ip):
        raise HTTPException(status_code=429, detail="提交过于频繁，请稍后再试（每小时上限 %d 次）" % IP_LIMIT_PER_HOUR)
    if store.running_count() >= GLOBAL_CONCURRENCY:
        raise HTTPException(status_code=429, detail="当前任务排队已满，请稍后重试")

    import tempfile
    from pathlib import Path
    tmp = tempfile.mkdtemp(prefix="vsb_up_")
    dest = Path(tmp) / file.filename
    with open(dest, "wb") as f:
        content = await file.read()
        f.write(content)

    job_id = uuid.uuid4().hex
    store.create(job_id)
    user_key = (api_key or "").strip()
    api_key_final = user_key or SERVER_API_KEY
    model_final = (model or "").strip() or None
    asr_final = (asr_model or "").strip() or None
    llm_final = (llm_model or "").strip() or None
    fb_final = (fallback_model or "").strip() or None
    import threading
    threading.Thread(
        target=pipe.run_pipeline,
        args=(job_id, "", api_key_final, model_final),
        kwargs={"local_file": str(dest),
                "asr_model": asr_final, "llm_model": llm_final, "fallback_model": fb_final},
        daemon=True,
    ).start()
    return {"job_id": job_id, "status": "running"}


@app.get("/api/job/{job_id}")
def get_job(job_id: str):
    job = store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在或已过期")
    return job


SECTION_LABEL = {
    "raw": "原始字幕",
    "summary": "梳理结果",
    "tech": "技术提取",
    "bilingual": "中英文对照",
}

# 一键导出 时，每个分区需要包含的格式（与查看页导出按钮保持一致）。
# 这些格式组合后打包成一个 zip，文件名统一为「当前视频名_页面名称.格式」，例如 测试视频_原始字幕.txt。
FORMATS_BY_SECTION = {
    "raw": ["txt", "srt", "xlsx", "pdf"],
    "summary": ["txt", "md", "xlsx", "pdf"],
    "tech": ["txt", "xlsx", "pdf"],
    "bilingual": ["txt", "xlsx", "pdf"],
}
# 一键导出包含的全部（分区 × 格式）文件数 = 4+4+3+3 = 14


@app.get("/api/job/{job_id}/export")
def export_job(job_id: str, section: str = "summary", format: str = "txt"):
    """把某一任务（单个视频）的指定分区导出为可下载文件。

    section: raw=原始字幕 / summary=梳理结果 / tech=技术提取 / bilingual=中英文对照
    format:  txt / srt / md / json / xlsx(Excel) / pdf
    分区+格式组合决定导出内容，按钮文案会明确「导出【xx】：」。"""
    job = store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在或已过期")
    result = job.get("result")
    if not result:
        raise HTTPException(status_code=400, detail="任务尚未完成，无法导出")
    section = (section or "summary").lower()
    if section not in SECTION_LABEL:
        section = "summary"
    fmt = (format or "txt").lower()
    content, ext, mt = _gen_section_file(result, section, fmt)
    title = re.sub(r'[\\/:*?"<>|]', "_", (result.get("title") or "subtitle")).strip() or "subtitle"
    # Content-Disposition 的 filename 若含中文，starlette 按 latin-1 编码 header 会抛
    # UnicodeEncodeError -> 500。改用 RFC 5987 的 filename*=UTF-8'' 编码，浏览器据此
    # 显示中文文件名；filename 留 ASCII 降级名（老客户端兼容）。
    fname = "%s_%s.%s" % (title, SECTION_LABEL[section], ext)
    ascii_name = fname.encode("ascii", "ignore").decode().strip() or ("subtitle_%s.%s" % (SECTION_LABEL[section], ext))
    cd = 'attachment; filename="%s"; filename*=UTF-8\'\'%s' % (ascii_name, _urlquote(fname))
    headers = {"Content-Disposition": cd}
    if isinstance(content, (bytes, bytearray)):
        return Response(bytes(content), media_type=mt, headers=headers)
    return Response(content.encode("utf-8"), media_type=mt, headers=headers)


def _gen_section_file(result, section, fmt):
    """生成某分区某格式的文件内容，返回 (content, ext, media_type)。

    被单分区导出 / 一键导出(打包 zip) / 一键发送 共用，保证三处产物完全一致。"""
    section = (section or "summary").lower()
    if section not in SECTION_LABEL:
        section = "summary"
    fmt = (fmt or "txt").lower()
    blocks = _section_blocks(result, section)
    label = SECTION_LABEL[section]
    title = re.sub(r'[\\/:*?"<>|]', "_", (result.get("title") or "subtitle")).strip() or "subtitle"
    if fmt == "xlsx":
        return _serialize_xlsx(blocks, label), "xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    if fmt == "pdf":
        return _serialize_pdf(blocks, title, label), "pdf", "application/pdf"
    if fmt == "srt":
        return _build_srt(result), "srt", "application/x-subrip; charset=utf-8"
    if fmt == "json":
        return json.dumps(result, ensure_ascii=False, indent=2), "json", "application/json; charset=utf-8"
    if fmt == "md":
        return _serialize_md(blocks), "md", "text/markdown; charset=utf-8"
    return _serialize_text(blocks), "txt", "text/plain; charset=utf-8"


def _build_all_zip(result):
    """把「查看」里所有可分区的文件打包成一个 zip，文件名统一为「当前视频名_页面名称.格式」。

    返回 zip 的字节内容。前端/邮件发送均复用此函数，确保命名与内容一致。"""
    title = re.sub(r'[\\/:*?"<>|]', "_", (result.get("title") or "subtitle")).strip() or "subtitle"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for sec in SECTION_LABEL:
            for fmt in FORMATS_BY_SECTION.get(sec, []):
                content, ext, _mt = _gen_section_file(result, sec, fmt)
                fname = "%s_%s.%s" % (title, SECTION_LABEL[sec], ext)   # 例如 测试视频_原始字幕.txt / 测试视频_梳理结果.xlsx
                data = content if isinstance(content, (bytes, bytearray)) else content.encode("utf-8")
                z.writestr(fname, data)
    return buf.getvalue()


def _zip_attachment_name(result):
    title = re.sub(r'[\\/:*?"<>|]', "_", (result.get("title") or "subtitle")).strip() or "subtitle"
    return "%s_全部导出.zip" % title


@app.post("/api/export-all")
async def export_all(req: Request):
    """一键导出：把某视频「查看」里所有可分区的文件打包成 zip 下载。

    请求体两种写法：
    - {job_id}: 优先用 job_id 从服务端内存取结果。请求体极小，可避免把完整 result
      再 POST 回去触发反向代理「请求体大小限制」→ 浏览器下载报 "Load failed"（生产常见）。
    - {result}: 回退写法（实例重启导致内存结果丢失时，前端用已缓存的 result 兜底）。"""
    try:
        payload = json.loads(await req.body() or b"{}")
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    result = None
    job_id = payload.get("job_id")
    if isinstance(job_id, str) and job_id:
        j = store.get(job_id)
        if j and isinstance(j.get("result"), dict):
            result = j["result"]
    if not isinstance(result, dict):
        result = payload.get("result") if isinstance(payload.get("result"), dict) else None
    if not isinstance(result, dict) or not (result.get("title") or result.get("raw_segments") or result.get("structured")):
        raise HTTPException(
            status_code=400,
            detail="缺少有效的 result（实例可能已重启导致结果丢失，请重新解析后即时导出；或保持本页面不刷新直接导出）")
    try:
        zbytes = _build_all_zip(result)
    except Exception as e:
        raise HTTPException(status_code=500, detail="打包失败：" + str(e))
    fname = _zip_attachment_name(result)
    ascii_name = fname.encode("ascii", "ignore").decode().strip() or "subtitle_all.zip"
    cd = 'attachment; filename="%s"; filename*=UTF-8\'\'%s' % (ascii_name, _urlquote(fname))
    return Response(zbytes, media_type="application/zip", headers={"Content-Disposition": cd})


# ---------------------------------------------------------------------------
# 分区内容构建 + 多格式序列化
# ---------------------------------------------------------------------------
def _xe(s):
    """XML/HTML 转义（用于 reportlab / xlsx 单元格安全）。"""
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _light_md(s):
    """去除 Markdown 标记，得到纯文本（用于 TXT/Excel/PDF 展示）。"""
    import re as _re
    s = str(s)
    s = _re.sub(r'^#{1,6}\s*', '', s, flags=_re.M)
    s = _re.sub(r'^\s*[-*]\s*', '', s, flags=_re.M)
    s = s.replace("**", "")
    return s


def _seg_text(result):
    segs = result.get("raw_segments") or []
    if not segs:
        return result.get("clean_transcript") or result.get("raw_transcript") or ""
    return "\n\n".join(segs)


def _section_blocks(result, section):
    """把某一分区表达为统一的 blocks：
       [("title", ...), ("h", 标题), ("p"/"md", 文本), ("table", [[...]])]

       对 LLM 可能返回的非常规结构做容错（避免导出 500）：
       - bilingual 可能是 [{orig,trans}]，也可能是纯字符串列表 / 字符串 / 键名不同（text/translation 等）
       - tech 可能是 {technologies,features}，也可能是裸列表
    """
    title = result.get("title") or "视频字幕"
    blocks = [("title", title)]
    if section == "raw":
        blocks.append(("h", "一、原始字幕（完整文本）"))
        blocks.append(("p", _seg_text(result)))
    elif section == "summary":
        blocks.append(("h", "二、智能梳理结果"))
        # structured 可能是字符串，也可能是 LLM 返回的 dict/其他非常规结构：统一 str() 兜底，
        # 避免 _serialize_md 的 "\n".join 对 dict 抛 "expected str instance, dict found" 导致 500。
        blocks.append(("md", str(result.get("structured") or result.get("clean_transcript") or "（无梳理结果）")))
    elif section == "tech":
        blocks.append(("h", "三、技术提取"))
        tech = result.get("tech")
        if not isinstance(tech, dict):
            # 兼容 tech 直接是列表的非常规返回
            tech = {"technologies": tech} if isinstance(tech, list) else {}
        techs = tech.get("technologies") or []
        feats = tech.get("features") or []
        if isinstance(techs, list) and techs and isinstance(techs[0], dict):
            rows = [["#", "技术名称", "主要作用", "国内可用性", "特别说明"]]
            for i, t in enumerate(techs, 1):
                if not isinstance(t, dict):
                    rows.append([str(i), str(t), "", "", ""]); continue
                # 每个字段都 str() 兜底：真实 LLM 返回里 name/function 等可能是 dict/list，
                # 直接写进 openpyxl 单元格会抛 "Cannot convert {...} to Excel" -> 500。
                rows.append([str(i), str(t.get("name", "") or ""),
                             str(t.get("function", "") or ""), str(t.get("domestic", "") or ""), str(t.get("notes", "") or "")])
            blocks.append(("table", rows))
        elif isinstance(techs, list):
            blocks.append(("p", "\n".join("- " + str(x) for x in techs) or "（无）"))
        blocks.append(("h", "主要功能"))
        blocks.append(("p", "\n".join("- " + str(x) for x in feats) if feats else "（无）"))
    elif section == "bilingual":
        blocks.append(("h", "四、中英文对照"))
        bl = result.get("bilingual")
        if isinstance(bl, str):
            bl = [x for x in bl.split("\n") if x.strip()]
        if isinstance(bl, list) and bl:
            rows = [["原文", "译文"]]
            for it in bl:
                if isinstance(it, dict):
                    # str() 兜底：真实 LLM 返回里 orig/trans 可能是 dict/list，
                    # 直接 .strip() 会抛 "'dict'/'list' object has no attribute 'strip'" -> 500。
                    o = str(it.get("orig") or it.get("text") or it.get("source") or "").strip()
                    t = str(it.get("trans") or it.get("translation") or it.get("target") or "").strip()
                else:
                    o, t = str(it).strip(), ""
                rows.append([o, t])
            blocks.append(("table", rows))
        else:
            blocks.append(("p", "（无中英文对照数据；需填写 API Key 经 AI 翻译后生成）"))
    return blocks


def _serialize_text(blocks):
    out = []
    for typ, p in blocks:
        if typ == "title":
            out.append(p); out.append("")
        elif typ == "h":
            out.append("■ " + p); out.append("")
        elif typ in ("p", "md"):
            out.append(_light_md(p)); out.append("")
        elif typ == "table":
            for r in p:
                out.append("\t".join(str(c) for c in r))
            out.append("")
    return "\n".join(out)


def _serialize_md(blocks):
    out = []
    for typ, p in blocks:
        if typ == "title":
            out.append("# " + p); out.append("")
        elif typ == "h":
            out.append("## " + p); out.append("")
        elif typ in ("p", "md"):
            # str() 兜底：structured 等字段若非常规地返回 dict/列表，直接序列化而非抛 500
            out.append(str(p)); out.append("")
        elif typ == "table":
            out.append("| " + " | ".join(str(c) for c in p[0]) + " |")
            out.append("| " + " | ".join("---" for _ in p[0]) + " |")
            for r in p[1:]:
                out.append("| " + " | ".join(str(c) for c in r) + " |")
            out.append("")
    return "\n".join(out)


def _serialize_xlsx(blocks, label):
    from openpyxl import Workbook
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter
    wb = Workbook()
    ws = wb.active
    ws.title = label[:31]
    r = 1
    for typ, p in blocks:
        if typ == "title":
            c = ws.cell(r, 1, p); c.font = Font(bold=True, size=14); r += 1
        elif typ == "h":
            c = ws.cell(r, 1, p); c.font = Font(bold=True, size=12); r += 1
        elif typ in ("p", "md"):
            ws.cell(r, 1, _light_md(p)); r += 1
        elif typ == "table":
            for ri, row in enumerate(p):
                for ci, cell in enumerate(row, 1):
                    # str() 兜底：极端情况下单元格仍可能为非字符串（如 LLM 嵌套结构漏网），
                    # 写进 openpyxl 会抛 "Cannot convert ... to Excel" -> 500。
                    c = ws.cell(r, ci, str(cell))
                    if ri == 0:
                        c.font = Font(bold=True)
                r += 1
    # 自适应列宽
    for col in ws.columns:
        m = 0
        for cell in col:
            m = max(m, len(str(cell.value or "")))
        ws.column_dimensions[get_column_letter(col[0].column)].width = min(80, max(10, m + 2))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _serialize_pdf(blocks, safe_title, label):
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib import colors
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer,
                                    Table, LongTable, TableStyle, LayoutError)
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))  # 内置中文 CID 字体
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, title=safe_title, author="Video Subtitle Agent",
                            leftMargin=48, rightMargin=48, topMargin=48, bottomMargin=48)
    st = ParagraphStyle("st", fontName="STSong-Light", fontSize=18, leading=24, spaceAfter=8)
    sh = ParagraphStyle("sh", fontName="STSong-Light", fontSize=14, leading=20,
                        spaceBefore=8, spaceAfter=4)
    sn = ParagraphStyle("sn", fontName="STSong-Light", fontSize=11, leading=17)

    def _cell(txt, bold=False):
        cstyle = ParagraphStyle("c", fontName="STSong-Light", fontSize=9 if not bold else 10,
                                leading=13, textColor=colors.white if bold else colors.black)
        return Paragraph(_xe(txt), cstyle)

    def _build_flow(flat):
        """flat=False 用表格渲染；flat=True 把所有表格改成竖排段落，
        保证任意超长单元格都能被 reportlab 拆分，绝不触发 'too large on page'。"""
        fl = [Paragraph(_xe(safe_title), st)]
        for typ, p in blocks[1:]:
            if typ == "h":
                fl.append(Paragraph(_xe(p), sh))
            elif typ in ("p", "md"):
                fl.append(Paragraph(_xe(_light_md(p)), sn))
                fl.append(Spacer(1, 6))
            elif typ == "table":
                rows = [[str(c) for c in row] for row in p]
                if flat or not rows:
                    # 竖排：表头加粗一行，其余逐行拼接字段（中英文对照则显式标注原文/译文）
                    is_bil = rows and [c.strip() for c in rows[0]] == ["原文", "译文"]
                    if is_bil:
                        for o, t in rows[1:]:
                            fl.append(Paragraph(_xe("【原文】" + o), sn))
                            fl.append(Paragraph(_xe("【译文】" + t), sn))
                            fl.append(Spacer(1, 4))
                    else:
                        for ri, row in enumerate(rows):
                            line = ("   ".join(row)) if ri == 0 else ("  ·  ".join(row))
                            fl.append(Paragraph(_xe(line), sh if ri == 0 else sn))
                            fl.append(Spacer(1, 2))
                else:
                    data = [[_cell(c, bold=(ri == 0)) for c in row] for ri, row in enumerate(rows)]
                    ncol = len(rows[0]) if rows else 1
                    cw = doc.width / ncol
                    tbl = LongTable(data, colWidths=[cw] * ncol, repeatRows=1)
                    tbl.setStyle(TableStyle([
                        ("FONTNAME", (0, 0), (-1, -1), "STSong-Light"),
                        ("FONTSIZE", (0, 0), (-1, -1), 9),
                        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1e2340")),
                        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                        ("LEFTPADDING", (0, 0), (-1, -1), 5),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                        ("TOPPADDING", (0, 0), (-1, -1), 3),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                    ]))
                    fl.append(tbl)
                    fl.append(Spacer(1, 8))
        return fl

    try:
        doc.build(_build_flow(flat=False))
    except LayoutError:
        # 单行单元格过高（如超长原文）无法在页框内拆分，退回竖排段落重建
        buf = io.BytesIO()
        doc = SimpleDocTemplate(buf, title=safe_title, author="Video Subtitle Agent",
                                leftMargin=48, rightMargin=48, topMargin=48, bottomMargin=48)
        doc.build(_build_flow(flat=True))
        return buf.getvalue()
    return buf.getvalue()


def _srt_time(sec):
    sec = max(0, float(sec))
    h, m = int(sec // 3600), int((sec % 3600) // 60)
    s = sec % 60
    return "%02d:%02d:%02d,%03d" % (h, m, int(s), int((s - int(s)) * 1000))


def _build_srt(result):
    segs = result.get("raw_segments") or []
    if not segs:
        text = result.get("clean_transcript") or result.get("raw_transcript") or ""
        segs = [p for p in text.split("\n\n") if p.strip()]
    if not segs:
        return ""
    n = len(segs)
    dur = result.get("duration") or 0
    span = (dur / n) if dur else 5.0   # 无真实时间戳时按总时长均分
    out = []
    for i, s in enumerate(segs, 1):
        out.append("%d\n%s --> %s\n%s\n" % (
            i, _srt_time((i - 1) * span), _srt_time(i * span), s.strip()))
    return "\n".join(out)


# 静态前端
import os as _os
_static_dir = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "static")
if _os.path.isdir(_static_dir):
    # 必须先挂载更具体的 /static：下面 mount("/") 会吞掉其余所有路径，
    # 若不先注册 /static，请求 /static/css/base.css 会被解析成 static/static/css/base.css 而 404。
    app.mount("/static", StaticFiles(directory=_static_dir), name="static_assets")
    # 根路径提供 index.html（前端已拆分为 static/css/*.css 与 static/js/*.js）
    app.mount("/", StaticFiles(directory=_static_dir, html=True), name="static")
