# -*- coding: utf-8 -*-
"""音乐数据刮削 Web 应用（FastAPI）。

数据源均为插件：从插件目录（安装向导配置）加载单文件 .py。
启动: uvicorn webapp.main:app --host 0.0.0.0 --port 6666
"""
from __future__ import annotations

import os
import threading
import urllib.parse
import time

import json

from fastapi import FastAPI, File, HTTPException, Response, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import db, scheduler
from musicmeta.fields import FIELD_MAP, FIELDS, active_fields, parse_active

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

_last_request_ts: float = time.time()   # 最近一次 HTTP 请求时间（空闲退出监控用）

app = FastAPI(title="音乐数据刮削", version="0.1.0")


def _resolve_src(meta_source: str, interval: float):
    """按候选来源解析数据源插件；不可用时尝试 qqmusic 插件；都没有返回 None。

    数据源均为插件（本安装包不内置），因此 QQ 兜底依赖用户安装了 qqmusic.py。
    """
    from musicmeta.sources.registry import get_source
    for name in (meta_source or "", "qqmusic"):
        if not name:
            continue
        try:
            return get_source(name, min_interval=interval)
        except Exception:
            continue
    return None

# 飞牛统一网关前缀（如 /app/music-meta-web）。
# 网关可能把带前缀的完整路径转发给应用，也可能已剥离前缀；
# 这里做兼容处理：若请求路径以该前缀开头则剥离后再路由。
GATEWAY_PREFIX = os.environ.get("MMW_GATEWAY_PREFIX", "/app/music-meta-web").rstrip("/")


@app.middleware("http")
async def strip_gateway_prefix(request, call_next):
    global _last_request_ts
    _last_request_ts = time.time()   # 记录任何请求活动（空闲自动退出用）
    if GATEWAY_PREFIX:
        path = request.scope.get("path", "")
        if path == GATEWAY_PREFIX or path.startswith(GATEWAY_PREFIX + "/"):
            new_path = path[len(GATEWAY_PREFIX):] or "/"
            request.scope["path"] = new_path
            request.scope["raw_path"] = new_path.encode("utf-8")
    return await call_next(request)


@app.on_event("startup")
def _startup() -> None:
    db.init_db()
    # 元数据缓存目录：优先用配置里指定的目录（空则用应用数据目录）
    _apply_cache_dir(db.get_config())
    # 通用元数据缓存初始化：首次启动自动迁移旧版 QQ 缓存库
    try:
        from musicmeta import cache as _mcache
        _mcache.init()
    except Exception:
        pass


# ---------------- 页面 ----------------

@app.get("/")
def index():
    # no-store：防止浏览器缓存旧版页面导致按钮失效/功能错乱
    return FileResponse(
        os.path.join(STATIC_DIR, "index.html"),
        headers={"Cache-Control": "no-store"})


# ---------------- 配置 ----------------

class ConfigBody(BaseModel):
    music_dir: str | None = None
    recursive: bool | None = None
    write_enabled: bool | None = None
    #: 生效字段（勾选的字段才会显示、才会写入）；见 musicmeta.fields
    active_fields: list[str] | None = None
    min_interval: float | None = None
    concurrency: int | None = None
    source_limit: int | None = None
    request_timeout: int | None = None
    request_retries: int | None = None
    run_limit: int | None = None
    pause_every: int | None = None
    pause_seconds: int | None = None
    cache_dir: str | None = None
    matching_mode: str | None = None
    acoustid_key: str | None = None
    source: str | None = None
    plugins_dir: str | None = None


@app.get("/api/config")
def get_config():
    cfg = db.get_config()
    cfg["active_fields"] = ",".join(active_fields(cfg))
    # 只读信息：应用数据目录 / 元数据缓存实际落盘位置（供「目录设置」展示）
    from musicmeta import cache as _mcache
    cfg["data_dir"] = (os.environ.get("TRIM_PKGVAR") or "").strip()
    try:
        cfg["manual_stats"] = db.manual_done_stats()
    except Exception:
        cfg["manual_stats"] = {}
    try:
        cfg["cache_db"] = _mcache.stats().get("db", "")
    except Exception:
        cfg["cache_db"] = ""
    return cfg


@app.get("/api/sources")
def get_sources():
    """可用元数据源列表（内置 + 用户插件）+ 当前插件目录。"""
    cfg = db.get_config()
    pdir = cfg.get("plugins_dir", "").strip()
    if pdir:
        os.environ["MMW_PLUGINS_DIR"] = pdir
    from musicmeta.sources.registry import list_sources
    return {"sources": list_sources(), "plugins_dir": pdir}


@app.get("/api/fields")
def get_fields():
    """可刮削字段清单（供设置页与手动刮削窗口使用）。"""
    return {
        "fields": [{"key": f.key, "label": f.label, "special": f.special}
                   for f in FIELDS],
        "active": active_fields(db.get_config()),
    }


