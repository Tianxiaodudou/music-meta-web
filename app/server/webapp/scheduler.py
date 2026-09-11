# -*- coding: utf-8 -*-
"""后台刮削调度器：扫描 -> 逐个匹配 -> 自动写入或进人工队列。

匹配规则（文件名模式）：
- 第 1 步 清洗文件名：去扩展名/音轨号/噪音标签（[320K]/(Hi-Res)/官方版/MV…）
- 第 2 步 生成候选查询词：整串 + 左右段（分隔符优先级）+ 括号内内容
- 第 3 步 逐个候选搜索，某个候选一旦出现「通过校验」的结果即停止后续候选（命中即停）
- 第 4 步 用搜索结果反推：结果 trackName 与 artistName 都出现在原始文件名中
  → 命中（顺序无关，天然兼容「歌手-歌名」与「歌名-歌手」两种命名）
- 命中 → 自动写入（write_enabled=1 时）；未命中 → 人工辅助队列 manual_pending
- 打分只用于候选排序，不再参与「是否命中」的判定
"""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from typing import List, Optional, Set

from musicmeta import writer
from musicmeta.filenames import (SUPPORTED_EXTS, CleanedName, build_candidates,
                                 clean_filename, verify_detail)
from musicmeta.sources.base import SongMeta, normalize_text
from musicmeta.sources.registry import get_source

from . import db


def collect_audio_files(path: str, recursive: bool) -> List[str]:
    """收集目录下的音频文件（仅文件名学习，不修改任何文件）。"""
    found: List[str] = []
    if os.path.isfile(path):
        if os.path.splitext(path)[1].lower() in SUPPORTED_EXTS:
            found.append(path)
        return found
    if not os.path.isdir(path):
        return found
    if recursive:
        for root, _dirs, files in os.walk(path):
            for f in files:
                if os.path.splitext(f)[1].lower() in SUPPORTED_EXTS:
                    found.append(os.path.join(root, f))
    else:
        for f in os.listdir(path):
            full = os.path.join(path, f)
            if os.path.isfile(full) and os.path.splitext(f)[1].lower() in SUPPORTED_EXTS:
                found.append(full)
    return sorted(found)


def _meta_to_dict(m: SongMeta) -> dict:
    return {
        "song_id": m.song_id, "title": m.title, "artist": m.artist,
        "album": m.album, "date": m.date, "album_id": m.album_id,
        "duration": m.duration, "confidence": m.confidence,
        "source": m.source,
        # 第 4 步：是否通过「文件名反推校验」（歌名+歌手都出现在文件名中）
        "verified": bool(m.extra.get("verified")),
    }


def _verified(m: SongMeta) -> bool:
    """该候选是否通过了第 4 步「文件名反推校验」。"""
    return bool(m.extra.get("verified"))


def _rank_key(m: SongMeta):
    """候选排序键：通过反推校验的优先，其次按排序分，最后按字段完整度。

    注意：这里的 confidence 只是排序分，不参与「是否命中」的判定。
    """
    filled = (1 if m.album else 0) + (1 if m.date else 0) \
        + (1 if m.duration else 0) + (1 if m.album_id else 0)
    return (1 if _verified(m) else 0, float(m.confidence or 0.0), filled)


def resolve_sources(cfg: dict) -> list:
    """按配置勾选的源建立独立实例（各自限速；插件未安装则跳过）。"""
    names = [n.strip() for n in cfg.get("source", "").split(",") if n.strip()]
    sources = []
    for n in names:
        try:
            sources.append(get_source(
                n, min_interval=float(cfg.get("min_interval", "0.3"))))
        except Exception as exc:  # noqa: BLE001
            print(f"[scheduler] 元数据源 {n} 不可用（插件未安装？）: {exc}")
    return sources


@dataclass
class MatchResult:
    """一个文件的文件名匹配结果。"""

    cleaned: CleanedName                    # 第 1 步清洗结果
    queries: List[str] = field(default_factory=list)   # 实际发出的候选词
    metas: List[SongMeta] = field(default_factory=list)   # 全部候选（已排序）
    verified: List[SongMeta] = field(default_factory=list)  # 通过反推校验的候选
    err_count: int = 0                      # 搜索异常次数（区分"搜不到"和"搜索失败"）
    # 文件名里没有任何分隔符 → 只有歌名、没有歌手信息，
    # 「歌名+歌手都出现在文件名中」这条校验天然无法满足（用于给出准确提示）
    title_only: bool = False


