# -*- coding: utf-8 -*-
"""音乐数据刮削 Web 应用（FastAPI）。

数据源均为插件：从插件目录（安装向导配置）加载单文件 .py。
启动: uvicorn webapp.main:app --host 0.0.0.0 --port 6666
"""
from __future__ import annotations

import os
import threading
import time

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db, scheduler

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


@app.get("/api/activity")
def activity():
    """应用活动状态：最后一次请求时间（供空闲退出监控 / 调试）。"""
    return {"last_request_ts": _last_request_ts,
            "scraper_running": bool(scheduler.is_running()) if hasattr(scheduler, "is_running") else None,
            "config": db.get_config()}


@app.on_event("startup")
def _startup() -> None:
    db.init_db()
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
    threshold: float | None = None
    music_dir: str | None = None
    recursive: bool | None = None
    write_enabled: bool | None = None
    write_cover: bool | None = None
    write_artist: bool | None = None
    write_year: bool | None = None
    write_lyrics: bool | None = None
    write_title: bool | None = None
    write_album: bool | None = None
    write_album_artist: bool | None = None
    write_genre: bool | None = None
    write_track: bool | None = None
    write_disc: bool | None = None
    write_company: bool | None = None
    write_language: bool | None = None
    min_interval: float | None = None
    concurrency: int | None = None
    source_limit: int | None = None
    idle_exit_minutes: int | None = None   # 空闲自动退出分钟数；0=关闭（进程常驻）
    matching_mode: str | None = None
    acoustid_key: str | None = None
    source: str | None = None
    plugins_dir: str | None = None


@app.get("/api/config")
def get_config():
    return db.get_config()


@app.get("/api/sources")
def get_sources():
    """可用元数据源列表（内置 + 用户插件）+ 当前插件目录。"""
    cfg = db.get_config()
    pdir = cfg.get("plugins_dir", "").strip()
    if pdir:
        os.environ["MMW_PLUGINS_DIR"] = pdir
    from musicmeta.sources.registry import list_sources
    return {"sources": list_sources(), "plugins_dir": pdir}


@app.put("/api/config")
def put_config(body: ConfigBody):
    patch = {}
    for name, val in body.dict(exclude_none=True).items():
        patch[name] = str(val)
    return db.set_config(patch)


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


class RestoreBody(BaseModel):
    file: str


@app.post("/api/tasks/restore")
def restore_task(body: RestoreBody):
    """把单个已人工（manual_done）任务移回人工辅助队列。

    同时删除永久记忆（哈希）与决策记录；任务回到 manual_pending，
    可重新参与人工选择；文件本身不被修改。
    """
    path = body.file
    if not path or not path.strip():
        raise HTTPException(400, "缺少文件路径")
    n = db.restore_manual_done(path)
    if n == 0:
        # 不是 manual_done 状态：检查是否手动队列，给明确提示
        with db.connect() as c:
            row = c.execute("SELECT status FROM tasks WHERE path=?", (path,)).fetchone()
        if row:
            raise HTTPException(400, f"该任务当前状态是「{row['status']}」，不是已人工，无需恢复")
        raise HTTPException(404, "任务不存在")
    return {"ok": True, "restored": n, "status": "manual_pending"}


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
    # 数量统计缓存一并失效（下次请求重新统计）
    with _filter_counts_lock:
        _filter_counts_cache.pop("c", None)


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


# 过滤条件 → 全部统计键（前端标签）
_FILTER_COUNT_KEYS = ("no_lyrics", "no_cover", "no_title", "no_artist",
                      "no_album", "no_year", "no_genre", "no_track")
_filter_counts_cache: dict = {}   # {"ts": expire_ts, "counts": {...}}
_filter_counts_lock = threading.Lock()
_FILTER_COUNTS_TTL = 30.0         # 计数缓存 30 秒（暖标签缓存后重算也快）


def _counts_for_task_tags(tags, has_cover, has_lyrics) -> dict:
    """根据单文件标签返回该文件满足哪些 no_* 条件（全 false 表示不满足任何）。"""
    return {
        "no_lyrics": not has_lyrics,
        "no_cover": not has_cover,
        "no_title": not tags.get("title"),
        "no_artist": not tags.get("artist"),
        "no_album": not tags.get("album"),
        "no_year": not (tags.get("date") or tags.get("year")),
        "no_genre": not tags.get("genre"),
        "no_track": not (tags.get("track") or tags.get("track_total")),
    }


