# -*- coding: utf-8 -*-
"""通用元数据源缓存（应用级，所有数据源插件共用）。

为什么放应用层而不是插件里：
- 任何一个数据源（qqmusic / netease / kugou / 未来新开发的源）都可以直接用，
  同一首歌只请求一次服务器，降低风控与重复请求；
- 未来新增数据源无需重新实现缓存，import 本模块即可。

用法（数据源插件内）::

    from musicmeta import cache

    # 搜索缓存：同一次搜索只请求一次服务器（7 天）
    ckey = cache.key(self.name, "s", title_clean, artist_clean)
    hit = cache.get(ckey)              # 命中返回存储的原始结果列表
    if hit is None:
        hit = _do_search(...)
        cache.set(ckey, hit)
    # 重新打分（不同文件名/歌手写法打分可能不同）...

    # 专辑详情（30 天）、歌词（30 天）、封面字节（1 天）、已确认歌曲（90 天）
    cache.key(self.name, "e", album_id)
    cache.key(self.name, "l", song_id)
    cache.key(self.name, "c", albummid, "800")
    cache.key(self.name, "f", title_clean, artist_clean)

key 格式: ``<source>:<kind>:<part1>|<part2>...``，源名前缀避免跨源键冲突。

存储：SQLite 单文件（默认 ``TRIM_PKGVAR/musicmeta_cache.db``，
环境变量 MMW_CACHE_DIR 可覆盖目录；目录不可写时自动降级为纯内存）。
启动时自动把旧版 QQ 缓存库（qqmusic_cache.db）迁移到本库（加 qqmusic: 前缀）。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from typing import Dict, Optional

# 各类型有效期（秒）：kind -> ttl
_TTL: Dict[str, int] = {
    "s": 7 * 86400,    # search 搜索结果
    "e": 30 * 86400,   # enrich 专辑详情
    "l": 30 * 86400,   # lyrics 歌词
    "c": 86400,        # cover 封面字节
    "f": 90 * 86400,   # confirmed 已确认歌曲（auto_ok 导出）
}
_DEFAULT_TTL = 3600

_MAX_ROWS = 20000      # 磁盘缓存行数上限，超出清最旧

_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None
_mem: Dict[str, tuple] = {}      # key -> (expire_ts, value)
_mem_only = False                # True = 无法落盘，只用内存


def _dir() -> str:
    for env in ("MMW_CACHE_DIR", "TRIM_PKGVAR"):
        d = (os.environ.get(env) or "").strip()
        if d:
            return d
    return os.path.join(os.path.expanduser("~"), ".musicmeta_cache")


def _path() -> str:
    return os.path.join(_dir(), "musicmeta_cache.db")


def _init() -> None:
    """打开/创建数据库；失败则降级为纯内存。"""
    global _conn, _mem_only
    if _conn is not None or _mem_only:
        return
    try:
        d = _dir()
        os.makedirs(d, exist_ok=True)
        conn = sqlite3.connect(_path(), timeout=15, check_same_thread=False)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS meta_cache("
            "key TEXT PRIMARY KEY, expire REAL, val TEXT)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_meta_cache_expire ON meta_cache(expire)")
        conn.commit()
        _conn = conn
    except Exception:  # noqa: BLE001 - 目录不可写则纯内存
        _mem_only = True


def key(source: str, kind: str, *parts) -> str:
    """构造缓存键：``<source>:<kind>:<part1>|<part2>...``。

    source=数据源名（如 "qqmusic"）；kind ∈ s/e/l/c/f；
    后续 parts 按顺序用 | 拼接（只拼非空段）。
    """
    if not source or not kind:
        raise ValueError("key() 需要 source 与 kind")
    return f"{source}:{kind}:" + "|".join(p for p in parts if p)


def get(cache_key: str):
    """读缓存：命中且未过期返回原始值；未命中/过期返回 None。"""
    with _lock:
        _init()
        now = time.time()
        if _conn is not None:
            try:
                row = _conn.execute(
                    "SELECT val FROM meta_cache WHERE key=? AND expire>?",
                    (cache_key, now)).fetchone()
                if row:
                    return json.loads(row[0])
            except Exception:  # noqa: BLE001
                pass
        else:
            ent = _mem.get(cache_key)
            if ent and ent[0] > now:
                return ent[1]
    return None


def set(cache_key: str, value, ttl: Optional[int] = None) -> None:
    """写缓存。ttl 缺省按 key 中的 kind 段取默认（s/e/l/c/f）。"""
    global _mem
    with _lock:
        _init()
        if ttl is None:
            try:
                kind = cache_key.split(":", 2)[1]
                ttl = _TTL.get(kind, _DEFAULT_TTL)
            except IndexError:
                ttl = _DEFAULT_TTL
        expire = time.time() + ttl
        blob = json.dumps(value, ensure_ascii=False)
        if _conn is not None:
            try:
                _conn.execute(
                    "INSERT OR REPLACE INTO meta_cache(key, expire, val) VALUES(?,?,?)",
                    (cache_key, expire, blob))
                _conn.commit()
                # 行数超限时清掉最旧的一批（低频维护，不阻塞）
                try:
                    n = _conn.execute(
                        "SELECT COUNT(*) AS c FROM meta_cache").fetchone()[0]
                    if n > _MAX_ROWS:
                        _conn.execute(
                            "DELETE FROM meta_cache WHERE key IN ("
                            "SELECT key FROM meta_cache ORDER BY expire LIMIT ?)",
                            (n - _MAX_ROWS,))
                        _conn.commit()
                except Exception:  # noqa: BLE001
                    pass
            except Exception:  # noqa: BLE001
                pass
        else:
            _mem[cache_key] = (expire, value)
            if len(_mem) > 5000:  # 内存上限，清最旧 20%
                cutoff = sorted(_mem.values())[len(_mem) // 5][0]
                _mem = {k: v for k, v in _mem.items() if v[0] > cutoff}


def _migrate_legacy_qq() -> None:
    """把旧版 QQ 插件自带的 qqmusic_cache.db 迁移到通用库。

    旧 key 无源前缀（如 ``s:稻香|周杰伦``），迁移后统一为
    ``qqmusic:s:稻香|周杰伦``；迁移成功后将旧库改名保留（不删除）。
    幂等：仅当通用库不存在/为空且旧库存在时执行。
    """
    try:
        d = _dir()
        old = os.path.join(d, "qqmusic_cache.db")
        if not os.path.isfile(old):
            return
        _init()
        if _conn is None:
            return  # 通用库不可写，跳过迁移
        n = _conn.execute("SELECT COUNT(*) AS c FROM meta_cache").fetchone()[0]
        if n > 0:
            return  # 通用库已有数据，跳过
        oc = sqlite3.connect(old, timeout=10)
        try:
            rows = oc.execute(
                "SELECT key, expire, val FROM qq_cache WHERE expire>?",
                (time.time(),)).fetchall()
            now = time.time()
            for k, exp, val in rows:
                # 旧 key 形如 "s:xxx" → 新 key "qqmusic:s:xxx"
                newk = "qqmusic:" + k
                _conn.execute(
                    "INSERT OR IGNORE INTO meta_cache(key, expire, val) VALUES(?,?,?)",
                    (newk, max(exp, now + 60), val))
            _conn.commit()
            try:
                os.rename(old, old + ".migrated")
            except OSError:
                pass
        finally:
            oc.close()
    except Exception:  # noqa: BLE001 - 迁移失败不影响启动
        pass


def set_dir(path: str) -> None:
    """切换缓存目录（由应用配置 cache_dir 驱动）：重开连接，失败则降级为内存。

    传空串表示回到默认（TRIM_PKGVAR 应用数据目录）。
    """
    global _conn, _mem_only
    path = (path or "").strip()
    if path:
        os.environ["MMW_CACHE_DIR"] = path
    else:
        os.environ.pop("MMW_CACHE_DIR", None)
    with _lock:
        if _conn is not None:
            try:
                _conn.close()
            except Exception:  # noqa: BLE001
                pass
        _conn = None
        _mem_only = False
        _init()


def init() -> None:
    """应用启动时调用一次：初始化并迁移旧缓存库。"""
    _init()
    _migrate_legacy_qq()


def stats() -> dict:
    """缓存统计（调试用）。"""
    with _lock:
        _init()
        if _conn is not None:
            try:
                n = _conn.execute("SELECT COUNT(*) AS c FROM meta_cache").fetchone()[0]
                return {"rows": n, "db": _path()}
            except Exception:  # noqa: BLE001
                pass
        return {"rows": len(_mem), "db": "memory"}


# ---------------- SongMeta 序列化辅助（数据源通用） ----------------

def meta_to_dict(m) -> dict:
    """把 SongMeta 序列化为可 JSON 存储的 dict（缓存原始字段，命中后重新打分）。"""
    return {
        "title": m.title, "artist": m.artist, "artists": m.artists,
        "album": m.album, "date": m.date, "duration": m.duration,
        "source": m.source, "song_id": m.song_id, "album_id": m.album_id,
        "extra": m.extra, "genre": m.genre, "publisher": m.publisher,
        "language": m.language, "track": m.track, "track_total": m.track_total,
        "disc": m.disc, "album_artist": m.album_artist, "comment": m.comment,
    }


def meta_from_dict(d: dict):
    """反序列化缓存 dict → SongMeta（返回 None 表示条目损坏）。"""
    from .sources.base import SongMeta
    try:
        return SongMeta(**{k: v for k, v in d.items()})
    except Exception:  # noqa: BLE001
        return None


def search_hit(source: str, title: str, artist: str, scorer, limit: int):
    """通用 search 缓存命中逻辑：返回命中结果列表；未命中返回 None。

    source=源名；title/artist=原始搜索词；scorer(meta, title, artist)=打分函数
    （如 simple_score）；命中结果会重新打分后按置信度降序返回。
    """
    ck = key(source, "s", title.strip(), artist.strip())
    hit = get(ck)
    if hit is None:
        return None
    out = []
    for d in hit:
        m = meta_from_dict(d)
        if m is None or not m.song_id:
            continue
        try:
            m.confidence = scorer(m, title, artist)
        except Exception:
            pass
        out.append(m)
    out.sort(key=lambda x: x.confidence, reverse=True)
    return out[:limit]


def search_store(source: str, title: str, artist: str, metas) -> None:
    """通用 search 缓存写入：把搜索到的 metas 序列化存库（7 天）。"""
    if metas:
        set(key(source, "s", title.strip(), artist.strip()),
            [meta_to_dict(m) for m in metas])