def match_file(path: str, cfg: dict, sources: Optional[list] = None,
               force_live: bool = False) -> MatchResult:
    """刮削核心：清洗文件名 → 候选词逐个搜索（命中即停）→ 搜索结果反推校验。

    - 不做「哪个是歌手、哪个是歌名」的方向猜测：候选词直接送去搜索，
      由搜索结果反推（verify_detail），因此不需要反序再搜一轮。
    - 命中即停：某个候选词一旦出现通过校验的结果，不再尝试后续候选词。
    - 单个候选词会查询所有选中源（便于人工队列里比较来源差异），
      但候选词层面的请求量已被早停压住。
    - 打分（_rescore）只用于候选排序。
    """
    name = os.path.basename(path)
    cleaned = clean_filename(name)
    cands = build_candidates(cleaned)
    plimit = max(1, int(cfg.get("source_limit", "10")))
    fdur = _file_duration(path)
    if sources is None:
        sources = resolve_sources(cfg)

    metas: List[SongMeta] = []
    verified: List[SongMeta] = []
    queries: List[str] = []
    seen: Set[tuple] = set()
    err_count = 0
    # 排序用的「文件名切分段」= 全部候选词（含早停后未尝试的），
    # 这样歌名/歌手与文件名各段的吻合度都能算到
    segs = [c.text for c in cands]

    for cand in cands:
        queries.append(cand.text)
        for src in sources:
            src_name = getattr(src, "name", "?")
            try:
                # 候选词整体作为搜索词（artist 留空），不做方向判断
                for meta in search_cached(src, cand.text, "", limit=plimit,
                                          force_live=force_live):
                    if not meta.song_id:
                        continue
                    key = (src_name, meta.song_id)
                    if key in seen:
                        continue
                    seen.add(key)
                    # 第 4 步：先用搜索结果反推校验，再算排序分
                    ok, why = verify_detail(cleaned.stem, meta.title, meta.artist)
                    meta.extra["verified"] = bool(ok)
                    meta.extra["verify_reason"] = why
                    meta.extra["query"] = cand.text
                    meta.confidence = _rescore(meta, segs, fdur)
                    metas.append(meta)
                    if ok:
                        verified.append(meta)
            except Exception as exc:  # noqa: BLE001
                err_count += 1
                print(f"[scheduler] 源 {src_name} 搜索「{cand.text}」异常: {exc}")
        if verified:
            break   # 命中即停：找到匹配就不再发后续候选词

    metas.sort(key=_rank_key, reverse=True)
    verified.sort(key=_rank_key, reverse=True)
    # 没有 left/right 候选 → 文件名里没有分隔符 → 没有歌手信息可校验
    title_only = not any(c.kind in ("left", "right") for c in cands)
    return MatchResult(cleaned=cleaned, queries=queries, metas=metas,
                       verified=verified, err_count=err_count,
                       title_only=title_only)


def _search_query_keys(title: str, artist: str):
    """返回与数据源插件一致的清理后 (query_title, query_artist, query_key)。"""
    import re as _re
    qt = _re.sub(r"[（(].*?[）)]", "", title or "").strip()
    qt = _re.sub(
        r"[-—–]\s*(?:铃声|伴奏|remix|mix|live|现场|演唱会|纯音乐|"
        r"翻唱|cover|instrumental|acoustic|dj|慢摇|加快|变调|串烧|"
        r"伴奏版|纯音乐版)[版曲]?$", "", qt, flags=_re.IGNORECASE).strip()
    qa = _re.sub(r"[-_]+", " ", (artist or "").strip()).strip()
    return qt or (title or "").strip(), qa


