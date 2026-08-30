# -*- coding: utf-8 -*-
"""视频字幕提取与智能梳理 —— 业务流水线（容器服务版）。

与云函数版的差异：
- 任务状态写入 store（Redis/内存），不再用进程内字典，多副本可共享
- 容器镜像内置 ffmpeg，恢复「转 16k 单声道 wav + 超长裁剪」能力
- 删除 /audio 回拉路由与相关映射：音频改为直传 DashScope 临时 OSS
- 删除未使用的 Whisper 懒加载残留
"""
import os
import re
import json
import time
import uuid
import shutil
import asyncio
import subprocess
import tempfile
import concurrent.futures
from pathlib import Path

import requests

from . import bilibili as bili
from .store import store


# B 站等站点对默认请求头返回 412，需带浏览器 UA / Referer
YDL_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.bilibili.com",
    "Cookie": "",
}


# ---------------------------------------------------------------------------
# 步骤 1：yt-dlp 解析视频信息
# ---------------------------------------------------------------------------
def extract_info(url):
    import yt_dlp
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "simulate": True,
        "http_headers": YDL_HEADERS,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
    return info


# ---------------------------------------------------------------------------
# 步骤 2：提取字幕
# ---------------------------------------------------------------------------
def _pick_subtitle_lang(info):
    """挑选最合适的字幕语言：人工字幕优先于 AI 字幕，中文优先。"""
    subs = info.get("subtitles") or {}
    auto = info.get("automatic_captions") or {}
    zh_like = ["zh-CN", "zh-Hans", "zh-Hant", "zh", "chi", "zh-Hans-CN", "cn"]
    en_like = ["en", "eng", "en-US", "en-GB"]

    def find(pool):
        for lang in zh_like:
            if lang in pool:
                return lang, False
        for lang in en_like:
            if lang in pool:
                return lang, False
        # 任意可用语言
        for lang in pool:
            if lang and lang != "live_chat":
                return lang, False
        return None, False

    lang, _ = find(subs)
    if lang:
        return lang, False
    lang, _ = find(auto)
    if lang:
        return lang, True
    return None, False


def download_subtitles(url, lang, is_auto, workdir):
    import yt_dlp
    outtmpl = str(Path(workdir) / "%(id)s")
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "writesubtitles": not is_auto,
        "writeautomaticsub": is_auto,
        "subtitleslangs": [lang],
        "subtitlesformat": "srt/vtt/best",
        "outtmpl": outtmpl,
        "http_headers": YDL_HEADERS,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])
    # 查找生成的字幕文件
    for f in Path(workdir).glob(f"*.{lang}.*"):
        if f.suffix in (".srt", ".vtt", ".json", ".sjson"):
            return f
    # 兜底：任意 srt/vtt
    for f in Path(workdir).glob("*"):
        if f.suffix in (".srt", ".vtt"):
            return f
    return None


def parse_subtitle_file(path):
    """把 srt/vtt/json 解析为纯文本（去时间轴与标签）。"""
    text = Path(path).read_text(encoding="utf-8", errors="ignore")
    if path.suffix == ".json" or path.suffix == ".sjson":
        try:
            data = json.loads(text)
            # Bilibili json 字幕格式
            if isinstance(data, dict) and "body" in data:
                return "\n".join(b.get("content", "") for b in data["body"])
        except Exception:
            pass
    # 去除 srt/vtt 时间轴与序号
    lines = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if re.match(r"^\d+$", line):
            continue
        if re.match(r"^\d{2}:\d{2}:?\d{0,2}.*-->", line):
            continue
        line = re.sub(r"<[^>]+>", "", line)  # 去 vtt 标签
        if line:
            lines.append(line)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 步骤 3：下载音频
# ---------------------------------------------------------------------------
def download_audio(url, workdir):
    import yt_dlp
    out = Path(workdir) / "audio"
    out.mkdir(parents=True, exist_ok=True)
    outtmpl = str(out / "%(id)s.%(ext)s")
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "format": "bestaudio/best",
        "outtmpl": outtmpl,
        "retries": 3,
        "http_headers": YDL_HEADERS,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
    except Exception:
        pass
    files = list(out.glob("*"))
    if not files:
        raise RuntimeError("无法下载音频（链接可能需登录、付费或已失效）")
    src = files[0]
    # 有 ffmpeg 时统一转 16k 单声道 wav 供识别使用；超长则裁剪到上限
    max_sec = int(os.environ.get("MAX_AUDIO_SEC", "7200"))
    if shutil.which("ffmpeg"):
        wav = out / "audio.wav"
        cmd = (
            f'ffmpeg -y -i "{src}" -ar 16000 -ac 1 -c:a pcm_s16le '
            f'-t {max_sec} "{wav}" -loglevel error'
        )
        os.system(cmd)
        if wav.exists() and wav.stat().st_size > 0:
            return wav
    # 无 ffmpeg（云函数环境常见）：保留原始容器格式，扩展名必须与真实编码一致，
    # 否则识别服务按扩展名解码会报 DECODE_ERROR
    return src