@app.put("/api/config")
def put_config(body: ConfigBody):
    patch = {}
    for name, val in body.dict(exclude_none=True).items():
        if name == "active_fields":
            patch[name] = ",".join(parse_active(val))
        else:
            patch[name] = str(val)
    cfg = db.set_config(patch)
    if "cache_dir" in patch:
        _apply_cache_dir(cfg)
    return cfg


def _apply_cache_dir(cfg: dict) -> None:
    """让「元数据缓存目录」配置生效（切目录后自动重开缓存库）。"""
    from musicmeta import cache as _mcache
    try:
        _mcache.set_dir(cfg.get("cache_dir", ""))
    except Exception as exc:  # noqa: BLE001
        print(f"[config] 切换缓存目录失败（保持原目录）: {exc}")


# ---------------- 任务 ----------------

class ScanBody(BaseModel):
    path: str | None = None
    recursive: bool | None = None


@app.post("/api/scan")
def scan(body: ScanBody):
    """扫描目录：只解析文件名生成待处理清单，不修改任何音乐文件。"""
    cfg = db.get_config()
    path = body.path or cfg.get("music_dir", "")
    recursive = body.recursive if body.recursive is not None else cfg.get("recursive") == "1"
    if not os.path.isdir(path):
        raise HTTPException(400, f"目录不存在或不可读: {path}")
    files = scheduler.collect_audio_files(path, recursive)
    added = db.add_tasks(files)
    return {"path": path, "total": len(files), "added": added}


@app.post("/api/tasks/reset")
def reset_tasks():
    db.reset_tasks()
    return {"ok": True}


@app.post("/api/tasks/unskip")
def unskip_tasks():
    """撤销误批量跳过：skipped 恢复为 manual_pending。"""
    n = db.unskip_all()
    return {"restored": n}


class DeleteTaskBody(BaseModel):
    file: str
    delete_file: bool = False   # True=连同音乐文件本体一起删除


@app.post("/api/tasks/delete")
def delete_task(body: DeleteTaskBody):
    """删除单个任务（人工辅助/跳过队列用）。

    delete_file=False：只删除数据库里的任务记录（候选/决策/人工记忆一并清理），
    磁盘上的音乐文件保留；
    delete_file=True：先删除磁盘上的音乐文件本体，再删除任务记录。
    """
    path = body.file
    if not path or not isinstance(path, str) or not path.strip():
        raise HTTPException(400, "缺少文件路径")
    # 只允许删除已授权音乐目录内的文件（防误删其它位置）
    root = os.path.abspath(db.get_config().get("music_dir", "") or "/")
    f = os.path.abspath(path)
    if not f.startswith(root + os.sep) and f != root:
        raise HTTPException(403, f"文件不在已配置的音乐目录内，拒绝删除: {path}")

    file_deleted = False
    if body.delete_file:
        if os.path.isfile(f):
            try:
                os.remove(f)
                file_deleted = True
            except OSError as exc:
                raise HTTPException(500, scheduler._cn_err(exc))
        else:
            file_deleted = False  # 文件本就不存在，视为已删
    removed = db.delete_task(f)
    _invalidate_filter_cache(f)   # 任务删除：过滤结果更新
    return {"ok": True, "removed": removed, "file_deleted": file_deleted}


@app.post("/api/tasks/retry-errors")
def retry_error_tasks():
    """把 error 任务恢复为 pending 重试。"""
    n = db.retry_errors()
    return {"retried": n}


@app.post("/api/write-auto")
def write_auto():
    """写入阶段：把已自动匹配（auto_ok）但尚未写入的文件按缓存候选补写。

    需要：网页勾选「启用写入」且音乐目录为可写挂载。
    返回写入数与跳过数（无候选的跳过）。
    """
    cfg = db.get_config()
    if cfg.get("write_enabled") != "1":
        raise HTTPException(400, "未启用写入：请先在配置里勾选「启用写入」")
    src = _resolve_src("", float(cfg.get("min_interval", "0.3")))
    from musicmeta.sources.base import SongMeta
    written = skipped = failed = 0
    for t in db.list_tasks("auto_ok", limit=100000):
        path = t["path"]
        if db.get_decision(path):
            continue  # 已处理过
        cands = db.get_candidates(path)
        if not cands:
            skipped += 1
            continue
        top = cands[0]
        meta = SongMeta(
            title=top["title"], artist=top["artist"],
            album=top["album"], date=top["year"],
            song_id=top["songmid"], album_id=top["albummid"],
            source=top.get("source") or "")
        try:
            try:
                if src is not None:
                    meta = scheduler.enrich_cached(src, meta)
            except Exception:
                pass
            w = scheduler._write_fields(path, meta, src, cfg)
            db.decide(path, top["songmid"], skip=False)
            db.update_task_status(path, "manual_done",
                                  written=",".join(w))
            written += 1
        except Exception:
            failed += 1
    return {"written": written, "skipped": skipped, "failed": failed}


