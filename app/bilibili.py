# -*- coding: utf-8 -*-
"""B 站直连客户端：绕过 www 页面的 412，使用 api.bilibili.com 数据接口。
提供：视频信息、字幕列表与下载、音频直链获取。
适用于本沙箱网络（仅 api.bilibili.com 可达，www.bilibili.com 返回 412）。

字幕说明：B 站大量视频的字幕标记为 need_login_subtitle=true，未登录时字幕列表为空。
传入含 SESSDATA 的登录 Cookie 可解锁这些字幕，优先于语音转写。
Cookie 既可按请求传入（推荐，函数参数 cookie=），也可通过环境变量 BILI_COOKIE 设默认值。
"""
import re
import os
import time
import json
import hashlib
import urllib.parse
import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
_WBI_ENC = [46,47,18,2,53,8,23,32,15,50,10,31,58,3,45,35,27,43,5,49,33,9,42,
            19,29,28,14,39,12,38,41,13,37,48,7,16,24,55,40,61,26,17,0,1,60,51,
            30,4,22,25,54,21,56,59,6,63,57,62,11,36,20,34,44,52]

# 按 cookie 串缓存 session 与 wbi mixin（未登录 / 不同账号互不干扰）
_sessions = {}
_mixins = {}


def _build_base_session():
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Referer": "https://www.bilibili.com"})
    try:
        spi = s.get("https://api.bilibili.com/x/frontend/finger/spi", timeout=15).json()["data"]
        s.cookies.set("buvid3", spi["b_3"])
        s.cookies.set("buvid4", spi["b_4"])
    except Exception:
        pass
    return s


def _parse_cookie(cookie):
    d = {}
    if not cookie:
        return d
    for part in cookie.split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            d[k.strip()] = v.strip()
    return d


def get_session(cookie=None):
    """返回带 buvid + 可选登录 Cookie 的 session；相同 cookie 串复用缓存。"""
    key = cookie or ""
    if key not in _sessions:
        s = _build_base_session()
        for k, v in _parse_cookie(cookie).items():
            s.cookies.set(k, v)
        _sessions[key] = s
    return _sessions[key]


def _get_mixin(cookie=None):
    key = cookie or ""
    if key not in _mixins:
        s = get_session(cookie)
        nav = s.get("https://api.bilibili.com/x/web-interface/nav", timeout=15).json()["data"]["wbi_img"]
        img = nav["img_url"].rsplit("/", 1)[-1].split(".")[0]
        sub = nav["sub_url"].rsplit("/", 1)[-1].split(".")[0]
        _mixins[key] = "".join((img + sub)[i] for i in _WBI_ENC)[:32]
    return _mixins[key]


def _sign(params, cookie=None):
    mixin = _get_mixin(cookie)
    p = dict(sorted({**params, "wts": int(time.time())}.items()))
    q = urllib.parse.urlencode(p, safe="!")
    p["w_rid"] = hashlib.md5((q + mixin).encode()).hexdigest()
    return p


def is_bilibili(url):
    return "bilibili.com" in url or "b23.tv" in url


_BVID_RE = re.compile(r"BV[0-9A-Za-z]+")


def _bvid(url, cookie=None):
    """从链接中解析 BVID；b23.tv 等短链先跟随重定向还原成含 BV 的真实链接。"""
    m = _BVID_RE.search(url)
    if m:
        return m.group(0)
    # 短链：跟随 302 重定向，从最终 Location 提取 BV
    if "b23.tv" in url:
        s = get_session(cookie)
        try:
            r = s.get(url, timeout=20, allow_redirects=True, stream=True)
            r.close()
            final = r.url or ""
            m2 = _BVID_RE.search(final)
            if m2:
                return m2.group(0)
        except Exception:
            pass
    return None


def _page_num(url):
    """从链接中解析分P序号（?p=16），缺省为 1。"""
    m = re.search(r"[?&]p=(\d+)", url or "")
    try:
        return max(1, int(m.group(1))) if m else 1
    except Exception:
        return 1


_EP_RE = re.compile(r"ep(\d+)", re.I)
_SS_RE = re.compile(r"ss(\d+)", re.I)
_EP_ID_Q = re.compile(r"[?&]ep_id=(\d+)", re.I)
_SS_ID_Q = re.compile(r"[?&]season_id=(\d+)", re.I)