def transcode_for_asr(src, workdir):
    """把用户上传的音视频转为识别用的 16k 单声道 wav。

    容器镜像内置 ffmpeg，这里统一转码并裁剪到上限长度（MAX_AUDIO_SEC），
    让 DashScope Paraformer 拿到最稳的格式。无 ffmpeg 时退回原文件（保留原扩展名）。"""
    src = Path(src)
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    max_sec = int(os.environ.get("MAX_AUDIO_SEC", "7200"))
    # 用户上传的文件名可能带中文/空格，输出到独立文件名避免 ffmpeg 解析出错
    wav = workdir / "upload.wav"
    if shutil.which("ffmpeg"):
        cmd = (
            f'ffmpeg -y -i "{src}" -ar 16000 -ac 1 -c:a pcm_s16le '
            f'-t {max_sec} "{wav}" -loglevel error'
        )
        os.system(cmd)
        if wav.exists() and wav.stat().st_size > 0:
            return wav
    return src


# ---------------------------------------------------------------------------
# 步骤 5a：可读化清洗（无 LLM key 时作最终输出；有 key 时作「整理文字」视图）
# ---------------------------------------------------------------------------
def clean_transcript(text):
    """把 ASR/字幕原始文本整理为可读、分段的中英纯文本：
    去非语音标注、折叠重复标点、按语义长度智能分段；保留英文词间空格。"""
    if not text:
        return ""
    # 1) 去非语音标注：[音乐][鼓掌]、【背景音乐】、<vtt 标签>、(笑声)(掌声)
    text = re.sub(r"\[[^\]]{0,20}\]", "", text)
    text = re.sub(r"【[^】]{0,20}】", "", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"（[^）]{0,20}）", "", text)
    text = re.sub(r"\([^)]{0,20}\)", "", text)
    # 2) 标点归一与清洗
    text = text.replace("，。", "。").replace("。。", "。").replace("？。", "？").replace("！。", "！")
    text = re.sub(r"([。！？!?])+", r"\1", text)   # 折叠重复句末标点
    text = re.sub(r"([，,])+", r"\1", text)        # 折叠重复逗号
    text = re.sub(r"\s+", " ", text).strip()        # 仅折叠空白，保留英文词间空格
    if not text:
        return ""
    # 3) 断句（以句末标点切分）
    pieces = re.split(r"(?<=[。！？!?])", text)
    pieces = [p.strip() for p in pieces if p.strip()]
    if not pieces:
        return text
    # 4) 分段：判断语种决定句间连接方式，累计约 140 字或满 6 句即换段
    is_latin = bool(re.search(r"[A-Za-z]", text)) and not bool(re.search(r"[一-鿿]", text))
    joiner = " " if is_latin else ""
    paras, buf, cnt = [], [], 0
    for s in pieces:
        buf.append(s)
        cnt += len(s)
        if cnt >= 140 or len(buf) >= 6:
            paras.append(joiner.join(buf))
            buf, cnt = [], 0
    if buf:
        paras.append(joiner.join(buf))
    return "\n\n".join(paras)


# ---------------------------------------------------------------------------
# 步骤 5b：通义千问 校验 + 梳理
# ---------------------------------------------------------------------------
DASHSCOPE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"

SYSTEM_PROMPT = (
    "你是一位专业的字幕校对与内容梳理专家。用户会给你一段从视频中提取的"
    "原始字幕/转写文本（可能含识别错误、断句不当、口语冗余、[音乐]/（笑声）等标注）。请完成两件事：\n"
    "1）校验与清洗：纠正明显的同音错别字与事实性错字，补全被截断的语句，去除无意义的"
    "口语填充、重复与非语音标注（如 [音乐]、（掌声））；尽量保留原意与原话。\n"
    "2）梳理：在理解全文后，提炼核心脉络，输出条理分明、便于阅读的中文排版。"
    "使用 Markdown：用分层标题组织章节，用要点列表呈现关键结论，保留少量关键原话。\n"
    "排版要求：段落之间必须空行分隔，每段聚焦一个意思、控制在 3–6 句；不要将全文堆成"
    "一大段或一长串无断句的文字；务必把内容划分成清晰、可读、有节奏的段落。\n"
    "只输出最终的 Markdown 内容，不要额外解释你的操作步骤。"
)


def llm_refine(text, title, api_key, model):
    if not api_key:
        return None
    user = f"视频标题：{title}\n\n==== 原始字幕文本 ====\n{text}\n\n请按上述要求校对并梳理。"
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ],
        "temperature": 0.3,
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    r = requests.post(DASHSCOPE_URL, headers=headers, json=body, timeout=180)
    if r.status_code != 200:
        raise RuntimeError(f"通义千问调用失败 {r.status_code}: {r.text[:300]}")
    data = r.json()
    return data["choices"][0]["message"]["content"].strip()