# 任务元数据过滤缓存：文件标签按 mtime 失效；过滤结果按条件组合缓存 60 秒
_tag_cache: dict = {}
_tag_cache_lock = threading.Lock()
_filter_cache: dict = {}   # key -> (expire_ts, matched_paths)
_filter_cache_lock = threading.Lock()
_FILTER_TTL = 600.0   # 过滤结果缓存 10 分钟（首次全量读标签慢，缓存避免重复扫描）


def _task_tags(path: str):
    """读取文件标签（带缓存：按 mtime 失效）。返回 (tags, has_cover, has_lyrics)。"""
    from musicmeta import writer as _w
    try:
        mtime = os.stat(path).st_mtime_ns
    except OSError:
        return {}, False, False
    with _tag_cache_lock:
        ent = _tag_cache.get(path)
        if ent and ent[0] == mtime:
            return ent[1]
    try:
        tags = _w.read_tags(path)
        has_cover = bool(_w.read_picture(path))
        has_lyrics = bool(_w.read_lyrics(path))
    except Exception:
        tags, has_cover, has_lyrics = {}, False, False
    with _tag_cache_lock:
        _tag_cache[path] = (mtime, (tags, has_cover, has_lyrics))
        if len(_tag_cache) > 20000:  # 上限：清掉一半
            for k in list(_tag_cache)[:10000]:
                _tag_cache.pop(k, None)
    return tags, has_cover, has_lyrics


def _task_matches(t: dict, conds: list) -> bool:
    path = t["path"]
    if not os.path.isfile(path):
        return False
    tags, has_cover, has_lyrics = _task_tags(path)
    for c in conds:
        if c == "no_lyrics" and has_lyrics: return False
        if c == "no_cover" and has_cover: return False
        if c == "no_title" and tags.get("title"): return False
        if c == "no_artist" and tags.get("artist"): return False
        if c == "no_album" and tags.get("album"): return False
        if c == "no_year" and (tags.get("date") or tags.get("year")): return False
        if c == "no_genre" and tags.get("genre"): return False
        if c == "no_track" and (tags.get("track") or tags.get("track_total")): return False
        if c == "has_lyrics" and not has_lyrics: return False
        if c == "has_cover" and not has_cover: return False
    return True


def _invalidate_filter_cache(path: str | None = None) -> None:
    """文件标签/状态变更后调用：使过滤缓存与标签缓存失效。

    path=None：清空全部过滤缓存（批量变更/删除时）。
    path=具体路径：仅剔除该文件的标签缓存，并把它从所有过滤结果缓存里移除，
    同时用最新标签重新判断它是否仍符合各缓存条件（符合则保留在结果里，
    不符合则移出——这样"修复了缺失字段后立即从过滤器消失"）。
    """
    if path:
        with _tag_cache_lock:
            _tag_cache.pop(path, None)
        with _filter_cache_lock:
            for fkey in list(_filter_cache.keys()):
                conds = fkey[2]
                exp, paths = _filter_cache[fkey]
                if path in paths:
                    # 用最新标签重新判断是否仍匹配该条件
                    still = False
                    try:
                        still = _task_matches({"path": path}, list(conds))
                    except Exception:
                        still = True
                    if still:
                        continue   # 仍匹配：保留
                    new_paths = [p for p in paths if p != path]
                    if new_paths:
                        _filter_cache[fkey] = (exp, new_paths)
                    else:
                        del _filter_cache[fkey]
    else:
        with _filter_cache_lock:
            _filter_cache.clear()


