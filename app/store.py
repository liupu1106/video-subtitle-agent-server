# -*- coding: utf-8 -*-
"""任务状态存储与限流计数。

为什么需要这一层：云函数/多副本容器里，任务状态放进程内字典会导致
「提交任务的实例」和「轮询状态的实例」不是同一个，用户随机看到 404。
这里把状态统一放到 Redis；没配 REDIS_URL 时降级为内存，方便本地单进程开发。

Redis 键设计：
  job:{id}       JSON 快照（status/stage/progress/meta/result/error/updated_at），带 TTL
  job:{id}:log   LIST 逐行日志（用 RPUSH 追加，避免读改写竞争）
  jobs:running   ZSET  member=job_id score=心跳时间戳，用于全局并发计数（可自愈）
  rate:{scope}   计数器，带 TTL，用于限流
"""
import os
import json
import time
import threading

JOB_TTL = int(os.environ.get("JOB_TTL_SEC", "86400"))
# 任务超过该时间没有心跳，视为进程被重启/杀掉，标记为失败而不是一直 running
JOB_STALE_SEC = int(os.environ.get("JOB_STALE_SEC", "900"))


# ---------------------------------------------------------------------------
# 后端实现
# ---------------------------------------------------------------------------
class MemoryBackend:
    """单进程内存后端。仅用于本地开发；多副本部署必须用 Redis。"""

    name = "memory"

    def __init__(self):
        self._jobs = {}
        self._logs = {}
        self._rate = {}
        self._running = {}
        self._lock = threading.Lock()

    def save(self, job_id, data):
        with self._lock:
            self._jobs[job_id] = (json.dumps(data, ensure_ascii=False), time.time() + JOB_TTL)

    def load(self, job_id):
        with self._lock:
            item = self._jobs.get(job_id)
            if not item:
                return None
            raw, exp = item
            if time.time() > exp:
                self._jobs.pop(job_id, None)
                self._logs.pop(job_id, None)
                return None
        return json.loads(raw)

    def push_log(self, job_id, line):
        with self._lock:
            self._logs.setdefault(job_id, []).append(line)

    def logs(self, job_id):
        with self._lock:
            return list(self._logs.get(job_id, []))

    def incr(self, key, ttl):
        now = time.time()
        with self._lock:
            cnt, exp = self._rate.get(key, (0, now + ttl))
            if now > exp:
                cnt, exp = 0, now + ttl
            cnt += 1
            self._rate[key] = (cnt, exp)
            return cnt

    def heartbeat(self, job_id):
        with self._lock:
            self._running[job_id] = time.time()

    def clear_running(self, job_id):
        with self._lock:
            self._running.pop(job_id, None)

    def running_count(self):
        cutoff = time.time() - JOB_STALE_SEC
        with self._lock:
            for k in [k for k, v in self._running.items() if v < cutoff]:
                self._running.pop(k, None)
            return len(self._running)


class RedisBackend:
    """Redis 后端，供多副本共享任务状态。"""

    name = "redis"

    def __init__(self, url):
        import redis
        self.r = redis.from_url(url, decode_responses=True,
                                socket_connect_timeout=5, socket_timeout=5)
        self.r.ping()

    def save(self, job_id, data):
        self.r.setex(f"job:{job_id}", JOB_TTL, json.dumps(data, ensure_ascii=False))

    def load(self, job_id):
        raw = self.r.get(f"job:{job_id}")
        return json.loads(raw) if raw else None

    def push_log(self, job_id, line):
        k = f"job:{job_id}:log"
        p = self.r.pipeline()
        p.rpush(k, line)
        p.expire(k, JOB_TTL)
        p.execute()

    def logs(self, job_id):
        return self.r.lrange(f"job:{job_id}:log", 0, -1)

    def incr(self, key, ttl):
        p = self.r.pipeline()
        p.incr(key)
        p.expire(key, ttl)
        return p.execute()[0]

    def heartbeat(self, job_id):
        self.r.zadd("jobs:running", {job_id: time.time()})

    def clear_running(self, job_id):
        self.r.zrem("jobs:running", job_id)

    def running_count(self):
        # 先按心跳时间剔除已死实例留下的僵尸条目，计数因此可自愈
        self.r.zremrangebyscore("jobs:running", 0, time.time() - JOB_STALE_SEC)
        return self.r.zcard("jobs:running")


def _make_backend():
    url = os.environ.get("REDIS_URL", "").strip()
    if not url:
        print("[store] 未配置 REDIS_URL，使用内存后端（仅适合单进程本地开发）", flush=True)
        return MemoryBackend()
    try:
        b = RedisBackend(url)
        print(f"[store] 已连接 Redis: {url.rsplit('@', 1)[-1]}", flush=True)
        return b
    except Exception as e:
        print(f"[store] Redis 连接失败（{e}），降级为内存后端", flush=True)
        return MemoryBackend()