@app.get("/api/filter-counts")
def filter_counts():
    """一次统计所有元数据过滤条件的歌曲数量（无歌词/无封面/无标题…）。

    单遍遍历全部任务，每首歌读一次标签（走 _tag_cache 缓存，暖后秒级）；
    结果缓存 30 秒。写操作（编辑/选用/封面上传）会自动失效重建。
    """
    now = time.time()
    with _filter_counts_lock:
        ent = _filter_counts_cache.get("c")
        if ent and ent[0] > now:
            return ent[1]
    rows = db.list_tasks(limit=100000)
    counts = {k: 0 for k in _FILTER_COUNT_KEYS}
    for t in rows:
        path = t["path"]
        if not os.path.isfile(path):
            continue
        try:
            tags, has_cover, has_lyrics = _task_tags(path)
        except Exception:
            continue
        m = _counts_for_task_tags(tags, has_cover, has_lyrics)
        for k in counts:
            if m[k]:
                counts[k] += 1
    with _filter_counts_lock:
        _filter_counts_cache["c"] = (time.time() + _FILTER_COUNTS_TTL, counts)
    return counts


@app.get("/api/tasks/export")
def export_tasks():
    """导出全部任务结果为 CSV（便于离线复核人工队列）。"""
    import csv
    import io
    rows = db.list_tasks(limit=100000)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["name", "path", "status", "score01", "title", "artist",
                "album", "year", "error"])
    for t in rows:
        w.writerow([
            t["name"], t["path"], t["status"],
            round((t["score"] or 0) / 100, 3) if t["score"] is not None else "",
            t["title"], t["artist"], t["album"], t["year"], t["error"]])
    return Response(
        buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=tasks.csv"})


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

class DecideBody(BaseModel):
    file: str
    songmid: str | None = None
    skip: bool = False
    fields: list[str] | None = None   # 勾选字段；未勾选的字段不写入


@app.get("/api/pending")
def pending(limit: int = 100, offset: int = 0):
    """待人工决策队列（含候选列表）。

    候选按当前勾选的元数据源过滤显示：取消勾选的源，其候选立即从队列中隐藏
    （重新搜索/刮削后彻底清除）。
    """
    cfg = db.get_config()
    allowed = {n.strip() for n in cfg.get("source", "").split(",") if n.strip()}
    tasks = db.list_tasks("manual_pending", limit, offset)
    total = db.count_tasks("manual_pending")
    out = []
    for t in tasks:
        cands = db.get_candidates(t["path"])
        if allowed:
            cands = [c for c in cands if (c.get("source") or "") in allowed]
        else:
            cands = []
        out.append({**t, "file_duration": scheduler._file_duration(t["path"]),
                    "candidates": cands})
    return {"total": total, "items": out}


class MarkDoneBody(BaseModel):
    file: str


@app.post("/api/mark-done")
def mark_done(body: MarkDoneBody):
    """直接记忆到已人工：不选用任何候选、不写入任何字段，
    只把这首歌标记为已人工处理（manual_done）并加入永久记忆（哈希），
    之后刮削自动排除。适合「文件内嵌的元数据已经很好，无需选用候选」的情况。
    """
    path = body.file
    if not path or not path.strip():
        raise HTTPException(400, "缺少文件路径")
    cfg = db.get_config()
    db.decide(path, "", skip=False)     # 写入 decisions（songmid 为空串但非 skip）
    db.update_task_status(path, "manual_done", written="")  # 不写任何字段
    if cfg.get("write_enabled") == "1":
        try:
            db.record_manual_done(path)
        except Exception:
            pass
    return {"ok": True, "status": "manual_done"}


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


@app.post("/api/reprocess-errors")
def reprocess_errors():
    """重刮错误队列：把 error 状态恢复为待处理，并只启动刮削这几首歌。"""
    with db.connect() as c:
        rows = c.execute("SELECT path FROM tasks WHERE status='error'").fetchall()
    paths = [r["path"] for r in rows]
    if not paths:
        return {"requeued": 0, "started": False}
    n = db.requeue_status("error")
    ok = scheduler.start_scraper(paths)
    return {"requeued": n, "started": ok, "files": paths}


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


@app.post("/api/decide")
def decide(body: DecideBody):
    """人工选择某个候选写入；skip=True 表示跳过不处理。

    fields 可选：勾选的字段列表（如 ["title","artist","cover"]）；
    未勾选的字段不会写入。不传则按配置写入全部字段。
    """
    path = body.file
    if body.skip:
        # 跳过不需要文件存在：文件可能已被删除，也应能跳过
        db.decide(path, "", skip=True)
        return {"status": "skipped"}
    if not os.path.isfile(path):
        raise HTTPException(400, f"文件不存在: {path}")

    cands = db.get_candidates(path)
    chosen = next((c for c in cands if c["songmid"] == body.songmid), None)
    if chosen is None:
        raise HTTPException(400, "候选不存在（可能已过期，请重新搜索）")

    cfg = db.get_config()
    # 字段勾选：未勾选的字段对应写入开关置 0
    if body.fields is not None:
        sel = set(f.strip() for f in body.fields if f and f.strip())
        for f, sw in FIELD_SWITCH.items():
            cfg[sw] = "1" if f in sel else "0"
    written: list = []
    if cfg.get("write_enabled") == "1":
        from musicmeta.sources.base import SongMeta
        meta = SongMeta(
            title=chosen["title"], artist=chosen["artist"],
            album=chosen["album"], date=chosen["year"],
            song_id=chosen["songmid"], album_id=chosen["albummid"],
            source=chosen.get("source") or "qqmusic")
        # 按候选来源 enrich（候选源插件可用则用它；否则尝试 qqmusic 插件兜底）
        src = _resolve_src(meta.source, float(cfg.get("min_interval", "0.3")))
        try:
            if src is not None:
                meta = scheduler.enrich_cached(src, meta)  # 补全全部字段
        except Exception:
            pass
        try:
            written = scheduler._write_fields(path, meta, src, cfg)
        except OSError as exc:
            raise HTTPException(500, scheduler._cn_err(exc))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(500, f"写入失败：{exc}")
    db.decide(path, body.songmid, skip=False)
    db.update_task_status(path, "manual_done", written=",".join(written))
    # 人工处理 + 真实写入模式开启 → 永久记忆（按文件哈希），后续刮削自动排除
    if cfg.get("write_enabled") == "1" and written:
        try:
            db.record_manual_done(path)
        except Exception:
            pass
    if written:
        _invalidate_filter_cache(path)   # 选用写入字段：过滤缓存失效
    return {"status": "manual_done", "songmid": body.songmid}


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


def _search_by_words(file: str, words: list, cfg: dict, force_live: bool = False,
                     use_cache: bool = True) -> list:
    """按一组 (title, artist) 关键词跨全部选中源搜索候选并排序（共享逻辑）。

    words: [(title, artist), ...]；用于「用户手动输入歌名/歌手」的场景
    （文件名自动刮削请用 scheduler.match_file，走候选词 + 命中即停）。
    force_live: 跳过缓存强制在线。
    use_cache=False 时仍走 search_cached（其内部缓存），此参数预留。
    返回 SongMeta 列表：通过文件名反推校验的排最前，其余按排序分降序。
    """
    from musicmeta.filenames import clean_filename, verify_detail
    from musicmeta.sources.registry import get_source
    names = [n.strip() for n in cfg.get("source", "").split(",") if n.strip()]
    plimit = max(1, int(cfg.get("source_limit", "10")))
    fdur = scheduler._file_duration(file)
    stem = clean_filename(os.path.basename(file)).stem
    # 排序用的「关键词段」= 用户输入的歌名/歌手
    segs = [t for t, _a in words if t] + [a for _t, a in words if a]
    # 按 (源, song_id) 去重
    best_map: dict = {}
    for name in names:
        try:
            src = get_source(name, min_interval=float(cfg.get("min_interval", "0.3")))
            for title, artist in words:
                round_seen = set()
                for meta in scheduler.search_cached(src, title, artist,
                                                    limit=plimit,
                                                    force_live=force_live):
                    key = (name, meta.song_id)
                    if meta.song_id and key not in round_seen:
                        round_seen.add(key)
                        # 手动搜索也用同一套反推校验，仅用于排序（不拦截结果）
                        ok, why = verify_detail(stem, meta.title, meta.artist)
                        meta.extra["verified"] = bool(ok)
                        meta.extra["verify_reason"] = why
                        meta.confidence = scheduler._rescore(meta, segs, fdur)
                        prev = best_map.get(key)
                        if prev is None or scheduler._rank_key(meta) > \
                                scheduler._rank_key(prev):
                            best_map[key] = meta
        except Exception as exc:  # noqa: BLE001
            print(f"[search] 源 {name} 手动搜索「{words}」异常: {exc}")
            continue
    metas = list(best_map.values())
    metas.sort(key=scheduler._rank_key, reverse=True)
    return metas


@app.get("/api/refresh/{file:path}")
def refresh_candidates(file: str):
    """对单个文件重新搜索候选（使用全部选中源，合并显示）。

    与自动刮削完全同一套规则（scheduler.match_file）：
    清洗文件名 → 候选词逐个搜索（命中即停）→ 用搜索结果反推校验。
    出现通过校验的候选即标记 auto_ok（write_enabled=1 时同时写入文件）；
    未通过校验则留在人工队列，并把候选列表返回给人工挑选。
    """
    if not os.path.isfile(file):
        raise HTTPException(400, f"文件不存在: {file}")
    cfg = db.get_config()
    res = scheduler.match_file(file, cfg, force_live=True)
    metas = res.metas
    db.add_candidates(file, [scheduler._meta_to_dict(m) for m in metas])

    auto_written = False
    write_error = ""
    if res.verified:
        best = res.verified[0]
        written: list = []
        if cfg.get("write_enabled") == "1":
            # 与自动刮削同一套写入流程（含 enrich）
            src = None
            try:
                src = _resolve_src(best.source, float(cfg.get("min_interval", "0.3")))
            except Exception:
                src = None
            try:
                if src is not None and hasattr(src, "enrich"):
                    best = scheduler.enrich_cached(src, best)
            except Exception:
                pass
            try:
                written = scheduler._write_fields(file, best, src, cfg)
            except OSError as exc:
                # 权限等写入失败：不标 500，返回提示，任务留在人工队列
                write_error = scheduler._cn_err(exc)
            except Exception as exc:  # noqa: BLE001
                write_error = f"写入失败：{exc}"
        if not write_error:
            db.update_task_status(
                file, "auto_ok", score=best.confidence, title=best.title,
                artist=best.artist, album=best.album, year=best.date,
                written=",".join(written))
            auto_written = True
            if written:
                _invalidate_filter_cache(file)
    elif metas:
        top = metas[0]
        msg = ("文件名里只有歌名、没有歌手，无法完成「歌名+歌手都出现在文件名中」的"
               "反推校验；请手动搜索确认，或改用指纹模式，待人工辅助"
               if res.title_only else
               "未通过文件名反推校验（结果歌名/歌手未同时出现在文件名中）；"
               "可手动搜索，或用音频指纹（Chromaprint+AcoustID）识别，待人工辅助")
        db.update_task_status(
            file, "manual_pending", score=top.confidence, title=top.title,
            artist=top.artist, album=top.album, year=top.date, error=msg)
    return {"candidates": db.get_candidates(file), "auto_written": auto_written,
            "write_error": write_error, "queries": res.queries,
            "verified": len(res.verified)}


class ManualSearchBody(BaseModel):
    file: str
    title: str = ""
    artist: str = ""


@app.post("/api/manual-search")
def manual_search(body: ManualSearchBody):
    """手动搜索：文件名格式不对/识别不出时，用户输入歌名(可选歌手)跨源搜索。

    只更新候选列表并返回，不自动写入 —— 结果由用户在人工辅助队列里选用。
    """
    file = (body.file or "").strip()
    title = (body.title or "").strip()
    artist = (body.artist or "").strip()
    if not os.path.isfile(file):
        raise HTTPException(400, f"文件不存在: {file}")
    if not title and not artist:
        raise HTTPException(400, "请至少输入歌名")
    cfg = db.get_config()
    metas = _search_by_words(file, [(title, artist)], cfg, force_live=True)
    db.add_candidates(file, [scheduler._meta_to_dict(m) for m in metas])
    return {"candidates": db.get_candidates(file), "count": len(metas),
            "title": title, "artist": artist}


@app.get("/api/lyrics/{songmid}")
def get_lyrics(songmid: str, source: str = "", title: str = "", artist: str = ""):
    """获取某候选的歌词（LRC），供人工确认时查看。

    候选来自非 QQ 源（lrclib/netease/kugou 等）时，按该源补全歌词；
    QQ 源走原逻辑（songmid 直查）。
    """
    cfg = db.get_config()
    interval = float(cfg.get("min_interval", "0.3"))
    src_name = (source or "").strip()
    if src_name and src_name != "qqmusic":
        try:
            from musicmeta.sources.base import SongMeta
            from musicmeta.sources.registry import get_source
            src = get_source(src_name, min_interval=interval)
            meta = SongMeta(title=title or "", artist=artist or "",
                            song_id=songmid, source=src_name)
            meta = scheduler.enrich_cached(src, meta)
            if meta.extra.get("lyrics"):
                return {"lyrics": meta.extra["lyrics"]}
        except HTTPException:
            raise
        except Exception:
            pass
        raise HTTPException(404, "该歌曲无歌词")
    src = _resolve_src("qqmusic", interval)
    if src is None:
        raise HTTPException(404, "未安装 qqmusic 数据源插件，无法获取歌词（请安装 qqmusic.py 后重启）")
    lrc = scheduler.lyrics_cached(src, songmid)
    if not lrc:
        raise HTTPException(404, "该歌曲无歌词")
    return {"lyrics": lrc}


@app.get("/api/stream/{file:path}")
def stream_audio(file: str):
    """流式播放本地音频文件（供预览：本地音频 + 候选元数据配合试听）。

    仅允许播放已授权音乐目录内的文件；支持 Range（浏览器可拖动进度）。
    """
    from fastapi.responses import FileResponse
    if not os.path.isfile(file):
        raise HTTPException(404, "文件不存在")
    root = os.path.abspath(db.get_config().get("music_dir", "") or "/")
    f = os.path.abspath(file)
    if not f.startswith(root + os.sep) and f != root:
        raise HTTPException(403, "文件不在已配置的音乐目录内")
    import mimetypes
    mt = mimetypes.guess_type(file)[0] or "application/octet-stream"
    return FileResponse(file, media_type=mt, filename=os.path.basename(file))


@app.get("/api/recognize/{file:path}")
def recognize_candidates(file: str):
    """音频指纹识别：用音频内容识别歌曲并回查 QQ，返回候选列表。"""
    if not os.path.isfile(file):
        raise HTTPException(400, f"文件不存在: {file}")
    cfg = db.get_config()
    key = cfg.get("acoustid_key", "").strip()
    if not key:
        raise HTTPException(400, "未配置 AcoustID API key（免费注册: acoustid.org/new-application）")
    src = _resolve_src("qqmusic", float(cfg.get("min_interval", "0.3")))
    if src is None:
        raise HTTPException(400, "指纹识别需要 qqmusic 数据源插件（数据源目录提供 qqmusic.py，安装后重启）")
    metas = src.recognize_by_fingerprint(file, key)
    if not metas:
        raise HTTPException(404, "指纹识别无结果（音频可能在 MusicBrainz 库中无记录）")
    db.add_candidates(file, [scheduler._meta_to_dict(m) for m in metas])
    return db.get_candidates(file)


@app.get("/api/health")
def health():
    return {"ok": True, "app": "music-meta-web"}


# ---------------- 已人工处理永久记忆（哈希） ----------------

@app.get("/api/manual-done")
def manual_done(limit: int = 100, offset: int = 0, q: str = ""):
    """已人工处理列表（持久化，分页+搜索）。每条附带当前文件标签（供简洁展示）。"""
    from musicmeta import writer as _w
    items = db.list_manual_done(limit, offset, q.strip())
    for m in items:
        m["exists"] = os.path.isfile(m["file_path"])
        if m["exists"]:
            try:
                m["tags"] = _w.read_tags(m["file_path"])
            except Exception:
                m["tags"] = {}
        else:
            m["tags"] = {}
    return {
        "total": db.count_manual_done(q.strip()),
        "items": items,
    }


class ManualDoneBody(BaseModel):
    file: str


@app.post("/api/manual-done/remove")
def manual_done_remove(body: ManualDoneBody):
    """把某首歌从已人工处理记忆中移除（之后刮削不再排除它）。"""
    ok = db.remove_manual_done(body.file)
    return {"removed": ok}


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


def _meta_full(m) -> dict:
    """完整序列化 SongMeta（含封面 URL 与歌词，供单项编辑候选使用）。"""
    return {
        "song_id": m.song_id, "title": m.title, "artist": m.artist,
        "album": m.album, "date": m.date, "album_id": m.album_id,
        "duration": m.duration, "confidence": m.confidence, "source": m.source,
        "genre": m.genre, "publisher": m.publisher, "language": m.language,
        "album_artist": m.album_artist, "track": m.track,
        "cover_url": m.extra.get("cover_url", ""),
        "lyrics": m.extra.get("lyrics", ""),
        # 文件名反推校验结果（1=歌名+歌手都出现在文件名中）
        "verified": bool(m.extra.get("verified")),
        "verify_reason": m.extra.get("verify_reason", ""),
    }


@app.get("/api/song-detail")
def song_detail(file: str):
    """单首歌详情：当前标签、当前封面(base64)、当前歌词、文件时长、任务信息、候选。"""
    if not os.path.isfile(file):
        raise HTTPException(400, f"文件不存在: {file}")
    from musicmeta import writer as _w
    tags = _w.read_tags(file)
    pic = _w.read_picture(file)
    lrc = _w.read_lyrics(file)
    task = None
    with db.connect() as c:
        row = c.execute("SELECT * FROM tasks WHERE path=?", (file,)).fetchone()
        if row:
            task = dict(row)
    return {
        "file": file, "name": os.path.basename(file),
        "tags": tags,
        "duration": scheduler._file_duration(file),   # 文件时长（秒），供候选对比
        "cover_base64": (b"data:image/jpeg;base64," + __import__("base64").b64encode(pic)).decode()
        if pic else "",
        "lyrics": lrc,
        "task": task,
        "candidates": db.get_candidates(file),
    }


@app.get("/api/field-candidates")
def field_candidates(file: str):
    """按配置勾选的源，为这首歌搜索候选（每源前 10 条，供单项刮削选择）。"""
    if not os.path.isfile(file):
        raise HTTPException(400, f"文件不存在: {file}")
    cfg = db.get_config()
    names = [n.strip() for n in cfg.get("source", "").split(",") if n.strip()]
    plimit = max(1, int(cfg.get("source_limit", "10")))
    from musicmeta.filenames import (build_candidates, clean_filename,
                                     verify_detail)
    from musicmeta.sources.registry import get_source
    cleaned = clean_filename(os.path.basename(file))
    cands = build_candidates(cleaned)
    result: dict = {}
    for name in names:
        try:
            src = get_source(name, min_interval=float(cfg.get("min_interval", "0.3")))
        except Exception:
            continue
        metas = []
        try:
            # 与自动刮削同一套候选词顺序，本源找到「通过反推校验」的候选即停
            for cand in cands:
                hit = False
                for m in scheduler.search_cached(src, cand.text, "", limit=plimit):
                    ok, why = verify_detail(cleaned.stem, m.title, m.artist)
                    m.extra["verified"] = bool(ok)
                    m.extra["verify_reason"] = why
                    try:
                        if hasattr(src, "enrich"):
                            m = scheduler.enrich_cached(src, m)   # 补封面/歌词
                    except Exception:
                        pass
                    m.extra["verified"] = bool(ok)   # enrich 后再标一次，供前端展示
                    metas.append(_meta_full(m))
                    hit = hit or ok
                if hit:
                    break
        except Exception:
            continue
        if metas:
            result[name] = metas
    return {"sources": result, "queries": [c.text for c in cands]}


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


@app.post("/api/cover-upload")
async def cover_upload(file: str = Form(...), image: UploadFile = File(...)):
    """上传封面图片写入歌曲标签（支持 png/jpg/jpeg/webp，≤5MB）。"""
    if not os.path.isfile(file):
        raise HTTPException(400, f"文件不存在: {file}")
    data = await image.read()
    if len(data) > 5 * 1024 * 1024:
        raise HTTPException(400, "图片超过 5MB 限制")
    if data[:3] == b"\xff\xd8\xff" or data[:4] == b"\x89PNG" or \
            data[:4] == b"RIFF" or data[:8] == b"\x89WEBP" or \
            (len(data) > 12 and data[8:12] == b"WEBP"):
        from musicmeta import writer as _w
        try:
            _w.write_cover(file, data)
        except OSError as exc:
            raise HTTPException(500, scheduler._cn_err(exc))
        scheduler._restore_owner(file)
        _invalidate_filter_cache(file)   # 封面变更：过滤器（如"无封面"）立即更新
        if db.get_config().get("write_enabled") == "1":
            try:
                db.record_manual_done(file)
            except Exception:
                pass
        return {"ok": True, "bytes": len(data)}
    raise HTTPException(400, "仅支持 PNG/JPEG/WebP 图片")