def detect_lang(text):
    """粗略判断主语言：含较多中文则 zh，否则 en。"""
    lat = len(re.findall(r"[A-Za-z]", text or ""))
    cjk = len(re.findall(r"[一-鿿]", text or ""))
    if cjk == 0:
        return "en"
    return "en" if lat >= cjk else "zh"


def _llm_json(user, system, api_key, model, max_tokens=2000):
    """调用通义千问并以严格 JSON 返回（带兜底解析）。"""
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.3,
        "response_format": {"type": "json_object"},
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    r = requests.post(DASHSCOPE_URL, headers=headers, json=body, timeout=180)
    if r.status_code != 200:
        raise RuntimeError(f"通义千问调用失败 {r.status_code}: {r.text[:200]}")
    content = r.json()["choices"][0]["message"]["content"]
    try:
        return json.loads(content)
    except Exception:
        m = re.search(r"\{.*\}", content, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                pass
    return None


def llm_tech_extract(text, title, api_key, model):
    """从字幕/转写中提炼：每一项技术的用途、是否支持国内使用、特别注意事项。
    返回 dict 或 None。"""
    if not api_key:
        return None
    system = (
        "你是技术分析师。请阅读视频字幕/转写文本，识别其中提到的每一项技术/工具/框架/方法。"
        "针对每一页技术，都要给出其作用、在国内（中国大陆）的可获得性，以及特别的注意事项。"
        '以严格 JSON 返回，格式：'
        '{"technologies": [{"name": "技术/工具/框架/方法名称", '
        '"function": "主要作用（一句话到两三句话）", '
        '"domestic": "是否支持国内使用：结论（支持/部分支持/不支持/未提及）+ 1-2 句依据", '
        '"notes": "针对该技术特别需要提出的事项，无则给空字符串"}], '
        '"features": ["视频整体呈现的主要功能/能力（字符串数组）"]}。'
        "只返回 JSON，不要任何解释或 markdown 代码块。"
    )
    user = f"视频标题：{title}\n\n==== 字幕/转写文本 ====\n{text}\n\n请按上述格式提取。"
    return _llm_json(user, system, api_key, model, max_tokens=3000)


def llm_bilingual(text, title, api_key, model, src_lang):
    """把字幕按语义分段并翻译为对照文本。返回 [{orig, trans}] 或 None。"""
    if not api_key:
        return None
    tgt = "中文" if src_lang == "en" else "英文"
    srclbl = "英文" if src_lang == "en" else "中文"
    system = (
        f"你负责字幕翻译与分段。请把用户给出的{srclbl}字幕按语义切分成若干段落"
        f"（每段 3-6 句），并为每段提供{tgt}译文。"
        '以严格 JSON 数组返回，每个元素格式：{"orig": "原段落文字", "trans": "译文文字"}。'
        "只返回 JSON 数组，不要任何解释或 markdown 代码块。"
    )
    user = f"视频标题：{title}\n\n==== {srclbl}字幕 ====\n{text}\n\n请分段并翻译为{tgt}。"
    obj = _llm_json(user, system, api_key, model, max_tokens=4000)
    if isinstance(obj, list):
        return obj
    if isinstance(obj, dict):
        for v in obj.values():
            if isinstance(v, list):
                return v
    return None


# ---------------------------------------------------------------------------
# 步骤 4b：通义千问语音识别（Paraformer）做无字幕时的语音转写
# ---------------------------------------------------------------------------
def _needs_spacing(raw):
    """Paraformer 英文输出常把单词连写（无空格）。检测是否存在较长的不含空格拉丁串，
    若存在则说明需要按词补空格。"""
    runs = re.findall(r"[A-Za-z]{12,}", raw or "")
    return any(len(r) >= 12 for r in runs)


def _parse_dashscope_transcript(raw_text):
    """解析 DashScope 录音文件转写结果：从 JSON 中提取纯文本正文，并对连写英文按词补空格。
    兼容：transcripts[].sentences[].words / text、直接 transcription 字符串、纯文本。"""
    try:
        data = json.loads(raw_text)
    except Exception:
        return raw_text.strip()
    if isinstance(data, str):
        return data.strip()
    transcripts = data.get("transcripts") or []
    if not transcripts:
        return (data.get("transcription") or data.get("text") or raw_text).strip()
    out = []
    for tr in transcripts:
        sents = tr.get("sentences") or []
        if not sents:
            out.append((tr.get("text") or "").strip())
            continue
        sent_strs = []
        for s in sents:
            raw = (s.get("text") or "").strip()
            words = [w.get("text", "").strip() for w in (s.get("words") or [])
                     if w.get("text")]
            if words and _needs_spacing(raw):
                sent = " ".join(words)
                # 补回句末标点（raw 末字符若标点则追加到末词之后）
                if raw and raw[-1] in "。！？.!?，" and sent and sent[-1] not in "。！？.!?，":
                    sent += raw[-1]
                sent_strs.append(sent)
            else:
                sent_strs.append(raw)
        out.append(" ".join(p for p in sent_strs if p))
    text = "\n\n".join(p for p in out if p)
    return text.strip() or raw_text.strip()


# 语音识别模型回退策略（2026-08-29 重构，详见 handover/对话记录）：
# - 优先走「实时(WebSocket)接口」：用户控制台已开通的 fun-asr-mtl-realtime 等实时模型。
#   它们无需公网可达的音频 URL（本地/容器内直接推 16k 单声道 PCM 流即可），各自独立配额；
#   老的异步文件转录接口（paraformer-v2/fun-asr）免费额度已耗尽时，用实时模型即可继续。
# - 仅在实时接口都不可用（未开通/配额耗尽）时，回退到老的异步文件转录接口。
#   注意：控制台里 `*-realtime` 模型名不能直接用于文件转录接口，必须走实时 WebSocket；
#   且控制台显示的 `qwen-audio-3.0-asr-flash`、`qwen3-asr-flash-realtime-*` 对应实时 API 的
#   实际模型名为 `qwen-audio-3.0-asr-flash-streaming`、以及 `fun-asr-mtl-realtime` 等。
REALTIME_ASR_MODELS = [
    "fun-asr-mtl-realtime",                 # 用户控制台"使用中"模型（多语种实时）
    "fun-asr-realtime",
    "qwen-audio-3.0-asr-flash-streaming",   # 控制台显示名 qwen-audio-3.0-asr-flash
    "fun-asr-flash-8k-realtime",            # 8k 电话音质兜底
]
ASR_FILE_MODELS = ["paraformer-v2", "fun-asr"]  # 老接口兜底（需 oss 直传）
# 兼容旧部署：若显式设置 ASR_MODEL 环境变量，作为首选模型
_ASR_MODEL_ENV = os.environ.get("ASR_MODEL", "").strip()


class _AsrModelUnavailable(Exception):
    """模型未开通配额 / 当前账号无可用额度，用于触发多模型回退。"""
    pass


def _dashscope_upload(api_key, model, file_path):
    """把本地音频上传到 DashScope 临时存储（48h 有效），返回 oss:// URL。

    不用「本服务公网路由 + file_urls 回拉」的方案，因为 EdgeOne 预览域名带访问
    鉴权（eo_token/Cookie），DashScope 回拉时拿到 401 HTML 页，会被当成音频解码
    从而报 DECODE_ERROR。上传方式不依赖本服务对公网可达。"""
    file_path = Path(file_path)
    r = requests.get("https://dashscope.aliyuncs.com/api/v1/uploads",
                     headers={"Authorization": f"Bearer {api_key}"},
                     params={"action": "getPolicy", "model": model}, timeout=60)
    if r.status_code != 200:
        if r.status_code == 403 and "AllocationQuota" in r.text:
            raise _AsrModelUnavailable(f"模型 {model} 未开通上传配额")
        raise RuntimeError(f"获取音频上传凭证失败 {r.status_code}: {r.text[:300]}")
    d = r.json()["data"]
    size_mb = file_path.stat().st_size / 1048576.0
    limit = float(d.get("max_file_size_mb") or 100)
    if size_mb > limit:
        raise RuntimeError(
            f"音频体积 {size_mb:.1f}MB 超过上传上限 {limit:.0f}MB，"
            f"请改用更短的视频或调小 MAX_AUDIO_SEC 环境变量")
    # 加随机前缀避免同名冲突（x-oss-forbid-overwrite=true 时重名会上传失败）。
    # 关键：key 只能用 ASCII（随机 hex + 原扩展名），绝不用原始文件名——
    # B 站视频常含中文/空格，拼出的 oss:// URL 含特殊字符会被 DashScope 拒收，
    # 转写任务 FAILED 报 "A valid file URL is required"（前几个 ASCII 名视频正常、后面的中文名视频才炸）。
    safe_name = f"{uuid.uuid4().hex}{file_path.suffix}"
    key = f"{d['upload_dir']}/{safe_name}"
    with open(file_path, "rb") as f:
        files = {
            "OSSAccessKeyId": (None, d["oss_access_key_id"]),
            "Signature": (None, d["signature"]),
            "policy": (None, d["policy"]),
            "x-oss-object-acl": (None, d["x_oss_object_acl"]),
            "x-oss-forbid-overwrite": (None, d["x_oss_forbid_overwrite"]),
            "key": (None, key),
            "success_action_status": (None, "200"),
            "file": (safe_name, f),
        }
        up = requests.post(d["upload_host"], files=files, timeout=900)
    if up.status_code != 200:
        raise RuntimeError(f"音频上传失败 {up.status_code}: {up.text[:300]}")
    return f"oss://{key}"


def _asr_try_model(wav_path, api_key, model, job_id):
    """对单个模型完成 上传→提交→轮询，返回转写文本；配额不足抛 _AsrModelUnavailable。"""
    if job_id:
        store.log(job_id, "语音转写", 50,
                  f"上传音频到 DashScope（{Path(wav_path).suffix.lstrip('.') or '未知'} 格式）")
    audio_url = _dashscope_upload(api_key, model, wav_path)
    if job_id:
        store.log(job_id, "语音转写", 55, f"DashScope 语音识别({model})中")
    url = "https://dashscope.aliyuncs.com/api/v1/services/audio/asr/transcription"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
               "X-DashScope-Async": "enable",
               "X-DashScope-OssResourceResolve": "enable"}
    body = {"model": model, "input": {"file_urls": [audio_url]}}
    r = requests.post(url, headers=headers, json=body, timeout=180)
    if r.status_code != 200:
        if r.status_code == 403 and "AllocationQuota" in r.text:
            raise _AsrModelUnavailable(f"模型 {model} 未开通转写配额")
        raise RuntimeError(f"DashScope 语音识别提交失败 {r.status_code}: {r.text[:300]}")
    out = r.json().get("output", {})
    task_id = out.get("task_id")
    if not task_id:
        raise RuntimeError("DashScope 语音识别未返回 task_id: " + str(r.text[:300]))
    get_url = f"https://dashscope.aliyuncs.com/api/v1/tasks/{task_id}"
    for _ in range(180):
        time.sleep(2)
        g = requests.get(get_url, headers=headers, timeout=30).json()
        st = g.get("output", {}).get("task_status")
        if st == "SUCCEEDED":
            res = g.get("output", {}).get("results", [{}])[0]
            txt_url = res.get("transcription_url") or res.get("url")
            if txt_url:
                return _parse_dashscope_transcript(requests.get(txt_url, timeout=30).text)
            return (res.get("transcription") or "").strip()
        if st == "FAILED":
            o = g.get("output", {})
            detail = o.get("message") or ""
            sub = (o.get("results") or [{}])[0]
            if sub.get("message"):
                detail = f"{detail} / {sub.get('message')}"
            raise RuntimeError("DashScope 语音识别失败: " + str(detail))
    raise RuntimeError("DashScope 语音识别轮询超时")