@app.get("/api/tasks")
def tasks(status: str | None = None, limit: int = 100, offset: int = 0,
          q: str = "", filter: str = ""):
    """任务列表（分页 limit+offset，可选状态过滤、关键词搜索 q、元数据过滤 filter）。

    filter 支持逗号分隔的条件（取文件实际内嵌标签判断）：
      no_lyrics / no_cover / no_title / no_artist / no_album / no_year /
      no_genre / no_track（缺失类）；has_lyrics / has_cover（存在类）。
    过滤基于真实文件标签：先全量筛选再分页，返回 filtered_total。
    """
    conds = [c.strip() for c in filter.split(",") if c.strip()]
    if conds:
        fkey = (status or "", q.strip(), tuple(sorted(conds)))
        now = time.time()
        with _filter_cache_lock:
            ent = _filter_cache.get(fkey)
            if ent and ent[0] > now:
                matched_paths = ent[1]
            else:
                if q.strip():
                    rows = db.search_tasks(q.strip(), status, limit=100000, offset=0)
                else:
                    rows = db.list_tasks(status, limit=100000, offset=0)
                matched_paths = [t["path"] for t in rows if _task_matches(t, conds)]
                _filter_cache[fkey] = (now + _FILTER_TTL, matched_paths)
                if len(_filter_cache) > 200:
                    for k in list(_filter_cache)[:100]:
                        _filter_cache.pop(k, None)
        total = len(matched_paths)
        # 按 path 恢复任务行（保持 rowid 倒序）
        path_set = set(matched_paths)
        if q.strip():
            all_rows = db.search_tasks(q.strip(), status, limit=100000, offset=0)
        else:
            all_rows = db.list_tasks(status, limit=100000, offset=0)
        page = [t for t in all_rows if t["path"] in path_set][offset:offset + limit]
        return {"total": total, "filtered_total": total, "items": page}
    if q.strip():
        rows = db.search_tasks(q.strip(), status, limit, offset)
        total = db.count_search(q.strip(), status)
    else:
        rows = db.list_tasks(status, limit, offset)
        total = db.count_tasks(status)
    return {"total": total, "filtered_total": None, "items": rows}


#: 允许人工指定的状态（与界面 6 种状态标签一致；processing 仅由调度器内部使用）
MANUAL_STATUS = ("pending", "auto_ok", "manual_done", "manual_pending",
                 "skipped", "error")


class TaskStatusBody(BaseModel):
    file: str
    status: str


@app.post("/api/task-status")
def set_task_status(body: TaskStatusBody):
    """手动指定某个文件的状态（队列里点状态标签弹出的菜单调用）。"""
    path = (body.file or "").strip()
    status = (body.status or "").strip()
    if not path:
        raise HTTPException(400, "缺少文件路径")
    if status not in MANUAL_STATUS:
        raise HTTPException(400, f"不支持的状态：{status}")
    if status == "manual_done":
        # 已人工：写入永久记忆（按内容哈希），后续刮削自动排除
        try:
            db.record_manual_done(path)
        except Exception:
            pass
    elif status in ("pending", "manual_pending", "auto_ok"):
        # 离开「已人工」时清掉永久记忆，否则下次扫描又会被排除
        try:
            db.remove_manual_done(path)
        except Exception:
            pass
    if db.set_status(path, status) == 0:
        raise HTTPException(404, "任务不存在")
    return {"ok": True, "status": status}


@app.post("/api/scrub/{file:path}")
def scrub_file(file: str):
    """手动刮削：现场按「生效数据源」重刮一遍，缓存候选并返回逐字段的当前值与候选值。

    候选数量 = 每源 source_limit 条（设置页参数），与自动刮削同一套搜索/排序规则。
    歌词不在这里返回正文（体积大），只给出候选列表，点选时再按 song_id 取。
    """
    import musicmeta.writer as _w
    if not os.path.isfile(file):
        raise HTTPException(400, f"文件不存在: {file}")
    cfg = db.get_config()
    active = active_fields(cfg)

    res = scheduler.match_file(file, cfg, force_live=True)
    metas = res.metas
    if metas:
        db.add_candidates(file, [scheduler._meta_to_dict(m) for m in metas])

    try:
        tags = _w.read_tags(file)
    except Exception:
        tags = {}
    try:
        cur_lyrics = _w.read_lyrics(file)
    except Exception:
        cur_lyrics = ""
    try:
        has_cover = bool(_w.read_picture(file))
    except Exception:
        has_cover = False

    fields: list = []
    for f in FIELDS:
        if f.key not in active or f.special == "lyrics":
            continue
        current = ""
        current_url = ""
        if f.special == "cover":
            if has_cover:
                current_url = "api/embedded-cover/" + urllib.parse.quote(file, safe="")
        else:
            current = str(tags.get(f.tag) or "")
        cands: list = []
        for m in metas:
            if f.special == "cover":
                value = (m.extra.get("cover_url") or "").strip()
                if not value and m.album_id:
                    # QQ 等源只给 album_id：按平台规则拼封面 URL（与写入下载路径一致）
                    value = ("https://y.gtimg.cn/music/photo_new/"
                             f"T002R500x500M000{m.album_id}.jpg")
            else:
                value = str(getattr(m, f.cand, "") or "").strip()
            if not value:
                continue
            cands.append({"value": value, "source": m.source, "title": m.title,
                          "artist": m.artist, "song_id": m.song_id})
        fields.append({"key": f.key, "label": f.label, "special": f.special,
                       "current": current, "current_url": current_url,
                       "candidates": cands})

    def _lrc_hint(m) -> str:
        """歌词候选的「字段内容」摘要：插件已带歌词时取首句（无需额外请求）。"""
        lrc = (m.extra.get("lyrics") or "").strip()
        if not lrc:
            return ""
        for raw in lrc.split("\n"):
            line = raw.strip()
            if not line or line.startswith("["):      # 跳过 LRC 时间标签/元信息
                continue
            return line[:40]
        return ""

    lyrics_cands = [{"value": "", "source": m.source, "title": m.title,
                     "artist": m.artist, "song_id": m.song_id,
                     "hint": _lrc_hint(m)}
                    for m in metas if m.song_id]

    return {
        "file": file, "name": os.path.basename(file),
        "audio": f"api/stream/{file}",
        "duration": scheduler._file_duration(file),
        "active_fields": active,
        "queries": res.queries,
        "verified": len(res.verified),
        "fields": fields,
        "lyrics": {"active": "lyrics" in active, "current": cur_lyrics,
                   "candidates": lyrics_cands},
    }