def _resolve_bangumi(url):
    """解析番剧/影视链接，返回 (bvid, cid, title, duration_sec)。

    支持：/bangumi/play/ep812858、?ep_id=812858、/bangumi/play/ss12345（整季取第 1 集）。
    仅靠 api.bilibili.com 即可，无需抓取 www 页面。返回 None 表示这不是番剧链接。
    duration 在接口里单位是毫秒，这里换算成秒。"""
    s = get_session()
    ep_m = _EP_RE.search(url or "") or _EP_ID_Q.search(url or "")
    ss_m = _SS_RE.search(url or "") or _SS_ID_Q.search(url or "")
    if not (ep_m or ss_m):
        return None
    eps = []
    if ep_m:
        epid = int(ep_m.group(1))
        r = s.get("https://api.bilibili.com/pgc/view/web/ep/list",
                  params={"ep_id": epid}, timeout=20).json()
        eps = (r.get("result") or {}).get("episodes") or []
        ep = next((e for e in eps if e.get("id") == epid), None)
        if not ep and eps:        # ep_id 不在列表（如整季入口）时退而取首集
            ep = eps[0]
    else:
        ssid = int(ss_m.group(1))
        r = s.get("https://api.bilibili.com/pgc/view/web/season",
                  params={"season_id": ssid}, timeout=20).json()
        res = r.get("result") or {}
        eps = res.get("episodes") or res.get("main_section", {}).get("episodes") or []
        ep = eps[0] if eps else None
    if not ep:
        raise RuntimeError("未找到该番剧/影视剧集信息")
    bvid = ep.get("bvid")
    cid = ep.get("cid")
    if not bvid or not cid:
        raise RuntimeError("该番剧集缺少 bvid/cid（可能已下架）")
    dur_ms = ep.get("duration") or 0
    title = ep.get("long_title") or ep.get("show_title") or ep.get("title") or bvid
    return bvid, cid, title, (dur_ms // 1000 if dur_ms else None)


def is_bangumi(url):
    """判断链接是否为番剧/影视页（ep{id} 或 ss{id}，含 ?ep_id= / ?season_id=）。"""
    u = url or ""
    return bool(_EP_RE.search(u) or _EP_ID_Q.search(u) or _SS_RE.search(u) or _SS_ID_Q.search(u))


def _episodes_from_list(eps):
    """把 pgc 接口返回的 episodes 数组标准化为 [{ep_id,bvid,cid,title,duration}]。"""
    out = []
    for e in eps:
        dur_ms = e.get("duration") or 0
        out.append({
            "ep_id": e.get("id"),
            "bvid": e.get("bvid"),
            "cid": e.get("cid"),
            "title": e.get("long_title") or e.get("show_title") or e.get("title") or e.get("bvid"),
            "duration": dur_ms // 1000 if dur_ms else None,
        })
    return out


def get_season_episodes(url):
    """返回番剧/影视整季的剧集列表 [{ep_id,bvid,cid,title,duration}]（按顺序）。

    输入可为 ep{id}（自动定位其所属整季）或 ss{id}（整季入口），也支持
    ?ep_id= / ?season_id= 查询参数。非番剧链接返回空列表 []。"""
    s = get_session()
    ep_m = _EP_RE.search(url or "") or _EP_ID_Q.search(url or "")
    ss_m = _SS_RE.search(url or "") or _SS_ID_Q.search(url or "")
    if not (ep_m or ss_m):
        return []
    if ep_m:
        epid = int(ep_m.group(1))
        r = s.get("https://api.bilibili.com/pgc/view/web/ep/list",
                  params={"ep_id": epid}, timeout=20).json()
        eps = (r.get("result") or {}).get("episodes") or []
    else:
        ssid = int(ss_m.group(1))
        r = s.get("https://api.bilibili.com/pgc/view/web/season",
                  params={"season_id": ssid}, timeout=20).json()
        res = r.get("result") or {}
        eps = res.get("episodes") or res.get("main_section", {}).get("episodes") or []
    return _episodes_from_list(eps)


def get_video_pages(url, cookie=None):
    """返回 UGC 视频（BVxxxx）的全部分P列表 [{page,bvid,cid,title,duration}]（按分P顺序）。

    无论传入链接带的是 ?p=16 还是不带 p，都返回该视频的**全部分P**，
    用于「整集选集批量解析」。单P视频也返回 1 个元素。非 BV 链接返回空列表 []。"""
    bvid = _bvid(url, cookie)
    if not bvid:
        return []
    s = get_session(cookie)
    view = s.get("https://api.bilibili.com/x/web-interface/view",
                 params={"bvid": bvid}, timeout=20).json()["data"]
    base_title = view.get("title") or bvid
    pages = view.get("pages") or []
    if not pages:
        # 单P：直接用 view 本身的 cid
        return [{
            "page": 1,
            "bvid": bvid,
            "cid": view.get("cid"),
            "title": base_title,
            "duration": view.get("duration"),
        }]
    out = []
    for pg in pages:
        pno = pg.get("page") or (len(out) + 1)
        part = (pg.get("part") or "").strip()
        title = (f"P{pno} · {part}" if part else f"P{pno}") or base_title
        out.append({
            "page": pno,
            "bvid": bvid,
            "cid": pg.get("cid"),
            "title": title,
            "duration": pg.get("duration"),
        })
    return out


def get_info(url, cookie=None):
    """返回 dict: title, bvid, cid, duration, extractor, page

    多分P视频必须用对应分P的 cid，否则字幕/音频都会取到第 1 集的内容。"""
    s = get_session(cookie)
    # 番剧/影视：ep{id} 或 ss{id} 链接先走专用解析（接口直接给 bvid+cid）
    bang = _resolve_bangumi(url)
    if bang:
        bvid, cid, title, duration = bang
        return {
            "title": title,
            "bvid": bvid,
            "cid": cid,
            "duration": duration,
            "page": 1,
            "extractor": "Bilibili",
        }
    bvid = _bvid(url, cookie)
    if not bvid:
        raise RuntimeError("无法从链接解析 BVID")
    view = s.get("https://api.bilibili.com/x/web-interface/view",
                 params={"bvid": bvid}, timeout=20).json()["data"]
    title = view.get("title") or bvid
    cid = view.get("cid")
    duration = view.get("duration")
    pages = view.get("pages") or []
    pno = _page_num(url)
    if pages:
        if pno > len(pages):
            raise RuntimeError(f"链接指定第 {pno} 集，但该视频只有 {len(pages)} 集")
        pg = pages[pno - 1]
        cid = pg.get("cid") or cid
        duration = pg.get("duration") or duration
        part = (pg.get("part") or "").strip()
        if len(pages) > 1:
            title = f"{title} · P{pno}" + (f" {part}" if part else "")
    return {
        "title": title,
        "bvid": bvid,
        "cid": cid,
        "duration": duration,
        "page": pno,
        "extractor": "Bilibili",
    }


def need_login_subtitle(url, cookie=None):
    """该视频的字幕是否需要登录才能获取（用于前端提示）。"""
    info = get_info(url, cookie)
    s = get_session(cookie)
    r = s.get("https://api.bilibili.com/x/player/wbi/v2",
              params=_sign({"bvid": info["bvid"], "cid": info["cid"], "platform": "pc",
                            "web_location": "player"}, cookie), timeout=20).json()
    d = r.get("data") or {}
    return bool(d.get("need_login_subtitle"))


def _collect_subtitles(info, cookie=None):
    """返回字幕条目列表 [{lang, doc, url, ai}]"""
    s = get_session(cookie)
    bvid, cid = info["bvid"], info["cid"]
    r = s.get("https://api.bilibili.com/x/player/wbi/v2",
              params=_sign({"bvid": bvid, "cid": cid, "platform": "pc",
                            "web_location": "player"}, cookie), timeout=20).json()
    data = r.get("data") or {}
    out = []
    for key in ("subtitle_list", "subtitle"):
        node = data.get(key)
        if isinstance(node, dict):
            for it in node.get("list", []) or []:
                out.append({
                    "lang": it.get("lan"),
                    "doc": it.get("lan_doc"),
                    "url": it.get("subtitle_url"),
                    "ai": bool(it.get("ai_type") or it.get("ai_closed") or key == "subtitle_list"),
                })
        elif isinstance(node, list):
            for it in node:
                out.append({"lang": it.get("lan"), "doc": it.get("lan_doc"),
                            "url": it.get("subtitle_url"), "ai": bool(it.get("ai_type"))})
    return out


def get_subtitle(url, cookie=None):
    """优先返回 (文本, 语言, 是否AI字幕)；无字幕返回 (None,None,None)"""
    info = get_info(url, cookie)
    subs = _collect_subtitles(info, cookie)
    if not subs:
        return None, None, None
    zh = ["zh-CN", "zh-Hans", "zh-Hant", "zh", "chi", "cn"]
    en = ["en", "eng", "en-US"]
    pick = None
    for lang in zh:
        for it in subs:
            if it["lang"] == lang and not it["ai"]:
                pick = it; break
        if pick:
            break
    if not pick:
        for lang in zh:
            for it in subs:
                if it["lang"] == lang:
                    pick = it; break
            if pick:
                break
    if not pick:
        for lang in en:
            for it in subs:
                if it["lang"] == lang:
                    pick = it; break
            if pick:
                break
    if not pick:
        pick = subs[0]
    text = _download_subtitle_text(pick["url"], cookie)
    if text and len(text.strip()) > 10:
        return text.strip(), pick["lang"], pick["ai"]
    return None, None, None


def _download_subtitle_text(url, cookie=None):
    if not url:
        return ""
    if url.startswith("//"):
        url = "https:" + url
    s = get_session(cookie)
    try:
        data = s.get(url, timeout=20).json()
    except Exception:
        return ""
    body = data.get("body") or []
    return "\n".join(b.get("content", "") for b in body if b.get("content"))


def get_audio_urls(url, cookie=None):
    """返回 B 站 DASH 音频候选直链列表（baseUrl 在前，backupUrl 在后，按码率排序）。

    不同 CDN 节点（upos-sz-mirrorhw / upos-hz-mirrorakam 等）连通性不同，下载时
    逐个回退可显著提升成功率（缓解单节点 HTTPSConnectionPool 失败）。"""
    info = get_info(url, cookie)
    s = get_session(cookie)
    pu = s.get("https://api.bilibili.com/x/player/wbi/playurl",
               params=_sign({"bvid": info["bvid"], "cid": info["cid"],
                             "qn": "64", "fnval": "16", "fourk": "1",
                             "platform": "pc"}, cookie), timeout=20).json()
    dash = (pu.get("data") or {}).get("dash") or {}
    audios = dash.get("audio") or []
    if not audios:
        raise RuntimeError("未获取到音频流（视频可能需登录/付费）")
    # 选码率最高
    audios.sort(key=lambda a: a.get("bandwidth", 0), reverse=True)
    cands = []
    for a in audios:
        base = a.get("baseUrl")
        if base and base not in cands:
            cands.append(base)
        for bk in (a.get("backupUrl") or []):
            if bk and bk not in cands:
                cands.append(bk)
    return cands


def get_audio_url(url, cookie=None):
    """返回 B 站 DASH 音频首选直链（兼容旧调用，等价于 get_audio_urls()[0]）。"""
    cands = get_audio_urls(url, cookie)
    if not cands:
        raise RuntimeError("未获取到音频流（视频可能需登录/付费）")
    return cands[0]


def download_audio_to(url, dst_wav, max_sec=7200, cookie=None):
    """下载 B 站音频。有 ffmpeg 时转 16k 单声道 wav，否则保留原始 m4a。
    返回实际可用的音频文件路径（扩展名与真实编码保持一致）。

    健壮性：依次尝试所有 CDN 候选直链(baseUrl + backupUrl)，每个最多重试 3 次并退避，
    任一成功即停止——缓解单 CDN 节点（如 upos-sz-mirrorhw）连接失败。"""
    import os
    import shutil as _sh
    import subprocess
    urls = get_audio_urls(url, cookie)
    s = get_session(cookie)
    tmp = dst_wav.with_suffix(".m4a")
    downloaded = False
    last_err = None
    for u in urls:
        for attempt in range(3):
            try:
                with s.get(u, timeout=120, stream=True) as resp:
                    resp.raise_for_status()
                    with open(tmp, "wb") as f:
                        for chunk in resp.iter_content(1024 * 256):
                            if chunk:
                                f.write(chunk)
                if tmp.stat().st_size > 0:
                    downloaded = True
                    break
                raise RuntimeError("下载内容为空")
            except Exception as e:
                last_err = e
                if attempt < 2:
                    time.sleep(2 * (attempt + 1))  # 退避 2s / 4s
        if downloaded:
            break
    if not downloaded:
        raise RuntimeError("B站音频下载失败（所有 CDN 直链均不可用）：%s" % (last_err,))
    if _sh.which("ffmpeg"):
        # 合并「抽取音频+静音切除+解码为 PCM」为单次 ffmpeg，直接产 .pcm 供实时 ASR，
        # 省掉原流程里 转 wav → 静音切除 → 再解码 PCM 的两次串行 ffmpeg 开销
        dst_pcm = dst_wav.with_suffix(".pcm")
        do_silence = os.environ.get("ASR_SILENCE_REMOVE", "1") not in ("0", "false", "no")
        af = ""
        if do_silence:
            af = ("silenceremove=start_periods=1:stop_periods=-1:"
                  "start_duration=0.3:stop_duration=0.5:"
                  "start_threshold=-35dB:stop_threshold=-35dB")
        cmd = ["ffmpeg", "-y", "-i", str(tmp)]
        if af:
            cmd += ["-af", af]
        cmd += ["-ar", "16000", "-ac", "1", "-f", "s16le", "-loglevel", "error"]
        # 仅当 max_sec 为有效正数时追加时长裁剪；否则 ffmpeg 收到 "-t None/0" 会失败，
        # 导致预处理静默回退为「未处理原文件」（吃掉静音切除+转码提速）。
        if max_sec and float(max_sec) > 0:
            cmd += ["-t", str(int(float(max_sec)))]
        cmd += [str(dst_pcm)]
        try:
            p = subprocess.run(cmd, capture_output=True, timeout=600)
            if p.returncode == 0 and dst_pcm.exists() and dst_pcm.stat().st_size > 0:
                return dst_pcm
        except Exception:
            pass
    # 无 ffmpeg：直接用 m4a（AAC），识别服务支持该格式；
    # 不能改名成 .wav，否则会按 wav 解码 AAC 数据而报 DECODE_ERROR
    return tmp


def shutil_copy(src, dst):
    import shutil
    shutil.copy(src, dst)