# ---------------------------------------------------------------------------
# 步骤 4c：DashScope 实时(WebSocket)语音识别 —— 优先路径
# ---------------------------------------------------------------------------
def _realtime_pcm(wav_path):
    """把 wav 解码为原始 16k 单声道 s16le PCM 字节（实时接口要求裸 PCM）。
    优先用 ffmpeg（容器/本地均内置），失败再回退到剥离 44 字节 wav 头。"""
    try:
        p = subprocess.run(
            ["ffmpeg", "-y", "-i", str(wav_path), "-ar", "16000", "-ac", "1",
             "-f", "s16le", "-loglevel", "error", "-"],
            capture_output=True,
        )
        if p.returncode == 0 and p.stdout:
            return p.stdout
    except Exception:
        pass
    data = Path(wav_path).read_bytes()
    if data[:4] == b"RIFF":
        return data[44:]
    return data


async def _realtime_ws(api_key, model, pcm, job_id):
    """实时 ASR 单次会话：run-task → 推流 PCM → finish-task → 收集最终结果。"""
    import websockets  # 延迟导入，避免无 websockets 环境整体加载失败
    uri = "wss://dashscope.aliyuncs.com/api-ws/v1/inference"
    tid = str(uuid.uuid4())
    sentences = []
    dur = len(pcm) / (16000 * 2)
    timeout = dur + 120  # 实时推流 + 余量
    t0 = time.time()
    async with websockets.connect(
        uri,
        additional_headers={"Authorization": f"Bearer {api_key}"},
        max_size=64 * 1024 * 1024,
        max_queue=1024,          # 放宽结果缓冲，避免读者被阻塞导致服务端 ping 无人应答
        ping_interval=15,        # 客户端主动发 ping，保活服务端侧连接
        ping_timeout=20,
        close_timeout=10,
    ) as ws:
        await ws.send(json.dumps({
            "header": {"action": "run-task", "task_id": tid, "streaming": "duplex"},
            "payload": {
                "task_group": "audio", "task": "asr", "function": "recognition",
                "model": model,
                "parameters": {"format": "pcm", "sample_rate": 16000,
                               "language_hints": ["zh"]},
                "input": {},
            },
        }))

        # 100ms 帧，按实时节奏推流（实时识别要求近似实时；每帧 3.2KB 在官方 1~16KB 建议区间内）
        chunks = [pcm[i:i + 3200] for i in range(0, len(pcm), 3200)]
        started = asyncio.Event()

        async def _sender():
            # 官方要求收到 task-started 后再推流；等待期间主循环用 ping 保活，避免 keepalive 超时
            try:
                await asyncio.wait_for(started.wait(), timeout=30)
            except asyncio.TimeoutError:
                pass
            for ch in chunks:
                await ws.send(ch)
                await asyncio.sleep(0.1)
            await ws.send(json.dumps({
                "header": {"action": "finish-task", "task_id": tid, "streaming": "duplex"},
                "payload": {"input": {}},
            }))

        # 推流与接收并发：修复「串行推流 → result-generated 在本地队列堆积 → 读者被阻塞 →
        # 服务端 ping 无人应答 → 1011 keepalive ping timeout 断连」的问题（长音频尤易触发）
        sender = asyncio.create_task(_sender())
        try:
            while time.time() - t0 < timeout:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=5.0)
                except asyncio.TimeoutError:
                    try:
                        await ws.ping()   # 空闲（等待 task-started / 结果）时主动保活
                    except Exception:
                        pass
                    continue
                if isinstance(msg, bytes):
                    continue
                ev = json.loads(msg)
                h = ev.get("header", {})
                e = h.get("event")
                if e == "task-started":
                    started.set()
                elif e == "result-generated":
                    s = ev.get("payload", {}).get("output", {}).get("sentence", {})
                    if s.get("sentence_end") and s.get("text"):
                        sentences.append(s["text"])
                elif e == "task-finished":
                    break
                elif e == "task-failed":
                    ec = h.get("error_code", "") or ""
                    em = h.get("error_message", "") or ""
                    if "ModelNotFound" in ec or "Quota" in ec or "quota" in em.lower():
                        raise _AsrModelUnavailable(f"{model}: {ec} {em}")
                    raise RuntimeError(f"DashScope 实时识别失败({model}): {ec} {em}")
            else:
                raise RuntimeError(f"DashScope 实时识别轮询超时({model})")
        finally:
            if not sender.done():
                sender.cancel()
                try:
                    await sender
                except asyncio.CancelledError:
                    pass
        return "\n".join(sentences).strip()