def search_cached(src, title: str, artist: str = "", limit: int = 10,
                  force_live: bool = False) -> list:
    """应用层统一搜索入口：同一首歌（同源）只请求一次服务器。

    所有数据源插件只负责"搜索 + 风控限速"，缓存统一由这里管理：
    - 已确认歌曲缓存（f:，auto_ok 导出）：命中直接返回（固定高分，
      extra["confirmed"]=True，自动写入零网络；export 端写入）。
    - 搜索缓存（s:，7 天）：命中后按当前请求重新打分。
    - 未命中 → 调用插件 search()（插件内部自带限速防风控）→ 写回缓存。
    force_live=True（重新搜索按钮）：跳过缓存强制在线，成功后仍写缓存。
    """
    from musicmeta import cache as _cache
    from musicmeta.sources.base import simple_score
    name = getattr(src, "name", "") or ""
    qt, qa = _search_query_keys(title, artist)
    key_title = qt or title.strip()
    key_artist = qa or artist.strip()

    if not force_live:
        # 1) 已确认歌曲缓存（仅 auto_ok 导出写入过才有）
        fkey = _cache.key(name, "f", key_title, key_artist)
        final = _cache.get(fkey)
        if final is not None:
            out = []
            for d in final:
                m = _cache.meta_from_dict(d)
                if m is None or not m.song_id:
                    continue
                m.extra["confirmed"] = True
                m.confidence = 95.0   # 已确认歌曲：直接给高分走自动写入
                out.append(m)
            out.sort(key=lambda x: x.confidence, reverse=True)
            return out[:limit]
        # 2) 普通搜索缓存
        hit = _cache.search_hit(name, key_title, key_artist, simple_score, limit)
        if hit is not None:
            return hit
    # 3) 在线搜索（插件内部有风控限速）→ 写回缓存
    try:
        metas = src.search(key_title if name == "qqmusic" else title,
                           key_artist if name == "qqmusic" else artist,
                           limit=limit)
    except TypeError:
        metas = src.search(title, artist, limit=limit)
    if metas:
        _cache.search_store(name, key_title, key_artist, metas)
    return metas[:limit]


def enrich_cached(src, meta: SongMeta) -> SongMeta:
    """应用层统一 enrich 入口：专辑详情补全带缓存（e: 按 album_id）。

    插件 enrich() 只负责在线拉取；命中缓存则直接应用字段不请求。
    已确认歌曲（f: 导出，extra.confirmed）字段已固化，直接返回。
    """
    from musicmeta import cache as _cache
    if meta.extra.get("confirmed"):
        return meta
    name = getattr(src, "name", "") or ""
    aid = meta.album_id or meta.song_id or ""
    if not aid:
        return meta
    ckey = _cache.key(name, "e", aid)
    cached = _cache.get(ckey)
    if cached is None:
        src.enrich(meta)          # 插件在线拉取（内部自带限速）
        # 把 enrich 得到的字段回存（仅存有值字段）
        _cache.set(ckey, {
            "date": meta.date, "genre": meta.genre,
            "album_artist": meta.album_artist, "publisher": meta.publisher,
            "language": meta.language, "track": meta.track,
            "track_total": meta.track_total, "disc": meta.disc,
            "extra": meta.extra,
        })
        return meta
    # 命中：把缓存字段应用回 meta
    if cached.get("date"): meta.date = cached["date"]
    if cached.get("genre"): meta.genre = cached["genre"]
    if cached.get("album_artist") and not meta.album_artist:
        meta.album_artist = cached["album_artist"]
    if cached.get("publisher"):
        meta.publisher = cached["publisher"]
        meta.extra["company"] = cached["publisher"]
    if cached.get("language"):
        meta.language = cached["language"]
        meta.extra["language"] = cached["language"]
    if cached.get("track_total"): meta.track_total = cached["track_total"]
    if cached.get("track"): meta.track = cached["track"]
    if cached.get("disc"): meta.disc = cached["disc"]
    for k, v in (cached.get("extra") or {}).items():
        meta.extra.setdefault(k, v)
    return meta


def lyrics_cached(src, songmid: str) -> str:
    """应用层统一歌词获取：命中 l: 缓存直接返回，否则调插件并写缓存。"""
    from musicmeta import cache as _cache
    if not songmid:
        return ""
    name = getattr(src, "name", "") or ""
    ckey = _cache.key(name, "l", songmid)
    hit = _cache.get(ckey)
    if hit is not None:
        return hit
    lrc = src.fetch_lyrics(songmid)
    if lrc:
        _cache.set(ckey, lrc)
    return lrc