@app.get("/api/cand-lyrics")
def candidate_lyrics(song_id: str = "", source: str = "", title: str = "",
                     artist: str = ""):
    """取某个候选歌曲的歌词（手选歌词行时按需加载，走应用层缓存）。"""
    cfg = db.get_config()
    interval = float(cfg.get("min_interval", "0.3"))
    src_name = (source or "").strip()
    song_id = (song_id or "").strip()
    if not song_id:
        raise HTTPException(400, "缺少候选 ID")
    if src_name and src_name != "qqmusic":
        try:
            from musicmeta.sources.base import SongMeta
            from musicmeta.sources.registry import get_source
            src = get_source(src_name, min_interval=interval)
            meta = SongMeta(title=title or "", artist=artist or "",
                            song_id=song_id, source=src_name)
            meta = scheduler.enrich_cached(src, meta)
            if meta.extra.get("lyrics"):
                return {"lyrics": meta.extra["lyrics"]}
        except HTTPException:
            raise
        except Exception:
            pass
        raise HTTPException(404, "该候选没有歌词")
    src = _resolve_src("qqmusic", interval)
    if src is None:
        raise HTTPException(404, "未安装 qqmusic 插件，无法获取歌词")
    lrc = scheduler.lyrics_cached(src, song_id)
    if not lrc:
        raise HTTPException(404, "该候选没有歌词")
    return {"lyrics": lrc}


@app.get("/api/stats")
def stats():
    return db.task_stats()


@app.post("/api/run")
def run():
    ok = scheduler.start_scraper()
    return {"started": ok}


@app.post("/api/stop")
def stop():
    scheduler.stop_scraper()
    return {"ok": True}


@app.get("/api/status")
def status():
    return {"running": scheduler._scraper.running if scheduler._scraper else False,
            "error": scheduler._scraper.error if scheduler._scraper else None}


# ---------------- 人工辅助 ----------------


@app.post("/api/reprocess-pending")
def reprocess_pending():
    """重新刮削整个人工辅助队列：清空候选恢复为待处理并启动刮削。

    用于超时/网络原因之前没搜到候选的歌曲再次尝试；
    再次匹配成功且达到阈值会自动进入自动写入队列。
    只刮本队列（不会带上其它状态的任务）。
    """
    with db.connect() as c:
        rows = c.execute("SELECT path FROM tasks WHERE status='manual_pending'").fetchall()
    paths = [r["path"] for r in rows]
    n = db.requeue_status("manual_pending")
    ok = scheduler.start_scraper(paths) if paths else False
    return {"requeued": n, "started": ok}


@app.post("/api/scrape-pending")
def scrape_pending():
    """只刮削当前处于「待处理(pending)」状态的文件（不涉及其它状态）。"""
    with db.connect() as c:
        rows = c.execute("SELECT path FROM tasks WHERE status='pending'").fetchall()
    paths = [r["path"] for r in rows]
    if not paths:
        return {"started": False, "count": 0}
    ok = scheduler.start_scraper(paths)
    return {"started": ok, "count": len(paths)}


class RenameBody(BaseModel):
    file: str
    new_name: str


@app.post("/api/rename")
def rename_file(body: RenameBody):
    """重命名音乐文件（只改文件名，保持原目录；同步更新任务/候选/决策记录）。

    搜索匹配依赖文件名（歌曲名-歌手），结果不对时改文件名后重新搜索即可。
    """
    old = body.file
    if not os.path.isfile(old):
        raise HTTPException(400, f"文件不存在: {old}")
    name = os.path.basename(body.new_name).strip()
    if not name or name in (".", "..") or "/" in name or "\\" in name:
        raise HTTPException(400, "文件名不合法")
    new = os.path.join(os.path.dirname(old), name)
    if os.path.abspath(new) == os.path.abspath(old):
        return {"ok": True, "path": old}
    if os.path.exists(new):
        raise HTTPException(400, f"目标文件已存在: {new}")
    try:
        os.rename(old, new)
    except OSError as exc:
        raise HTTPException(500, f"重命名失败（目录无写权限？）: {exc}")
    scheduler._restore_owner(new)
    with db.connect() as c:
        c.execute("UPDATE tasks SET path=?, name=? WHERE path=?",
                  (new, name, old))
        c.execute("UPDATE candidates SET file_path=? WHERE file_path=?",
                  (new, old))
        c.execute("UPDATE decisions SET file_path=? WHERE file_path=?",
                  (new, old))
        c.execute("UPDATE manual_done SET file_path=? WHERE file_path=?",
                  (new, old))
    _invalidate_filter_cache()   # 路径变更：清空过滤缓存
    return {"ok": True, "path": new}