def _split_pcm_overlap(pcm, n, overlap_sec=1.5):
    """把整段 PCM 切成 n 块，相邻块之间留 overlap_sec 重叠，避免句子在分块边界被截断。

    每块独立识别（只收集 sentence_end 完整句），按序拼接即可，重叠区不会重复计数：
    跨边界的句子只会出现在「包含其句尾」的那一块里。"""
    total = len(pcm)
    chunk_samples = total // n
    if chunk_samples <= 0:
        return [pcm]
    ov = int(overlap_sec * 16000 * 2)
    parts, start = [], 0
    for i in range(n):
        end = total if i == n - 1 else start + chunk_samples
        s = (start - ov) if i > 0 else start
        if s < 0:
            s = 0
        parts.append(pcm[s:end])
        start = end
    return parts


async def _realtime_ws_parallel(api_key, model, parts, job_id):
    """并行跑多个实时 ASR 会话（同一模型）；任一分块失败则整体失败，触发上层模型回退。"""
    results = await asyncio.gather(
        *[_realtime_ws(api_key, model, p, job_id) for p in parts],
        return_exceptions=True)
    out = []
    for r in results:
        if isinstance(r, Exception):
            raise r
        out.append(r)
    return out


def _realtime_asr(api_key, model, wav_path, job_id=None):
    """实时 ASR（含并行分块加速）：长音频切 N 块并发识别，总耗时≈单块时长。

    实时接口服务端识别速率≈实时，单流无法快于音频时长；但把长音频切片、并发多路
    实时会话，即可把整段识别时间从「音频时长」压到「单块时长」(≈ 音频时长/N)，这是
    当前可用模型下唯一能显著缩短解析速度的手段（异步文件转录：本账号 qwen-audio-3.0
    -asr-flash 报 url error、paraformer-v2 免费额度耗尽，均不可用）。"""
    pcm = _realtime_pcm(wav_path)
    if not pcm:
        raise RuntimeError("音频解码为 PCM 失败，无法实时识别")
    dur = len(pcm) / (16000 * 2)
    max_chunks = max(1, int(os.environ.get("ASR_MAX_CHUNKS", "6")))
    chunk_sec = max(20, int(os.environ.get("ASR_CHUNK_SEC", "120")))
    n = int(dur // chunk_sec) + (1 if (dur % chunk_sec) > 0 else 0)
    n = max(1, min(max_chunks, n))
    if n <= 1:
        return asyncio.run(_realtime_ws(api_key, model, pcm, job_id))
    if job_id:
        try:
            store.log(job_id, "语音转写", 45,
                      f"音频 {dur:.0f}s 分 {n} 块并行实时识别加速（单块≈{dur/n:.0f}s）")
        except Exception:
            pass
    parts = _split_pcm_overlap(pcm, n)
    texts = asyncio.run(_realtime_ws_parallel(api_key, model, parts, job_id))
    return "\n".join(t for t in texts if t).strip()


def dashscope_asr(wav_path, api_key, model=None, job_id=None):
    """DashScope 语音识别，自动多模型回退。

    优先走实时(WebSocket)接口：用户控制台已开通的 fun-asr-mtl-realtime 等实时模型
    无需公网可达的音频 URL，本地/容器都能直接用，且各自独立配额（老的 paraformer-v2 /
    fun-asr 免费额度已耗尽时仍可用）。仅当实时接口都不可用（未开通/配额耗尽）时，
    才回退到老的异步文件转录接口（paraformer-v2/fun-asr，需 oss 直传）。

    历史坑：早期用「本服务公网路由 + file_urls 回拉」，带访问鉴权的域名返回 401 HTML
    被当成音频解码 → DECODE_ERROR；改用实时推流后不再依赖公网可达。"""
    rt_cands = []
    if _ASR_MODEL_ENV:
        rt_cands.append(_ASR_MODEL_ENV)
    if model:
        rt_cands.append(model)
    rt_cands += list(REALTIME_ASR_MODELS)
    rt_cands = list(dict.fromkeys(rt_cands))  # 去重保序

    for m in rt_cands:
        try:
            if job_id:
                store.log(job_id, "语音转写", 52, f"DashScope 实时识别({m})中")
            return _realtime_asr(api_key, m, wav_path, job_id), "zh(识别)"
        except _AsrModelUnavailable as e:
            if job_id:
                store.log(job_id, "语音转写", 56, f"{m} 不可用，尝试下一模型")
            continue

    # 兜底：老的异步文件转录接口
    for m in ASR_FILE_MODELS:
        try:
            if job_id:
                store.log(job_id, "语音转写", 52, f"DashScope 文件转录({m})中")
            return _asr_try_model(wav_path, api_key, m, job_id), "zh(识别)"
        except _AsrModelUnavailable as e:
            if job_id:
                store.log(job_id, "语音转写", 56, f"{m} 无配额，尝试下一模型")
            continue

    raise RuntimeError(
        "所有语音识别模型均不可用。请到阿里云百炼控制台开通实时语音识别模型"
        "（fun-asr-mtl-realtime 等每月赠送免费额度，需手动开通后才计入），开通页："
        "https://bailian.console.aliyun.com/#/model-market 。"
        "若仍想用老的 paraformer-v2/fun-asr，请在其免费额度耗尽后在控制台开通或付费。")


def do_asr(wav_path, api_key, job_id):
    """语音转写：DashScope Paraformer（需 Key，中文效果好、无需下载模型）。
    本环境未内置离线 Whisper 模型，未配置 Key 时给出明确提示。"""
    if api_key:
        return dashscope_asr(wav_path, api_key, job_id=job_id)
    raise RuntimeError(
        "服务端未配置 DASHSCOPE_API_KEY，且请求未携带 api_key，无法语音转写。"
        "请在服务端环境变量配置 Key（推荐），或在页面填入通义千问 Key。"
    )


# ---------------------------------------------------------------------------
# 主流水线
# ---------------------------------------------------------------------------
def run_pipeline(job_id, url, api_key, model, bili_cookie="", local_file=None, ep_title=None):
    """执行一次完整处理。

    url 为空且给了 local_file 时走「用户上传自有音视频」路径——这条入口不抓取
    第三方站点内容，是应用商店上架时的合规主路径。
    """
    workdir = tempfile.mkdtemp(prefix="vsb_")
    try:
        title = url or (Path(local_file).name if local_file else "")
        platform = "unknown"
        duration = None
        method = None
        raw = ""
        sub_lang = None

        if local_file:
            # —— 用户上传的本地音视频：直接转写，不涉及任何第三方抓取 ——
            platform = "upload"
            store.set_meta(job_id, {"title": title, "platform": platform, "duration": None})
            store.log(job_id, "读取文件", 10, f"已接收上传文件 {title}")
            audio = transcode_for_asr(Path(local_file), Path(workdir))
            raw, sub_lang = do_asr(audio, api_key, job_id)
            method = "asr"
        elif bili.is_bilibili(url):
            # —— B 站：直连数据 API（绕过 www 页面 412）——
            info = bili.get_info(url, cookie=bili_cookie)
            # 批量/选集场景下，整季(BV)主标题是「更上层名」，单集自身的分集名(ep_title)
            # 才是用户要的文件名。优先用分集名，缺省再退回主标题。
            title = ep_title or info["title"]
            platform = "Bilibili"
            duration = info.get("duration")
            store.set_meta(job_id, {"title": title, "platform": platform, "duration": duration})
            store.log(job_id, "提取字幕", 20,
                      "查询 B 站字幕" + ("（已带登录Cookie）" if bili_cookie else ""))
            stext, slang, is_auto = bili.get_subtitle(url, cookie=bili_cookie)
            if stext:
                raw = stext
                sub_lang = slang
                method = "subtitle"
                store.log(job_id, "提取字幕", 30, f"已获取{'AI' if is_auto else '人工'}字幕（{slang}）")
            else:
                # 无字幕：无需 SESSDATA，自动走语音转写（DashScope Key）。
                # 仅当用户主动提供登录 Cookie 时，才可能解锁需登录的字幕。
                if bili_cookie:
                    store.log(job_id, "下载音频", 35, "未取到字幕，下载音频做语音转写")
                else:
                    store.log(job_id, "下载音频", 35,
                         "无可用字幕（B站多数字幕需登录）。无需填写 Cookie："
                         "将自动语音转写" + ("（需 DashScope Key）" if not api_key else ""))
                wav = bili.download_audio_to(
                    url, Path(workdir) / "audio.wav",
                    int(os.environ.get("MAX_AUDIO_SEC", "7200")), cookie=bili_cookie)
                raw, sub_lang = do_asr(wav, api_key, job_id)
                method = "asr"
        else:
            # —— 通用直链 / 其他平台：yt-dlp ——
            info = extract_info(url)
            title = info.get("title") or url
            platform = info.get("extractor") or "unknown"
            duration = info.get("duration")
            store.set_meta(job_id, {"title": title, "platform": platform, "duration": duration})

            lang, is_auto = _pick_subtitle_lang(info)
            if lang:
                store.log(job_id, "提取字幕", 25, f"语言={lang} {'AI字幕' if is_auto else '人工字幕'}")
                try:
                    f = download_subtitles(url, lang, is_auto, workdir)
                    if f:
                        raw = parse_subtitle_file(f)
                except Exception as e:
                    raw = ""
                    store.log(job_id, "提取字幕", 25, f"字幕下载失败，转语音转写：{e}")
                if raw and len(raw.strip()) > 10:
                    method = "subtitle"
                    sub_lang = lang

            if method is None:
                store.log(job_id, "下载音频", 35, "未找到可用字幕，开始语音转写")
                wav = download_audio(url, workdir)
                raw, det_lang = do_asr(wav, api_key, job_id)
                method = "asr"
                sub_lang = det_lang

        raw = raw.strip()
        if len(raw) < 5:
            raise RuntimeError("未能获取到有效字幕/转写文本")

        clean = clean_transcript(raw)

        # 校验 + 梳理
        store.log(job_id, "AI 校验与梳理", 80, "调用通义千问" if api_key else "规则降级清洗")

        tech = None
        bilingual = None
        bl_lang = detect_lang(raw)
        if api_key:
            # 三处 LLM 调用（梳理 / 技术提取 / 中英文对照）相互独立，并发执行，
            # 把原来 ~3 倍串行耗时压缩到约 1 倍，显著缩短单视频处理时间。
            store.log(job_id, "对照翻译", 92,
                     f"调用通义千问生成中英文对照（原文为{'英文' if bl_lang == 'en' else '中文'}）")
            with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
                f_refine = ex.submit(llm_refine, raw, title, api_key, model)
                f_tech = ex.submit(llm_tech_extract, raw, title, api_key, model)
                src_for_bl = clean[:12000] if len(clean) > 12000 else clean
                if len(clean) > 12000:
                    store.log(job_id, "对照翻译", 92, "字幕较长，仅翻译前 12000 字用于对照")
                f_bl = ex.submit(llm_bilingual, src_for_bl, title, api_key, model, bl_lang)
                try:
                    structured = f_refine.result()
                except Exception as e:
                    structured = None
                    store.log(job_id, "AI 校验与梳理", 80, "梳理调用失败，降级为规则清洗：" + str(e)[:120])
                try:
                    tech = f_tech.result()
                except Exception as e:
                    store.log(job_id, "技术提取", 88, "技术提取失败：" + str(e)[:120])
                try:
                    bilingual = f_bl.result()
                except Exception as e:
                    store.log(job_id, "对照翻译", 92, "对照翻译失败：" + str(e)[:120])
            used_llm = bool(structured)
            if not structured:
                structured = clean
        else:
            structured = clean
            used_llm = False

        result = {
            "title": title,
            "platform": platform,
            "source_url": url,
            "method": method,
            "subtitle_lang": sub_lang,
            "duration": duration,
            "raw_transcript": raw,
            "clean_transcript": clean,
            "raw_segments": [p for p in clean.split("\n\n") if p.strip()],
            "bilingual": bilingual,
            "bilingual_lang": bl_lang,
            "tech": tech,
            "structured": structured,
            "used_llm": used_llm,
        }
        store.finish(job_id, result)
    except Exception as e:
        store.fail(job_id, str(e))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