def cover_cached(src, albummid: str, size: int = 800) -> Optional[bytes]:
    """应用层统一封面获取：命中 c: 缓存直接返回，否则调插件并写缓存。"""
    from musicmeta import cache as _cache
    import base64
    if not albummid:
        return None
    name = getattr(src, "name", "") or ""
    ckey = _cache.key(name, "c", albummid, str(size))
    hit = _cache.get(ckey)
    if hit is not None:
        try:
            return base64.b64decode(hit)
        except Exception:
            pass
    data = src.fetch_cover(albummid, size)
    if data:
        _cache.set(ckey, base64.b64encode(data).decode("ascii"))
    return data


def cover_best_cached(src, albummid: str,
                      sizes: tuple = (800, 500, 300)) -> Optional[bytes]:
    """按尺寸优先级取封面（应用层缓存版）：大图优先，任一尺寸成功即返回。"""
    for size in sizes:
        data = cover_cached(src, albummid, size)
        if data:
            return data
    return None


def _cn_err(exc) -> str:
    """把常见 OSError/异常转成中文提示，避免给用户显示英文堆栈。"""
    if isinstance(exc, PermissionError):
        return "没有写入权限：该文件/目录不允许应用写入（请检查目录 ACL 或共享权限，然后在文件管理器里给音乐库目录加上写权限）"
    if isinstance(exc, FileNotFoundError):
        return "文件不存在：该文件可能已被移动或删除"
    s = str(exc)
    if "[Errno 13]" in s or "Permission denied" in s:
        return "没有写入权限：该文件/目录不允许应用写入（请检查目录 ACL 或共享权限）"
    if "[Errno 2]" in s or "No such file" in s:
        return "文件不存在：该文件可能已被移动或删除"
    if "[Errno 28]" in s or "No space left" in s:
        return "磁盘空间不足，无法写入"
    if "[Errno 30]" in s or "Read-only" in s or "read-only" in s:
        return "文件系统只读，无法写入"
    return f"写入失败：{s}"


def _write_fields(path: str, meta: SongMeta, src, cfg: dict) -> list:
    """按配置写入标签字段（默认全部），返回实际写入的字段名列表。

    写入保护：候选里为空的字段绝不写入 —— 即使该字段被勾选，只要候选值
    为空（或纯空白），就跳过，文件里原本已有的数据不会被覆盖成"无"。
    """
    written: list = []
    on = lambda k: cfg.get(k, "1") == "1"
    has = lambda v: v is not None and str(v).strip() != ""
    partial = SongMeta()
    if on("write_title") and has(meta.title):
        partial.title = str(meta.title).strip()
    if on("write_artist") and has(meta.artist):
        partial.artist = str(meta.artist).strip()
    if on("write_year") and has(meta.date):
        partial.date = str(meta.date).strip()
    if on("write_album") and has(meta.album):
        partial.album = str(meta.album).strip()
    if on("write_album_artist") and has(meta.album_artist):
        partial.album_artist = str(meta.album_artist).strip()
    if on("write_genre") and has(meta.genre):
        partial.genre = str(meta.genre).strip()
    if on("write_track") and has(meta.track):
        partial.track = str(meta.track).strip()
    if has(meta.track_total):
        partial.track_total = str(meta.track_total).strip()
    if on("write_disc") and has(meta.disc):
        partial.disc = str(meta.disc).strip()
    if on("write_company") and has(meta.publisher):
        partial.publisher = str(meta.publisher).strip()
    if on("write_language") and has(meta.language):
        partial.language = str(meta.language).strip()
    if partial.title or partial.artist or partial.date or partial.album \
            or partial.album_artist or partial.genre or partial.track \
            or partial.disc or partial.publisher or partial.language:
        writer.write_metadata(path, partial)
        for k, v in (("title", partial.title), ("artist", partial.artist),
                     ("year", partial.date), ("album", partial.album),
                     ("album_artist", partial.album_artist),
                     ("genre", partial.genre), ("track", partial.track),
                     ("disc", partial.disc), ("company", partial.publisher),
                     ("language", partial.language)):
            if v:
                written.append(k)
    if on("write_cover"):
        # 已确认歌曲（f: 缓存导出）：文件里已写入过封面 → 直接跳过下载
        skip_cover = False
        if meta.extra.get("confirmed"):
            try:
                skip_cover = bool(writer.read_picture(path))
            except Exception:
                skip_cover = False
        if skip_cover:
            written.append("cover")
        else:
            # 封面：插件 extra.cover_url → 本源 fetch_cover_best → 回查 QQ
            # （封面下载走应用层缓存 cover_best_cached）
            cover = None
            if meta.extra.get("cover_url"):
                cover = _download_cover(meta.extra["cover_url"])
            if cover is None and hasattr(src, "fetch_cover_best"):
                cover = cover_best_cached(src, meta.album_id)
            if cover is None:
                _qq, _qm = _qq_fallback(src, meta)
                if _qq and _qm:
                    cover = cover_best_cached(_qq, _qm.album_id)
            if cover:
                writer.write_cover(path, cover)
                written.append("cover")
    if on("write_lyrics"):
        lrc = meta.extra.get("lyrics") or ""
        if not lrc and hasattr(src, "fetch_lyrics"):
            lrc = lyrics_cached(src, meta.song_id)
        if not lrc:
            _qq, _qm = _qq_fallback(src, meta)
            if _qq and _qm:
                lrc = lyrics_cached(_qq, _qm.song_id)
        if lrc:
            writer.write_lyrics(path, lrc)
            written.append("lyrics")
    _restore_owner(path)
    return written