# ---------------------------------------------------------------------------
# 领域 API
# ---------------------------------------------------------------------------
class JobStore:
    def __init__(self, backend=None):
        self.b = backend or _make_backend()

    @property
    def backend_name(self):
        return self.b.name

    # -- 任务生命周期 --
    def create(self, job_id, kind="job", total=0, beat=True):
        data = {"status": "running", "stage": "排队", "progress": 0,
                "created_at": time.time(), "updated_at": time.time(), "kind": kind}
        if kind == "batch":
            data["total"] = total
            data["job_ids"] = []
        self.b.save(job_id, data)
        # 批量父任务不占心跳名额：仅子任务计入全局并发
        if beat:
            self.b.heartbeat(job_id)

    def set_batch_children(self, batch_id, children):
        """children: [{id, title, ep_no}]。记录子任务清单供聚合与展示。"""
        d = self.b.load(batch_id) or {}
        d["job_ids"] = [c["id"] for c in children]
        d["titles"] = {c["id"]: c.get("title", "") for c in children}
        d["ep_nos"] = {c["id"]: c.get("ep_no") for c in children}
        d["total"] = len(children)
        d["updated_at"] = time.time()
        self.b.save(batch_id, d)

    def log(self, job_id, stage, progress, msg=""):
        """写进度 + 追加日志行。同时刷新心跳，供并发计数与 stale 判定。"""
        d = self.b.load(job_id) or {}
        d.update({"stage": stage, "progress": progress, "updated_at": time.time()})
        self.b.save(job_id, d)
        line = f"[{stage}] {msg}" if msg else f"[{stage}]"
        self.b.push_log(job_id, line)
        self.b.heartbeat(job_id)
        print(f"{job_id} {line}", flush=True)

    def set_meta(self, job_id, meta):
        d = self.b.load(job_id) or {}
        d["meta"] = meta
        d["updated_at"] = time.time()
        self.b.save(job_id, d)

    def finish(self, job_id, result):
        d = self.b.load(job_id) or {}
        d.update({"status": "done", "stage": "完成", "progress": 100,
                  "result": result, "updated_at": time.time()})
        self.b.save(job_id, d)
        self.b.clear_running(job_id)

    def fail(self, job_id, error):
        d = self.b.load(job_id) or {}
        d.update({"status": "error", "stage": "错误", "progress": 100,
                  "error": str(error), "updated_at": time.time()})
        self.b.save(job_id, d)
        self.b.push_log(job_id, f"[错误] {error}")
        self.b.clear_running(job_id)

    def get(self, job_id):
        """读取任务快照。running 但心跳超时的任务判定为中断，避免前端无限轮询。"""
        d = self.b.load(job_id)
        if not d:
            return None
        if d.get("kind") == "batch":
            return self._get_batch(job_id, d)
        if d.get("status") == "running" and \
                time.time() - d.get("updated_at", 0) > JOB_STALE_SEC:
            d["status"] = "error"
            d["error"] = "任务已中断（服务重启或执行超时），请重新提交"
        d["log"] = self.b.logs(job_id)
        return d

    def _get_batch(self, job_id, d):
        """聚合整季批量任务：逐个读取子任务，统计进度与完成数。"""
        ids = d.get("job_ids") or []
        titles = d.get("titles") or {}
        ep_nos = d.get("ep_nos") or {}
        children, done = [], 0
        errored = running = 0
        prog_sum = 0
        for cid in ids:
            c = self.b.load(cid) or {}
            st = c.get("status", "missing")
            pr = c.get("progress", 0)
            children.append({
                "id": cid,
                "title": titles.get(cid, ""),
                "ep_no": ep_nos.get(cid),
                "status": st,
                "progress": pr,
                "has_result": bool(c.get("result")),
                "error": c.get("error"),
                # 完成时把 result 内联返回，前端即可直接缓存并用于整批查看/批量导出，
                # 无需再逐子任务请求（多副本/缩容场景也能稳定拿到结果）。
                "result": c.get("result") if st == "done" and c.get("result") else None,
            })
            prog_sum += pr
            if st == "done":
                done += 1
            elif st == "error":
                errored += 1
            elif st == "missing":
                errored += 1
            else:
                running += 1
        total = max(1, d.get("total", len(ids)))
        d["children"] = children
        d["done_count"] = done
        d["error_count"] = errored
        d["running_count"] = running
        d["progress"] = round(prog_sum / len(ids)) if ids else 0
        # 全部子任务结束（成功或失败）即视为批量完成
        d["status"] = "done" if running == 0 else "running"
        d["stage"] = f"整季批量：{done}/{total} 完成" + (f"，{errored} 失败" if errored else "")
        d["log"] = self.b.logs(job_id)
        return d

    # -- 限流 / 并发 --
    def running_count(self):
        return self.b.running_count()

    def hit_rate_limit(self, scope, limit, window):
        """返回 (是否超限, 当前计数)。limit<=0 表示不限制。"""
        if limit <= 0:
            return False, 0
        cnt = self.b.incr(f"rate:{scope}", window)
        return cnt > limit, cnt


store = JobStore()