class BatchDecideBody(BaseModel):
    mode: str = "best"   # best=每个文件选用最高分候选；skip=全部跳过


@app.post("/api/decide/batch")
def decide_batch(body: BatchDecideBody):
    """批量处理整个人工队列：best=按最高分候选写入，skip=全部跳过。"""
    cfg = db.get_config()
    src = None
    if cfg.get("write_enabled") == "1" and body.mode == "best":
        src = _resolve_src("", float(cfg.get("min_interval", "0.3")))
    done = chosen = skipped = 0
    for t in db.list_tasks("manual_pending", limit=100000):
        path = t["path"]
        if body.mode == "best":
            cands = db.get_candidates(path)
            if not cands:
                continue  # 无候选的留在队列
            top = cands[0]
            if src is not None:
                from musicmeta.sources.base import SongMeta
                meta = SongMeta(
                    title=top["title"], artist=top["artist"],
                    album=top["album"], date=top["year"],
                    song_id=top["songmid"], album_id=top["albummid"])
                try:
                    scheduler._write_fields(path, meta, src, cfg)
                except Exception:
                    continue  # 写入失败的不标记，留给人工
            db.decide(path, top["songmid"], skip=False)
            chosen += 1
        else:
            db.decide(path, "", skip=True)
            skipped += 1
        done += 1
    if chosen:
        _invalidate_filter_cache()   # 批量写入多个文件：清空过滤缓存
    return {"mode": body.mode, "done": done, "chosen": chosen, "skipped": skipped}


@app.get("/api/stream/{file:path}")
def stream_audio(file: str):
    """流式播放本地音频文件（供预览：本地音频 + 候选元数据配合试听）。

    仅允许播放已授权音乐目录内的文件；支持 Range（浏览器可拖动进度）。
    """
    if not os.path.isfile(file):
        raise HTTPException(404, "文件不存在")
    root = os.path.abspath(db.get_config().get("music_dir", "") or "/")
    f = os.path.abspath(file)
    if not f.startswith(root + os.sep) and f != root:
        raise HTTPException(403, "文件不在已配置的音乐目录内")
    import mimetypes
    mt = mimetypes.guess_type(file)[0] or "application/octet-stream"
    return FileResponse(file, media_type=mt, filename=os.path.basename(file))


@app.get("/api/embedded-cover/{file:path}")
def embedded_cover(file: str):
    """返回音乐文件里内嵌的封面图（供手动刮削窗口显示「原有封面」缩略图）。

    只允许读取已配置音乐目录内的文件；封面多为几百 KB，前端用小图显示即可。
    """
    import musicmeta.writer as _w
    if not os.path.isfile(file):
        raise HTTPException(404, "文件不存在")
    root = os.path.abspath(db.get_config().get("music_dir", "") or "/")
    f = os.path.abspath(file)
    if not f.startswith(root + os.sep) and f != root:
        raise HTTPException(403, "文件不在已配置的音乐目录内")
    try:
        data = _w.read_picture(file)
    except Exception:
        data = None
    if not data:
        raise HTTPException(404, "该文件没有内嵌封面")
    if data[:4] == b"\x89PNG":
        mt = "image/png"
    elif len(data) > 12 and data[8:12] == b"WEBP":
        mt = "image/webp"
    elif data[:2] == b"\xff\xd8":
        mt = "image/jpeg"
    else:
        mt = "application/octet-stream"
    return Response(content=data, media_type=mt,
                    headers={"Cache-Control": "no-store"})


@app.get("/api/manual-done/export")
def manual_done_export():
    """导出人工处理记录（永久记忆 + 人工选择）为 JSON 文件。

    直接在浏览器/手机/iOS/APP 内点一下就能保存到当前终端的下载目录。
    """
    import musicmeta.writer  # noqa: F401  (保持与其它接口一致的导入习惯)
    data = db.export_manual_done()
    body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    name = f"music-meta-manual-{time.strftime('%Y%m%d-%H%M%S')}.json"
    return Response(
        content=body, media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{name}"',
                 "Cache-Control": "no-store"})