def _download_cover(url: str) -> Optional[bytes]:
    """按 URL 下载封面（供插件 extra.cover_url 使用）。"""
    import urllib.request
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://y.qq.com/"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read()
        if data[:3] == b"\xff\xd8\xff" or data[:4] == b"\x89PNG":
            return data
    except Exception:
        pass
    return None


def _qq_fallback(src, meta: SongMeta):
    """跨源兜底：第三方源识别出歌曲后，回查 QQ 获取封面/歌词。

    数据源均为插件：qqmusic 插件已安装才可兜底，否则返回 (None, None)。
    返回 (QQMusicSource, QQ匹配的 SongMeta) 或 (None, None)。
    """
    if getattr(src, "name", "") == "qqmusic":
        return src, meta
    try:
        qq = get_source("qqmusic", min_interval=0.2)
    except Exception:
        return None, None
    try:
        qm = qq.lookup(meta.title, meta.artist)
        return (qq, qm) if qm is not None else (None, None)
    except Exception:
        return None, None


def _rescore(meta: SongMeta, cand_texts, file_duration: int) -> float:
    """候选**排序分**（0~100）：只决定候选列表先后顺序，不参与命中判定。

    组成（相加，封顶 100）：
    - 第 4 步「文件名反推校验」通过：            +40（最强信号）
    - 时长接近度：min/max 比值 ≥0.5 时          +40 × ratio（文件时长与候选时长都有才算）
    - 结果歌名正好等于某个候选词（文件名切分段）：+15，互为子串 +8
    - 结果歌手与某个候选词吻合：                 +5

    已确认歌曲（历史上人工/自动写过的精确匹配，extra.confirmed）直接 100 分。

    注意：源插件内部的打分（QQ 的 _score / 通用 simple_score）在新链路里是拿
    「候选词」当期望值算的，含负分惩罚项，直接用作排序会失真，因此这里统一
    用文件名 + 时长这两个客观信号重算。是否命中由 verify_detail() 决定。
    """
    if meta.extra.get("confirmed"):
        return 100.0
    score = 0.0
    if _verified(meta):
        score += 40.0
    if file_duration and meta.duration:
        ratio = min(file_duration, meta.duration) / max(file_duration, meta.duration)
        if ratio >= 0.5:
            score += 40.0 * ratio
    segs = {normalize_text(x) for x in (cand_texts or []) if x}
    gt = normalize_text(meta.title)
    if gt and segs:
        if gt in segs:
            score += 15.0
        elif any(len(gt) >= 2 and (gt in s or (len(s) >= 2 and s in gt))
                 for s in segs):
            score += 8.0
    ga = normalize_text(meta.artist)
    if ga and segs and any(ga == s or (len(ga) >= 2 and (ga in s or s in ga))
                           for s in segs):
        score += 5.0
    return round(min(100.0, score), 1)


