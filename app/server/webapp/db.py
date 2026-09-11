# -*- coding: utf-8 -*-
"""SQLite 数据层：配置 / 任务 / 候选 / 人工决策。"""
from __future__ import annotations

import os
import sqlite3
import time
from typing import Dict, List, Optional

DB_PATH = os.environ.get(
    "MMW_DB",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "app.db"),
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS config (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    path       TEXT PRIMARY KEY,      -- 音乐文件绝对路径
    name       TEXT NOT NULL,
    status     TEXT NOT NULL,         -- pending/processing/auto_ok/manual_pending/manual_done/skipped/error
    score      REAL,
    title      TEXT, artist TEXT, album TEXT, year TEXT,
    error      TEXT,
    written    TEXT,                  -- 已写入字段（cover,artist,year,lyrics,title）
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS candidates (
    file_path TEXT NOT NULL,
    songmid   TEXT NOT NULL,
    title     TEXT, artist TEXT, album TEXT, year TEXT,
    albummid  TEXT, duration INTEGER,
    score     REAL,
    source    TEXT,                  -- 候选来自哪个元数据源（多选合并时区分）
    verified  INTEGER,               -- 是否通过「文件名反推校验」（1=歌名+歌手都出现在文件名中）
    PRIMARY KEY (file_path, songmid)
);
CREATE TABLE IF NOT EXISTS decisions (
    file_path  TEXT PRIMARY KEY,
    songmid    TEXT,                  -- 空串表示跳过
    decided_at TEXT
);
-- 已人工处理永久记忆（刮削时排除；重新扫描时自动标记为「已人工」）
-- 以文件路径为主键（同一文件只记一行，存最新内容哈希）；哈希用于文件移动后仍可识别
-- file_size 是识别改名/搬家文件的前置筛子：大小不同就不必读内容算哈希
CREATE TABLE IF NOT EXISTS manual_done (
    file_path TEXT PRIMARY KEY,
    file_hash TEXT,
    file_size INTEGER,
    done_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_manual_done_hash ON manual_done(file_hash);
"""

DEFAULTS: Dict[str, str] = {
    "music_dir": "",                 # 学习/刮削目录（安装向导必填；需在应用设置中授权读取）
    "recursive": "1",
    "write_enabled": "0",            # 安全开关：默认不写入（学习模式）
    # ---- 风控（限速/并发/重试/批量） ----
    "min_interval": "0.3",           # 请求间隔（秒）：越小越快，越容易被风控
    "concurrency": "1",              # 并发刮削的 worker 数
    "request_timeout": "15",         # 单次网络请求超时（秒）
    "request_retries": "2",          # 单次请求失败后的重试次数
    "run_limit": "0",                # 单次「开始刮削」最多处理几首；0=不限
    "pause_every": "0",              # 每处理 N 首后暂停一次；0=不暂停
    "pause_seconds": "5",            # 上述暂停的时长（秒）
    # ---- 目录 ----
    "cache_dir": "",                 # 元数据缓存目录；空=应用数据目录
    "source_limit": "10",            # 每个源在候选列表显示的结果条数（1~20）
    # 匹配方式：二选一，不自动混用
    #   filename   = 按文件名匹配（候选关键词搜索 → 用搜索结果反推歌名/歌手）
    #   fingerprint= 按音频指纹识别（fpcalc → AcoustID → 回查 QQ，需 qqmusic 插件）
    "matching_mode": "filename",
    "acoustid_key": "",              # AcoustID API key（安装向导可配，免费注册 https://acoustid.org/new-application）
    # 用户插件目录（安装向导必填；默认应用数据目录 plugins/）
    "plugins_dir": "",
    # 元数据源（可多选，逗号分隔；候选列表合并显示各源结果，每源最多10条）
    # 本安装包不内置数据源：全部来自插件目录（官方插件见仓库 data-source-plugins/）
    "source": "",
    # 写入字段开关（默认全部写入）
    # 生效字段：应用里唯一一份字段开关（勾选的字段才会显示，也才会被写入）
    # 默认只勾选 歌名 / 歌手 / 封面图 / 歌词
    "active_fields": "title,artist,cover,lyrics",
}

#: 命中「人工处理永久记忆」时写入任务备注的固定文案（扫描 / 刮削 / 导入共用一处）
MANUAL_NOTE = "已人工处理过（永久记忆），已自动标记为「已人工」"


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    # WAL 模式：并发读写更友好，显著减少 "database is locked"
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db() -> None:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with connect() as c:
        c.executescript(SCHEMA)
        # 兼容旧库：补 written 列
        cols = [r[1] for r in c.execute("PRAGMA table_info(tasks)").fetchall()]
        if "written" not in cols:
            c.execute("ALTER TABLE tasks ADD COLUMN written TEXT")
        # 兼容旧库：补 candidates.source 列
        ccols = [r[1] for r in c.execute("PRAGMA table_info(candidates)").fetchall()]
        if "source" not in ccols:
            c.execute("ALTER TABLE candidates ADD COLUMN source TEXT")
        # 兼容旧库：补 candidates.verified 列（文件名反推校验结果）
        if "verified" not in ccols:
            c.execute("ALTER TABLE candidates ADD COLUMN verified INTEGER")
        # 兼容旧库：manual_done 补 file_size 列，并尽量从磁盘回填
        # （文件若已改名/搬家则回填不到，这类旧记录会走「未知大小」兜底逻辑）
        mcols = [r[1] for r in c.execute("PRAGMA table_info(manual_done)").fetchall()]
        if "file_size" not in mcols:
            c.execute("ALTER TABLE manual_done ADD COLUMN file_size INTEGER")
        rows = c.execute("SELECT file_path FROM manual_done "
                         "WHERE file_size IS NULL").fetchall()
        for r in rows:
            try:
                size = os.path.getsize(r["file_path"])
            except OSError:
                continue
            c.execute("UPDATE manual_done SET file_size=? WHERE file_path=?",
                      (size, r["file_path"]))
        # 清理已废弃的配置键（旧版逐个字段的 write_* 开关与 threshold，
        # 现由 active_fields 与「文件名反推校验」取代）
        c.execute("DELETE FROM config WHERE key LIKE 'write\\_%' ESCAPE '\\' "
                  "OR key IN ('threshold', 'idle_exit_minutes')")
        for key, value in DEFAULTS.items():
            c.execute("INSERT OR IGNORE INTO config(key, value) VALUES(?, ?)",
                      (key, value))


# ---------------- 配置 ----------------

def get_config() -> Dict[str, str]:
    with connect() as c:
        rows = c.execute("SELECT key, value FROM config").fetchall()
    cfg = dict(DEFAULTS)
    cfg.update({r["key"]: r["value"] for r in rows})
    return cfg


def set_config(patch: Dict[str, str]) -> Dict[str, str]:
    allowed = set(DEFAULTS)  # 所有默认配置键均可保存
    with connect() as c:
        for key, value in patch.items():
            if key in allowed:
                # 布尔值统一存 "1"/"0"（前端传 true/false，代码判断 == "1"）
                if str(value).lower() in ("true", "false"):
                    value = "1" if str(value).lower() == "true" else "0"
                c.execute(
                    "INSERT INTO config(key, value) VALUES(?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, str(value)))
    return get_config()


# ---------------- 任务 ----------------

def reset_tasks() -> None:
    with connect() as c:
        c.execute("DELETE FROM tasks")
        c.execute("DELETE FROM candidates")
        c.execute("DELETE FROM decisions")


def delete_task(path: str) -> int:
    """删除单个任务（连同该文件的候选与选择记录）。返回删除的任务行数。

    只删队列相关记录（tasks/candidates/decisions），不动磁盘文件，也**不动人工处理
    永久记忆**（manual_done）：删除只表示「移出队列」，重新扫描时仍会按永久记忆
    标回「已人工」；要让某首歌重新刮削，得先人工把它指定为「待处理」
    （那一步会清掉永久记忆）。是否删除文件本体由上层（API）决定。
    """
    with connect() as c:
        cur = c.execute("DELETE FROM tasks WHERE path=?", (path,))
        c.execute("DELETE FROM candidates WHERE file_path=?", (path,))
        c.execute("DELETE FROM decisions WHERE file_path=?", (path,))
    return cur.rowcount


def add_tasks(files: List[str], manual: Optional[set] = None) -> Dict[str, int]:
    """把扫描到的文件加入任务表。

    manual 是「命中人工处理永久记忆」的路径集合（由 memory_hits 计算）：
    这些文件直接以 manual_done 状态入列，重新扫描不会把它们打回待处理；
    已存在且处于 pending 的同类任务也一并纠正为 manual_done（例如先导入记录再扫描）。

    返回 {"added": 新增任务数, "manual_done": 其中/被纠正为已人工的条数}。
    """
    manual = manual or set()
    added = done = 0
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    with connect() as c:
        for path in files:
            is_mem = path in manual
            row = c.execute("SELECT status FROM tasks WHERE path=?", (path,)).fetchone()
            if row:
                # 只纠正 pending（唯一会重新进入刮削的状态）；
                # 其它状态是人工指定或刮削结果，保持不动
                if is_mem and row["status"] == "pending":
                    c.execute(
                        "UPDATE tasks SET status='manual_done', error=?, "
                        "updated_at=? WHERE path=?",
                        (MANUAL_NOTE, now, path))
                    done += 1
                continue
            c.execute(
                "INSERT OR IGNORE INTO tasks(path, name, status, error, updated_at) "
                "VALUES(?, ?, ?, ?, ?)",
                (path, os.path.basename(path),
                 "manual_done" if is_mem else "pending",
                 MANUAL_NOTE if is_mem else "", now))
            added += 1
            if is_mem:
                done += 1
    return {"added": added, "manual_done": done}


def claim_next(scope: Optional[set] = None) -> Optional[dict]:
    """原子认领一个 pending 任务（并发 worker 安全），并置为 processing。

    scope 非空时只认领指定路径集合内的任务（用于"只刮某些歌"）。
    """
    with connect() as c:
        c.execute("BEGIN IMMEDIATE")
        if scope:
            ph = ",".join("?" * len(scope))
            row = c.execute(
                f"SELECT path FROM tasks WHERE status='pending' "
                f"AND path IN ({ph}) ORDER BY rowid LIMIT 1",
                list(scope)).fetchone()
        else:
            row = c.execute(
                "SELECT path FROM tasks WHERE status='pending' "
                "ORDER BY rowid LIMIT 1").fetchone()
        if row is None:
            c.execute("COMMIT")
            return None
        c.execute("UPDATE tasks SET status='processing' WHERE path=?",
                  (row["path"],))
        c.execute("COMMIT")
        d = c.execute("SELECT * FROM tasks WHERE path=?",
                      (row["path"],)).fetchone()
        return dict(d) if d else None


def recover_stale_processing() -> int:
    """把卡在 processing 的任务恢复为 pending（崩溃/重启恢复）。"""
    with connect() as c:
        cur = c.execute("UPDATE tasks SET status='pending' WHERE status='processing'")
        return cur.rowcount


def update_task_status(path: str, status: str, *, score: Optional[float] = None,
                       title: str = "", artist: str = "", album: str = "",
                       year: str = "", error: str = "", written: str = "") -> None:
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    with connect() as c:
        c.execute(
            "UPDATE tasks SET status=?, score=?, title=?, artist=?, album=?, "
            "year=?, error=?, written=?, updated_at=? WHERE path=?",
            (status, score, title, artist, album, year, error, written, now, path))


def set_status(path: str, status: str, error: str = "",
               written: Optional[str] = None) -> int:
    """只改任务状态（保留匹配结果/得分/已匹配的歌名歌手等字段），返回受影响行数。

    written 为 None 时不动该列；传入时一并更新（用于「补写/批量写入」记录写了哪些字段）。
    """
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    with connect() as c:
        if written is None:
            cur = c.execute(
                "UPDATE tasks SET status=?, error=?, updated_at=? WHERE path=?",
                (status, error, now, path))
        else:
            cur = c.execute(
                "UPDATE tasks SET status=?, error=?, written=?, updated_at=? "
                "WHERE path=?", (status, error, written, now, path))
        return cur.rowcount


def task_stats() -> Dict[str, int]:
    with connect() as c:
        rows = c.execute(
            "SELECT status, COUNT(*) AS n FROM tasks GROUP BY status").fetchall()
    stats = {r["status"]: r["n"] for r in rows}
    return stats


#: 状态分组：「自动写入」这一档在界面上同时包含正在写入的 processing
_STATUS_GROUPS = {"auto_ok": ("auto_ok", "processing")}


def _status_cond(status: Optional[str]) -> tuple:
    """状态过滤 → (SQL 片段, 参数)；status 为空表示不过滤。"""
    if not status:
        return "", []
    keys = _STATUS_GROUPS.get(status, (status,))
    if len(keys) == 1:
        return "status=?", [status]
    return f"status IN ({','.join('?' * len(keys))})", list(keys)


def list_tasks(status: Optional[str] = None, limit: int = 200,
               offset: int = 0) -> List[dict]:
    """任务列表（分页：limit+offset）。"""
    q = "SELECT * FROM tasks"
    cond, params = _status_cond(status)
    if cond:
        q += " WHERE " + cond
    q += " ORDER BY rowid DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    with connect() as c:
        rows = c.execute(q, params).fetchall()
    return [dict(r) for r in rows]


def count_tasks(status: Optional[str] = None) -> int:
    q = "SELECT COUNT(*) AS n FROM tasks"
    cond, params = _status_cond(status)
    if cond:
        q += " WHERE " + cond
    with connect() as c:
        return c.execute(q, params).fetchone()["n"]


def search_tasks(q: str, status: Optional[str] = None, limit: int = 100,
                 offset: int = 0) -> List[dict]:
    """按文件名/路径模糊搜索任务（分页）。"""
    like = f"%{q}%"
    cond = "(name LIKE ? OR path LIKE ?)"
    params: list = [like, like]
    scond, sparams = _status_cond(status)
    if scond:
        cond += " AND " + scond
        params.extend(sparams)
    params.extend([limit, offset])
    with connect() as c:
        rows = c.execute(
            f"SELECT * FROM tasks WHERE {cond} ORDER BY rowid DESC "
            "LIMIT ? OFFSET ?", params).fetchall()
    return [dict(r) for r in rows]


def count_search(q: str, status: Optional[str] = None) -> int:
    like = f"%{q}%"
    cond = "(name LIKE ? OR path LIKE ?)"
    params: list = [like, like]
    scond, sparams = _status_cond(status)
    if scond:
        cond += " AND " + scond
        params.extend(sparams)
    with connect() as c:
        return c.execute(
            f"SELECT COUNT(*) AS n FROM tasks WHERE {cond}", params).fetchone()["n"]


# ---------------- 已人工处理永久记忆（路径 / 大小 + 哈希） ----------------

def manual_done_stats() -> dict:
    """人工处理记录条数（含人工选择记录）。"""
    with connect() as c:
        md = c.execute("SELECT COUNT(*) AS n FROM manual_done").fetchone()["n"]
        dec = c.execute("SELECT COUNT(*) AS n FROM decisions").fetchone()["n"]
    return {"manual_done": md, "decisions": dec}


def export_manual_done() -> dict:
    """导出人工处理记录（永久记忆 + 人工选择记录），供任意终端下载保存。"""
    with connect() as c:
        md = [dict(r) for r in c.execute(
            "SELECT file_path, file_hash, file_size, done_at FROM manual_done "
            "ORDER BY done_at")]
        dec = [dict(r) for r in c.execute(
            "SELECT file_path, songmid, decided_at FROM decisions "
            "ORDER BY decided_at")]
    return {"app": "music-meta-web", "schema": 1,
            "exported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "manual_done": md, "decisions": dec}


def import_manual_done(data: dict) -> dict:
    """导入人工处理记录：按 file_path 合并，不删除本地已有记录。

    data 可以是导出文件本身，也可以只是 manual_done 数组。
    返回 {"manual_done": {"added","updated","skipped"}, "decisions": {...}}。
    """
    if isinstance(data, list):
        data = {"manual_done": data}
    if not isinstance(data, dict):
        raise ValueError("文件内容不是有效的导出格式")
    md_rows = data.get("manual_done") or []
    dec_rows = data.get("decisions") or []
    if not isinstance(md_rows, list) or not isinstance(dec_rows, list):
        raise ValueError("manual_done / decisions 必须是数组")

    now = time.strftime("%Y-%m-%d %H:%M:%S")
    stat = {}
    with connect() as c:
        added = updated = skipped = 0
        for r in md_rows:
            if not isinstance(r, dict):
                skipped += 1
                continue
            path = str(r.get("file_path") or r.get("path") or "").strip()
            if not path:
                skipped += 1
                continue
            exists = c.execute("SELECT 1 FROM manual_done WHERE file_path=?",
                               (path,)).fetchone() is not None
            try:
                size = int(r.get("file_size") or 0)
            except (TypeError, ValueError):
                size = 0
            if not size:
                try:
                    size = os.path.getsize(path)
                except OSError:
                    size = 0
            c.execute(
                "INSERT INTO manual_done(file_path, file_hash, file_size, done_at) "
                "VALUES(?,?,?,?) "
                "ON CONFLICT(file_path) DO UPDATE SET file_hash=excluded.file_hash, "
                "file_size=excluded.file_size, done_at=excluded.done_at",
                (path, str(r.get("file_hash") or "").strip(), size,
                 str(r.get("done_at") or "").strip() or now))
            updated += 1 if exists else 0
            added += 0 if exists else 1
        stat["manual_done"] = {"added": added, "updated": updated,
                               "skipped": skipped, "total": len(md_rows)}

        added = updated = skipped = 0
        for r in dec_rows:
            if not isinstance(r, dict):
                skipped += 1
                continue
            path = str(r.get("file_path") or r.get("path") or "").strip()
            if not path:
                skipped += 1
                continue
            exists = c.execute("SELECT 1 FROM decisions WHERE file_path=?",
                               (path,)).fetchone() is not None
            c.execute(
                "INSERT INTO decisions(file_path, songmid, decided_at) VALUES(?,?,?) "
                "ON CONFLICT(file_path) DO UPDATE SET songmid=excluded.songmid, "
                "decided_at=excluded.decided_at",
                (path, str(r.get("songmid") or ""), 
                 str(r.get("decided_at") or "").strip() or now))
            updated += 1 if exists else 0
            added += 0 if exists else 1
        stat["decisions"] = {"added": added, "updated": updated,
                             "skipped": skipped, "total": len(dec_rows)}
    return stat


def file_sha256(path: str) -> str:
    """计算音乐文件内容 sha256（用于已人工处理的永久记忆与排除）。"""
    import hashlib
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            while True:
                chunk = f.read(1 << 20)
                if not chunk:
                    break
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()


def record_manual_done(path: str) -> str:
    """把人工处理过的文件永久记忆（写入模式下调用）。返回内容哈希。"""
    fh = file_sha256(path)
    if not fh:
        return ""
    try:
        size = os.path.getsize(path)
    except OSError:
        size = 0
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    with connect() as c:
        c.execute(
            "INSERT OR REPLACE INTO manual_done(file_path, file_hash, file_size, "
            "done_at) VALUES(?,?,?,?)", (path, fh, size, now))
    return fh


def remove_manual_done(path: str) -> bool:
    with connect() as c:
        cur = c.execute("DELETE FROM manual_done WHERE file_path=? OR file_hash=?",
                        (path, path))
        return cur.rowcount > 0


def memory_hits(paths) -> set:
    """批量找出哪些文件属于「人工处理永久记忆」。

    先按路径命中（绝大多数情况，零成本）；未命中的再用「大小 + 内容 sha256」
    兜底识别被改名/搬家过的文件——先用 file_size 做筛子，大小对不上就不读内容，
    因此整个音乐库扫描只是每文件一次 stat，不会把整库都哈希一遍。
    旧库中大小未知（文件早已搬走）的记录会让整表退化为逐个哈希比对（与旧行为一致）。
    """
    paths = list(paths)
    if not paths:
        return set()
    with connect() as c:
        rows = c.execute("SELECT file_path, file_hash, file_size "
                         "FROM manual_done").fetchall()
    if not rows:
        return set()
    known = {r["file_path"] for r in rows}
    hits = {p for p in paths if p in known}
    hashes = {r["file_hash"] for r in rows if r["file_hash"]}
    if not hashes:
        return hits
    sizes = {int(r["file_size"]) for r in rows if r["file_size"]}
    # 有哈希但不知道大小的记录：无法用大小筛选，只能对剩余文件逐个算哈希
    size_unknown = any(r["file_hash"] and not r["file_size"] for r in rows)
    for p in paths:
        if p in hits:
            continue
        try:
            size = os.path.getsize(p)
        except OSError:
            continue
        if not size_unknown and size not in sizes:
            continue
        if file_sha256(p) in hashes:
            hits.add(p)
    return hits


def is_manual_done(path: str) -> bool:
    """按路径或（大小 + 内容哈希）判断是否已人工处理过。"""
    return bool(memory_hits([path]))


def sync_memory_tasks() -> int:
    """把永久记忆里的 pending 任务纠正为 manual_done（导入人工记录后调用）。

    返回被纠正的任务数；不动其它状态（其它状态是人工指定或刮削结果）。
    """
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    with connect() as c:
        cur = c.execute(
            "UPDATE tasks SET status='manual_done', error=?, updated_at=? "
            "WHERE status='pending' AND path IN (SELECT file_path FROM manual_done)",
            (MANUAL_NOTE, now))
        return cur.rowcount


# ---------------- 候选 / 决策 ----------------

def add_candidates(file_path: str, metas: List[dict]) -> None:
    """写入某文件的候选列表（替换语义：先清空旧候选）。

    这样「重新搜索/重新刮削」后，候选只来自当前勾选的元数据源，
    不会残留未勾选源的旧候选。
    """
    with connect() as c:
        c.execute("DELETE FROM candidates WHERE file_path=?", (file_path,))
        for m in metas:
            c.execute(
                "INSERT OR REPLACE INTO candidates(file_path, songmid, title, "
                "artist, album, year, albummid, duration, score, source, verified) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (file_path, m.get("song_id", ""), m.get("title", ""),
                 m.get("artist", ""), m.get("album", ""), m.get("date", ""),
                 m.get("album_id", ""), m.get("duration", 0),
                 m.get("confidence", 0.0), m.get("source", ""),
                 1 if m.get("verified") else 0))


def get_candidates(file_path: str) -> List[dict]:
    """候选列表：通过「文件名反推校验」的排最前，其余按排序分降序。"""
    with connect() as c:
        rows = c.execute(
            "SELECT * FROM candidates WHERE file_path=? "
            "ORDER BY COALESCE(verified,0) DESC, score DESC", (file_path,)).fetchall()
    return [dict(r) for r in rows]


def decide(file_path: str, songmid: str, skip: bool = False) -> None:
    """留档一次「选定/跳过」，并把任务改成当前生效的状态。

    注意：「已人工」标签与永久记忆只由人工逐条操作产生（队列里指定状态、
    手动刮削窗口写字段），批量处理与补写**不产生**已人工 ——
    写入成功记为 auto_ok（自动写入），跳过记为 skipped。
    """
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    with connect() as c:
        c.execute(
            "INSERT OR REPLACE INTO decisions(file_path, songmid, decided_at) "
            "VALUES(?,?,?)",
            (file_path, "" if skip else songmid, now))
        status = "skipped" if skip else "auto_ok"
        c.execute("UPDATE tasks SET status=?, updated_at=? WHERE path=?",
                  (status, now, file_path))


def drop_decision(file_path: str) -> None:
    """清掉该文件的选择记录（人工改状态/重新排队时调用）。

    历史状态不参与后续判断，留着只会让「补写为什么跳过」这类问题无迹可循。
    """
    with connect() as c:
        c.execute("DELETE FROM decisions WHERE file_path=?", (file_path,))


def get_decision(file_path: str) -> Optional[dict]:
    with connect() as c:
        row = c.execute(
            "SELECT * FROM decisions WHERE file_path=?", (file_path,)).fetchone()
    return dict(row) if row else None


def unskip_all() -> int:
    """把 skipped 的任务恢复为 manual_pending（撤销误批量跳过）。"""
    with connect() as c:
        rows = c.execute("SELECT path FROM tasks WHERE status='skipped'").fetchall()
        for r in rows:
            c.execute("DELETE FROM decisions WHERE file_path=?", (r["path"],))
        cur = c.execute(
            "UPDATE tasks SET status='manual_pending', error='已从批量跳过中恢复' "
            "WHERE status='skipped'")
        return cur.rowcount


def retry_errors() -> int:
    """把 error 状态的任务恢复为 pending 以便重试。"""
    with connect() as c:
        cur = c.execute(
            "UPDATE tasks SET status='pending', error=NULL WHERE status='error'")
        return cur.rowcount


def requeue_status(status: str) -> int:
    """把某状态的任务恢复为 pending 并清空其候选（用于整队列重刮）。

    同时清掉这些任务的选择/跳过记录：重刮就是从当前状态重新开始，
    旧记录不该再影响后续判断。
    """
    with connect() as c:
        rows = c.execute("SELECT path FROM tasks WHERE status=?", (status,)).fetchall()
        for r in rows:
            c.execute("DELETE FROM candidates WHERE file_path=?", (r["path"],))
            c.execute("DELETE FROM decisions WHERE file_path=?", (r["path"],))
        cur = c.execute(
            "UPDATE tasks SET status='pending', error=NULL WHERE status=?", (status,))
        return cur.rowcount