@app.post("/api/manual-done/import")
async def manual_done_import(file: UploadFile = File(...)):
    """导入人工处理记录文件（由导出功能生成）：按文件路径合并，不删除本地已有记录。"""
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "文件是空的")
    if len(raw) > 16 * 1024 * 1024:
        raise HTTPException(400, "文件过大（>16MB），请确认是本应用导出的记录文件")
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"不是有效的 JSON 文件：{exc}")
    try:
        stat = db.import_manual_done(data)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    print(f"[manual] 导入人工记录：{stat}")
    return stat


@app.get("/api/health")
def health():
    return {"ok": True, "app": "music-meta-web"}


# ---------------- 已人工处理永久记忆（哈希） ----------------


# ---------------- 已确认歌曲缓存导出（auto_ok 固化，下次全量扫描零网络） ----------------

import threading  # noqa: E402
_export_lock = threading.Lock()
_export_state = {"running": False, "done": 0, "total": 0, "errors": 0, "last": ""}


def _export_auto_ok_worker():
    """把 auto_ok 任务的文件标签/封面/歌词固化到 f: 缓存（已确认歌曲）。

    下次全量扫描同一首歌时 search() 直接命中 f: 缓存返回（固定高分），
    写入时封面/歌词也直接用缓存内数据，全程零网络请求。
    """
    global _export_state
    try:
        from musicmeta.sources.registry import get_source
        try:
            src = get_source("qqmusic")
        except Exception:
            src = None
        if src is None or not hasattr(src, "search"):
            _export_state.update(running=False, errors=_export_state["errors"] + 1,
                                 last="qqmusic 插件不可用（无法导出到缓存）")
            return
        from musicmeta import writer as _w
        with db.connect() as c:
            rows = c.execute(
                "SELECT path FROM tasks WHERE status='auto_ok'").fetchall()
        total = len(rows)
        _export_state.update(running=True, done=0, total=total, errors=0, last="")
        done = errs = 0
        for r in rows:
            path = r["path"]
            try:
                if not os.path.isfile(path):
                    errs += 1
                    done += 1
                    continue
                tags = _w.read_tags(path)
                title = tags.get("title") or ""
                artist = tags.get("artist") or ""
                if not title or not artist:
                    errs += 1
                    done += 1
                    continue
                lrc = _w.read_lyrics(path)
                # 从文件读到的歌词也写入 l: 缓存（按候选 songmid），
                # 下次自动写入歌词时直接命中，零请求
                # 注：封面不缓存（文件里已有；已确认歌曲写入时直接跳过下载）
                # 按文件名解析生成缓存键（与 search() 的 key 规则一致），
                # 这样下次扫描同一文件名时命中同一键
                from musicmeta.sources.base import SongMeta
                meta_dict = {
                    "title": title, "artist": artist,
                    "artists": [a.strip() for a in artist.replace("/", " / ").split("/") if a.strip()],
                    "album": tags.get("album") or "",
                    "album_artist": tags.get("album_artist") or "",
                    "date": tags.get("date") or "",
                    "genre": tags.get("genre") or "",
                    "track": tags.get("track") or "",
                    "track_total": tags.get("track_total") or "",
                    "disc": tags.get("disc") or "",
                    "publisher": tags.get("publisher") or "",
                    "language": tags.get("language") or "",
                    "duration": 0,
                    "source": "qqmusic",
                    "song_id": "", "album_id": "",
                    "extra": {
                        "confirmed": True,
                        "lyrics": lrc,
                    },
                }
                # 从候选表尽量补 song_id / album_id（歌词兜底用）
                with db.connect() as c2:
                    cand = c2.execute(
                        "SELECT songmid, albummid, duration FROM candidates "
                        "WHERE file_path=? "
                        "ORDER BY COALESCE(verified,0) DESC, score DESC LIMIT 1",
                        (path,)).fetchone()
                if cand:
                    meta_dict["song_id"] = cand["songmid"] or ""
                    meta_dict["album_id"] = cand["albummid"] or ""
                    meta_dict["duration"] = cand["duration"] or 0
                    if cand["songmid"] and lrc:
                        try:
                            # 歌词缓存写通用库（应用层统一管理）
                            from musicmeta import cache as _mc
                            _mc.set(_mc.key("qqmusic", "l", cand["songmid"]), lrc)
                        except Exception:
                            pass
                # 为每个「候选查询词」写入 f: 缓存（解析不一致也能命中）；
                # key 必须与 search_cached(src, cand.text, "") 一致，
                # 否则新链路（候选词整体搜索、artist 留空）会命中不到缓存。
                from musicmeta import cache as _mc
                from musicmeta.filenames import (build_candidates,
                                                 clean_filename,
                                                 parse_candidates)
                name = os.path.basename(path)
                keys: list = []
                for c in build_candidates(clean_filename(name)):
                    keys.append(scheduler._search_query_keys(c.text, ""))
                # 兼容旧链路的键（历史缓存/旧版写下的键）
                for pc in parse_candidates(name):
                    keys.append(scheduler._search_query_keys(pc.title, pc.artist))
                for qt, qa in keys:
                    kt = qt or ""
                    if kt:
                        _mc.set(_mc.key("qqmusic", "f", kt, qa), [meta_dict])
                done += 1
            except Exception:  # noqa: BLE001 - 单首失败不影响整体
                errs += 1
                done += 1
            if done % 100 == 0:
                _export_state.update(done=done, errors=errs)
        _export_state.update(running=False, done=done, errors=errs, last="完成")
    except Exception as exc:  # noqa: BLE001
        _export_state.update(running=False, errors=_export_state["errors"] + 1,
                             last=str(exc))