def _file_duration(path: str) -> int:
    """读取音频文件时长（秒）；失败返回 0。"""
    try:
        from mutagen import File as MFile
        m = MFile(path)
        if m is not None and getattr(m, "info", None) is not None:
            return int(getattr(m.info, "length", 0) or 0)
    except Exception:
        pass
    return 0


def _restore_owner(path: str) -> None:
    """容器以 root 写入会使文件变为 root 所有；尽力 chown 回业务用户。

    目标 uid/gid 取环境变量 PUID/PGID（默认 1000 = NAS 用户 xiaoyu）。
    非 root 进程（如宿主机调试实例）chown 会失败，忽略即可。
    """
    try:
        uid = int(os.environ.get("PUID", "1000"))
        gid = int(os.environ.get("PGID", "1000"))
        os.chown(path, uid, gid)
    except (OSError, ValueError):
        pass


class Scraper(threading.Thread):
    """后台刮削线程池：并发 worker 各自持有独立 QQMusicSource（各自限速）。"""

    def __init__(self, scope: Optional[set] = None) -> None:
        super().__init__(daemon=True)
        self._stop_flag = threading.Event()
        self.running = False
        self.error: Optional[str] = None
        # 限定只处理这些路径（用于"只刮错误/待人工队列里的那几首"）；None=全部
        self.scope: Optional[set] = scope

    def stop(self) -> None:
        self._stop_flag.set()

    def run(self) -> None:
        self.running = True
        try:
            cfg = db.get_config()
            db.recover_stale_processing()  # 上次崩溃残留的 processing 恢复为 pending
            workers = max(1, int(cfg.get("concurrency", "1")))
            pool = [threading.Thread(target=self._worker, args=(cfg,),
                                     daemon=True) for _ in range(workers)]
            for t in pool:
                t.start()
            for t in pool:
                t.join()
        except Exception as exc:  # noqa: BLE001
            self.error = str(exc)
        finally:
            self.running = False

    def _worker(self, cfg: dict) -> None:
        pdir = cfg.get("plugins_dir", "").strip()
        if pdir:
            import os
            os.environ["MMW_PLUGINS_DIR"] = pdir
        # 多源：逗号分隔选择，每个源独立实例（各自限速）
        sources = resolve_sources(cfg)
        if not sources:
            # 未安装任何数据源插件：任务保持排队，提示用户后退出本 worker
            print("[scheduler] 未安装任何元数据源插件，任务保持排队。"
                  "请把数据源 .py 放入插件目录并重启应用后在配置页勾选源。")
            task = db.claim_next(self.scope)
            if task is not None:
                db.update_task_status(task["path"], "pending")
            return
        while not self._stop_flag.is_set():
            task = db.claim_next(self.scope)   # 原子认领，多 worker 不重复
            if task is None:
                return
            path = task["path"]
            # 已人工处理过的音乐（永久记忆哈希）→ 刮削时排除
            try:
                if db.is_manual_done(path):
                    db.update_task_status(
                        path, "skipped",
                        error="已人工处理过（永久记忆），本次刮削已排除")
                    continue
            except Exception:
                pass
            try:
                self._process(path, sources, cfg)
            except Exception as exc:  # noqa: BLE001
                db.update_task_status(path, "error", error=_cn_err(exc))

    # ---------------- 单文件处理 ----------------

    def _process(self, path: str, sources: list, cfg: dict) -> None:
        """按配置的匹配方式处理单个文件；多源候选合并，每源最多 source_limit 条。"""
        metas: List[SongMeta] = []
        err_count = 0
        res: Optional[MatchResult] = None
        mode = cfg.get("matching_mode", "filename")

        if mode == "fingerprint":
            # 指纹模式：只用音频指纹识别（需 AcoustID Key + qqmusic 插件）
            key = cfg.get("acoustid_key", "").strip()
            if not key:
                db.update_task_status(
                    path, "manual_pending",
                    error="指纹模式未配置 AcoustID Key（配置页填写）")
                return
            try:
                qq = get_source("qqmusic",
                                min_interval=float(cfg.get("min_interval", "0.3")))
            except Exception:
                db.update_task_status(
                    path, "manual_pending",
                    error="指纹识别需要 qqmusic 插件（请安装数据源 qqmusic.py 后重启）")
                return
            try:
                metas = qq.recognize_by_fingerprint(path, key)
            except Exception as exc:  # noqa: BLE001
                db.update_task_status(path, "manual_pending",
                                      error=f"指纹识别失败: {exc}")
                return
        else:
            # 文件名模式：第 1~4 步全在 match_file 里完成
            # （清洗 → 候选词 → 逐个搜索（命中即停）→ 结果反推校验）
            res = match_file(path, cfg, sources)
            metas = res.metas
            err_count = res.err_count

        if not metas:
            # 区分「搜索失败（网络/超时/源异常，重试可能成功）」与「真无匹配」
            if err_count >= max(1, len(sources) // 2):
                db.update_task_status(
                    path, "manual_pending",
                    error="搜索失败（网络超时或数据源异常），可点「重新搜索」重试")
            else:
                db.update_task_status(
                    path, "manual_pending",
                    error="无匹配，待人工辅助（可手动搜索，或用音频指纹识别）")
            return

        db.add_candidates(path, [_meta_to_dict(m) for m in metas])

        if res is not None:
            # 第 4 步判定：必须有通过「文件名反推校验」的候选才允许自动写入。
            # 未通过 → 进人工队列（候选列表仍保留，便于人工挑选）。
            if not res.verified:
                top = metas[0]
                if res.title_only:
                    msg = ("文件名里只有歌名、没有歌手，无法完成「歌名+歌手都出现在"
                           "文件名中」的反推校验；请手动搜索确认，或改用指纹模式，"
                           "待人工辅助")
                else:
                    msg = ("未通过文件名反推校验（结果歌名/歌手未同时出现在文件名中）；"
                           "可手动搜索，或用音频指纹（Chromaprint+AcoustID）识别，"
                           "待人工辅助")
                db.update_task_status(
                    path, "manual_pending", score=top.confidence,
                    title=top.title, artist=top.artist, album=top.album,
                    year=top.date, error=msg)
                return
            best = res.verified[0]
        else:
            best = max(metas, key=lambda m: m.confidence)

        written_fields: list = []
        if cfg.get("write_enabled") == "1":
            # 定位产生最佳候选的源（跨源合并时各自 enrich/写入）
            best_src = next((s for s in sources if getattr(s, "name", "") == best.source),
                            sources[0] if sources else None)
            try:
                # 写入前 enrich（应用层缓存版）：补全专辑艺人/流派/曲目号等
                if best_src is not None and hasattr(best_src, "enrich"):
                    try:
                        best = enrich_cached(best_src, best)
                    except Exception:
                        pass
                written_fields = _write_fields(path, best, best_src, cfg)
            except Exception as exc:  # noqa: BLE001
                db.update_task_status(path, "error", error=_cn_err(exc))
                return
        db.update_task_status(
            path, "auto_ok", score=best.confidence, title=best.title,
            artist=best.artist, album=best.album, year=best.date,
            written=",".join(written_fields))


# 全局唯一调度器实例
_scraper: Optional[Scraper] = None


def start_scraper(paths: Optional[list] = None) -> bool:
    """启动后台刮削；已在运行返回 False。

    paths 非空时只处理这些路径（用于"只刮错误/待人工队列里的那几首"）。
    """
    global _scraper
    if _scraper is not None and _scraper.running:
        return False
    _scraper = Scraper(set(paths) if paths else None)
    _scraper.start()
    return True


def stop_scraper() -> None:
    global _scraper
    if _scraper is not None:
        _scraper.stop()


def is_running() -> bool:
    """调度器是否正在刮削（空闲自动退出时避免在有任务时退出）。"""
    global _scraper
    return _scraper is not None and _scraper.running