@app.post("/api/cache/export-auto-ok")
def export_auto_ok():
    """把全部自动写入（auto_ok）歌曲固化到本地 f: 缓存（后台执行）。"""
    global _export_state
    with _export_lock:
        if _export_state.get("running"):
            return {"started": False, "running": True, "total": _export_state.get("total", 0)}
        with db.connect() as c:
            n = c.execute("SELECT COUNT(*) AS n FROM tasks WHERE status='auto_ok'").fetchone()["n"]
        _export_state.update(running=False, done=0, total=n, errors=0, last="")
        t = threading.Thread(target=_export_auto_ok_worker, daemon=True)
        t.start()
        return {"started": True, "total": n}


@app.get("/api/cache/export-status")
def export_status():
    """导出进度查询。"""
    return dict(_export_state)


# ---------------- 歌曲详情 / 单项编辑 ----------------

# 前端字段名 -> SongMeta 属性 / 写入方式
_FIELD_ATTR = {
    "title": "title", "artist": "artist", "album": "album",
    "album_artist": "album_artist", "year": "date", "genre": "genre",
    "track": "track", "track_total": "track_total", "disc": "disc",
    "publisher": "publisher", "language": "language", "comment": "comment",
}

# 字段选择 -> 写入开关（人工选用时按勾选决定写哪些字段）
FIELD_SWITCH = {
    "title": "write_title", "artist": "write_artist", "year": "write_year",
    "album": "write_album", "album_artist": "write_album_artist",
    "genre": "write_genre", "track": "write_track", "disc": "write_disc",
    "company": "write_company", "language": "write_language",
    "cover": "write_cover", "lyrics": "write_lyrics",
}


class FieldWriteBody(BaseModel):
    file: str
    field: str
    value: str = ""


@app.post("/api/field-write")
def field_write(body: FieldWriteBody):
    """写入单个元数据字段（手动编辑或应用刮削候选值）。

    field: title/artist/album/album_artist/year/genre/track/track_total/disc/
           publisher/language/comment/cover/lyrics
    cover 传图片 URL（下载写入）；lyrics 传 LRC 文本；其余传文本。
    """
    path = body.file
    if not os.path.isfile(path):
        raise HTTPException(400, f"文件不存在: {path}")
    from musicmeta import writer as _w
    from musicmeta.sources.base import SongMeta
    field = body.field.strip()
    value = body.value.strip()
    cfg = db.get_config()
    # 只允许写入「生效字段」：未勾选的字段在应用里不存在，接口层一并拦住
    if field in FIELD_MAP and field not in active_fields(cfg):
        raise HTTPException(400, f"字段「{FIELD_MAP[field].label}」未生效，"
                                 f"请到设置里勾选后再写入")

    if field == "cover":
        if value:
            data = scheduler._download_cover(value)
            if not data:
                raise HTTPException(400, "封面下载失败（URL 不可访问或非图片）")
            try:
                _w.write_cover(path, data)
            except OSError as exc:
                raise HTTPException(500, scheduler._cn_err(exc))
    elif field == "lyrics":
        try:
            _w.write_lyrics(path, body.value)  # 保留换行，不用 strip
        except OSError as exc:
            raise HTTPException(500, scheduler._cn_err(exc))
    elif field in _FIELD_ATTR:
        if not value:
            raise HTTPException(400, "值不能为空")
        meta = SongMeta()
        setattr(meta, _FIELD_ATTR[field], value)
        try:
            _w.write_metadata(path, meta)
        except OSError as exc:
            raise HTTPException(500, scheduler._cn_err(exc))
    else:
        raise HTTPException(400, f"未知字段: {field}")

    scheduler._restore_owner(path)
    # 标签已变更：使该文件的过滤缓存/标签缓存失效（修复字段后从过滤器消失）
    _invalidate_filter_cache(path)
    # 手动编辑 + 写模式开启 → 永久记忆（后续刮削排除）
    if cfg.get("write_enabled") == "1":
        try:
            db.record_manual_done(path)
        except Exception:
            pass
    return {"ok": True, "field": field}
