# ============================================================
# 单文件插件版（免配置数据源）
# 用法：把本文件放入应用的「插件目录」，重启应用后勾选。
# ============================================================
# -*- coding: utf-8 -*-
"""QQ 音乐元数据源（风控加固版）。

基于公开网页接口实现，零第三方依赖（仅标准库），需要能访问外网：

- 搜索接口:   https://c.y.qq.com/soso/fcgi-bin/client_search_cp
- 备用搜索:   https://c6.y.qq.com / https://i.y.qq.com（同路径，主域风控时轮换）
- 专辑详情:   https://c.y.qq.com/v8/fcg-bin/fcg_v8_album_info_cp.fcg
- 备用歌词:   https://c.y.qq.com/lyric/fcgi-bin/fcg_query_lyric_new.fcg

防风控设计：
1. **全局共享限速**：所有实例（刮削线程/接口多次创建）共用一个限速器，
   相邻网络请求最小间隔 min_interval，且间隔带随机抖动（0.5x~1.5x），
   避免固定节奏被腾讯风控识别。
2. **本地缓存（SQLite）**：同一首歌的搜索/专辑详情/歌词只请求一次服务器，
   后续直接命中缓存（默认：搜索 7 天、专辑/歌词 30 天）。
   缓存目录取 MMW_CACHE_DIR 或 TRIM_PKGVAR（应用数据目录），
   目录不可写时自动降级为纯内存缓存。
3. **多域名轮换 + 指数退避**：主域失败/风控时自动换备用域名重试，
   失败间隔逐步加大（2s/4s/8s + 抖动），避免连续打同一个接口。

用法::

    from musicmeta.sources.qqmusic import QQMusicSource

    src = QQMusicSource(min_interval=0.3)          # 批量处理时用于限速防封
    best = src.lookup("海阔天空", "Beyond")         # 返回已补全的最佳匹配
"""
from __future__ import annotations

import html
import json
import random
import re
import time
from typing import List, Optional, Set
import urllib.parse
import urllib.request
from datetime import datetime

from musicmeta.sources.base import MetaSource, SongMeta, normalize_text

# 注册为内置元数据源（插件系统统一入口）
from musicmeta.sources.registry import register_source  # noqa: E402

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
_REFERER = "https://y.qq.com/"

# 搜索接口主域 + 备用域（同一路径；主域风控/失败时轮换）
_SEARCH_HOSTS = ("https://c.y.qq.com", "https://c6.y.qq.com", "https://i.y.qq.com")
_SEARCH_PATH = "/soso/fcgi-bin/client_search_cp"
_ALBUM_PATH = "/v8/fcg-bin/fcg_v8_album_info_cp.fcg"
_MUSICU_URL = "https://u.y.qq.com/cgi-bin/musicu.fcg"
# 专辑封面图（albummid 拼 URL），尺寸 300/500/800
_COVER_URL = ("https://y.gtimg.cn/music/photo_new/T002R{size}x{size}M000{albummid}.jpg")
# 歌词接口：fcg_query_lyric_new 直接返回纯 JSON + LRC（实测最稳）
_LYRIC_NEW_URL = "https://c.y.qq.com/lyric/fcgi-bin/fcg_query_lyric_new.fcg"
# 备用歌词接口：fcg_query_lyric 返回 JSONP，歌词带 HTML 实体
_LYRIC_OLD_URL = "https://c.y.qq.com/lyric/fcgi-bin/fcg_query_lyric.fcg"

# 歌名中出现这些词说明是变体版本（翻唱/伴奏/现场/再版等），匹配时扣分
_VERSION_MARKERS: tuple = (
    "live", "现场", "演唱会", "remix", "伴奏", "纯音乐", "instrumental",
    "acoustic", "铃声", "karaoke", "翻唱", "cover", "dj", "慢摇",
    "加快", "变调", "串烧",
    # 再版 / 特殊版本
    "黑胶", "重制", "remaster", "复刻", "珍藏", "限量", "radio edit",
    "drumless", "周年", "纪念",
)
# 形如 "歌名 - 铃声" / "歌名-伴奏版" 的尾部版本后缀
_SUFFIX_RE = re.compile(
    r"[-—–]\s*(?:铃声|伴奏|remix|mix|live|现场|演唱会|纯音乐|翻唱|cover|"
    r"instrumental|acoustic|dj|慢摇|加快|变调|串烧|伴奏版|纯音乐版)[版曲]?$",
    re.IGNORECASE,
)
# 括号内容（如 "晴天 (Live)"），比对标题时先去掉
_PAREN_RE = re.compile(r"[（(].*?[）)]")
# 歌手字段中的多歌手分隔符（"_" 常见于下载站文件名，如 "周传雄_吴迪"）
_ARTIST_SEP_RE = re.compile(r"[/;、，,&_]+")


class QQMusicError(RuntimeError):
    """QQ 音乐接口请求失败。"""


# ============================================================
# 限速：统一走应用层全局共享限速器（musicmeta.ratelimit）
# —— 所有数据源插件共用同一节奏，多实例/多线程也不会并发打爆接口
# ============================================================
from musicmeta import ratelimit as _ratelimit  # noqa: E402


def _throttle(min_interval: float) -> None:
    """限速（全局共享 + 随机抖动，所有源实例共用）。"""
    _ratelimit.throttle(min_interval)


# ============================================================
def _http_get_json(url: str, params: dict, timeout: int = 15, retries: int = 2) -> dict:
    """GET 请求并解析 JSON，自动重试（指数退避 + 抖动）；兼容 JSONP 包装返回。"""
    qs = urllib.parse.urlencode(params)
    full = url + ("&" if "?" in url else "?") + qs
    last_err: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(
                full, headers={"User-Agent": _UA, "Referer": _REFERER})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", "replace").strip()
            if raw and raw[0] not in "{[":
                # 去掉 JSONP 回调包装，如 jsonCallback({...});
                start, end = raw.find("("), raw.rfind(")")
                if start >= 0 and end > start:
                    raw = raw[start + 1:end]
            return json.loads(raw)
        except Exception as exc:  # noqa: BLE001 - 网络/解析错误统一重试
            last_err = exc
            if attempt < retries:
                # 指数退避 + 抖动：0.5s / 1s / 2s ... * (0.5~1.5)
                time.sleep(0.5 * (2 ** attempt) * (0.5 + random.random()))
    raise QQMusicError(f"请求失败: {full} -> {last_err}")


def _pubtime_to_date(ts) -> str:
    """Unix 时间戳 -> "YYYY-MM-DD"；0 或缺失返回空串。"""
    try:
        ts = int(ts or 0)
    except (TypeError, ValueError):
        return ""
    if ts <= 0:
        return ""
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
    except (OverflowError, OSError, ValueError):
        return ""


def clean_songname(songname: str) -> str:
    """去掉 QQ 在歌名后附加的版本后缀，如 '屋顶 - 铃声' -> '屋顶'。"""
    s = (songname or "").strip()
    cleaned = _SUFFIX_RE.sub("", s).strip()
    return cleaned or s


def _clean_query_title(title: str) -> str:
    """搜索用的标题：去掉括号内容与版本后缀，提高 QQ 搜索命中率。

    例: '蓝莲花(Live)' -> '蓝莲花'；'江南(DJ白鹤版)' -> '江南'。
    匹配打分仍会优先非变体版本，这里只影响搜索词。
    """
    t = _PAREN_RE.sub("", title or "").strip()
    t = _SUFFIX_RE.sub("", t).strip()
    return t or (title or "").strip()


class QQMusicSource(MetaSource):
    """QQ 音乐元数据源（风控加固版）。"""

    name = "qqmusic"

    def __init__(self, min_interval: float = 0.3, timeout: int = 15, retries: int = 2,
                 min_confidence: float = 40.0):
        """
        min_interval:   相邻两次网络请求的最小间隔（秒）。批量处理数千首歌时
                        用于限速，避免触发风控；带随机抖动。
        min_confidence: lookup() 接受的最低置信度。低于该值视为无匹配，
                        防止搜索词被模糊匹配到无关歌曲时误写元数据。
        """
        self.min_interval = min_interval
        self.timeout = timeout
        self.retries = retries
        self.min_confidence = min_confidence

    # ---------------- 内部工具 ----------------

    def _throttle(self) -> None:
        """限速（委托模块级全局限速器，多实例共享 + 随机抖动）。"""
        _throttle(self.min_interval)

    def _parse_item(self, item: dict, want_title: str, want_artist: str) -> Optional[SongMeta]:
        """把搜索接口的单条记录转换为 SongMeta 并打分。"""
        if item.get("type") not in (0, None):
            return None  # 只取普通歌曲，跳过 MV/视频等
        raw_name = item.get("songname") or item.get("name") or ""
        if not raw_name:
            return None

        artists: List[str] = []
        for singer in item.get("singer") or []:
            name = (singer.get("name") or "").strip()
            for part in _ARTIST_SEP_RE.split(name):
                part = part.strip()
                if part and part not in artists:
                    artists.append(part)
        if not artists:
            return None

        meta = SongMeta(
            title=clean_songname(raw_name),
            artist=" / ".join(artists),
            artists=artists,
            album=item.get("albumname") or "",
            date=_pubtime_to_date(item.get("pubtime")),
            duration=int(item.get("interval") or 0),
            source=self.name,
            song_id=str(item.get("songmid") or item.get("songid") or ""),
            album_id=str(item.get("albummid") or ""),
            extra={
                "songname_raw": raw_name,
                "size_flac": int(item.get("sizeflac") or 0),
                "size_320": int(item.get("size320") or 0),
                "pubtime": item.get("pubtime"),
            },
        )
        meta.confidence = self._score(meta, want_title, want_artist, item)
        return meta

    def _score(self, meta: SongMeta, want_title: str, want_artist: str,
               item: dict) -> float:
        """对候选打分。正分越高越可信；<=0 视为不匹配。"""
        score = 0.0
        want_t = normalize_text(want_title)
        base = _PAREN_RE.sub("", meta.extra.get("songname_raw", meta.title)).strip()
        got_t = normalize_text(base)

        # ---- 标题 ----
        if want_t:
            if got_t == want_t:
                score += 60
            elif (len(got_t) >= 4 and len(want_t) >= 4
                  and (got_t in want_t or want_t in got_t)):
                score += 30  # 互相包含（双方都足够长，避免短串误匹配）
            else:
                score -= 40  # 标题对不上，基本淘汰
        else:
            score += 30

        # ---- 歌手（支持多歌手；集合/拼接/子串三种比较，兼容
        #      "刘欢-Sarah Brightman"、"周传雄_吴迪"、"CoCo李玟" vs "李玟"）----
        want_names: Set[str] = {
            normalize_text(x) for x in _ARTIST_SEP_RE.split(want_artist or "")
            if x.strip()
        }
        got_names: Set[str] = {normalize_text(a) for a in meta.artists}
        want_joined = normalize_text(want_artist)
        got_joined = "".join(normalize_text(a) for a in meta.artists)
        if want_names:
            if (want_names == got_names
                    or (want_joined and want_joined == got_joined)):
                score += 30
            elif want_names <= got_names or got_names <= want_names:
                score += 18
            elif (min(len(want_joined), len(got_joined)) >= 2
                  and (want_joined in got_joined or got_joined in want_joined)):
                score += 18
            else:
                score -= 25
        else:
            score += 10

        # ---- 版本标记扣分 ----
        low = meta.extra.get("songname_raw", meta.title).lower()
        flags = 0
        if _SUFFIX_RE.search(meta.extra.get("songname_raw", meta.title)):
            flags += 1
        flags += sum(1 for m in _VERSION_MARKERS if m in low)
        score -= 15 * min(flags, 3)
        # 无发行时间的很可能是翻录/变体
        if not item.get("pubtime"):
            score -= 10
        return round(score, 2)

    # ---------------- MetaSource 接口 ----------------

    def search(self, title: str, artist: str = "", limit: int = 10) -> List[SongMeta]:
        """搜索候选（纯在线；缓存由应用层统一管理）。"""
        if not title:
            return []
        # 搜索词规范化：标题去括号/版本后缀；歌手名里的 -/_ 换成空格（如
        # "刘欢-Sarah Brightman"、"周传雄_吴迪"），提高 QQ 搜索命中率
        query_title = _clean_query_title(title)
        query_artist = re.sub(r"[-_]+", " ", (artist or "").strip()).strip()
        query = query_title if not query_artist else f"{query_title} {query_artist}"
        # ---- 主接口（多域名轮换，风控/失败自动换备用域）----
        data = None
        last_err: Optional[Exception] = None
        for host in _SEARCH_HOSTS:
            self._throttle()
            try:
                data = _http_get_json(host + _SEARCH_PATH, {
                    # 2026-09 起腾讯对匿名搜索收紧：需带完整 web 播放器参数，
                    # 否则返回 subcode=-10003（query error）空结果
                    "ct": "24", "qqmusic_uin": "0", "format": "json",
                    "inCharset": "utf8", "outCharset": "utf-8", "notice": "0",
                    "platform": "yqq.json", "needNewCode": "0",
                    "p": 1, "n": max(limit, 10), "w": query,
                }, timeout=self.timeout, retries=self.retries)
                if data and data.get("code") == 0:
                    break  # 成功
                # code!=0：可能是风控（subcode=-10003），休息久一点换域名
                last_err = QQMusicError(
                    f"code={data.get('code')} subcode={((data.get('data') or {}).get('subcode'))}")
                time.sleep(1.5 * (0.5 + random.random()))
            except QQMusicError as exc:
                last_err = exc
                time.sleep(1.5 * (0.5 + random.random()))
        if data is None or data.get("code") != 0:
            # 主接口全部失败 → 备用 musicu 接口兜底
            try:
                data = self._search_musicu(query)
            except Exception:  # noqa: BLE001
                if last_err is not None:
                    print(f"[qqmusic] 搜索全部失败: {last_err}")
                return []

        items = ((data.get("data") or {}).get("song") or {}).get("list") or []
        results: List[SongMeta] = []
        seen: Set[str] = set()
        for item in items:
            # 打分用清理后的标题比较（与搜索词一致），否则文件名里的
            # "(Live)" 等会让已找到的录音室版被误判为标题失配
            meta = self._parse_item(item, query_title, artist)
            if meta and meta.song_id and meta.song_id not in seen:
                seen.add(meta.song_id)
                results.append(meta)
        results.sort(key=lambda m: m.confidence, reverse=True)
        return results[:limit]

    def enrich(self, meta: SongMeta) -> SongMeta:
        """用专辑详情补全：发行日期/流派/唱片公司/语言/曲目号/碟号（纯在线）。

        缓存由应用层统一管理（musicmeta.cache / scheduler.enrich_cached），
        插件只负责从 QQ 专辑接口拉取并应用字段。
        """
        if not meta.album_id:
            return meta
        self._throttle()
        data = None
        for host in _SEARCH_HOSTS:
            try:
                data = _http_get_json(host + _ALBUM_PATH, {
                    "albummid": meta.album_id, "format": "json",
                }, timeout=self.timeout, retries=self.retries)
                if data and data.get("code") == 0:
                    break
                data = None
                time.sleep(1.5 * (0.5 + random.random()))
            except QQMusicError:
                time.sleep(1.5 * (0.5 + random.random()))
        if not data or data.get("code") != 0:
            return meta
        info = data.get("data") or {}
        if info.get("aDate"):
            meta.date = str(info["aDate"])
        if info.get("genre"):
            meta.genre = str(info["genre"])
        if info.get("singername") and not meta.album_artist:
            meta.album_artist = str(info["singername"])
        if info.get("company"):
            meta.publisher = str(info["company"])
            meta.extra["company"] = str(info["company"])
        if info.get("lan"):
            meta.language = str(info["lan"])
            meta.extra["language"] = str(info["lan"])
        songlist = info.get("list") or []
        if songlist:
            meta.track_total = str(len(songlist))
            for i, item in enumerate(songlist, 1):
                if str(item.get("songmid") or "") == meta.song_id:
                    meta.track = str(i)
                    cd = item.get("cdIdx")
                    if isinstance(cd, int) and cd >= 0:
                        meta.disc = str(cd + 1)
                    break
        return meta

    # ---------------- 封面图 / 歌词（刮削 Web 应用用） ----------------

    @staticmethod
    def cover_url(albummid: str, size: int = 300) -> str:
        """按 albummid 拼 QQ 音乐封面图 URL（size: 300/500/800）。"""
        return _COVER_URL.format(size=size, albummid=albummid)

    def fetch_cover(self, albummid: str, size: int = 300) -> Optional[bytes]:
        """下载专辑封面图，返回 JPEG 字节；失败返回 None（纯在线，缓存应用层管）。"""
        if not albummid:
            return None
        url = self.cover_url(albummid, size)
        self._throttle()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _UA,
                                                       "Referer": _REFERER})
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = resp.read()
            # 校验确是图片（JPEG/PNG 魔数）
            if data[:3] == b"\xff\xd8\xff" or data[:4] == b"\x89PNG":
                return data
            return None
        except Exception:
            return None

    def fetch_cover_best(self, albummid: str,
                         sizes: tuple = (800, 500, 300)) -> Optional[bytes]:
        """按尺寸优先级获取封面：大图优先，任一尺寸成功即返回。

        用户约定：封面图多大都可以用，但大的优先级高、小的优先级低。
        """
        for size in sizes:
            data = self.fetch_cover(albummid, size)
            if data:
                return data
        return None

    def fetch_lyrics(self, songmid: str) -> str:
        """获取 LRC 歌词文本；无歌词或失败返回空串（纯在线，缓存应用层管）。"""
        if not songmid:
            return ""
        self._throttle()
        # 主接口：fcg_query_lyric_new（纯 JSON）
        try:
            data = _http_get_json(_LYRIC_NEW_URL, {
                "songmid": songmid, "format": "json", "nobase64": 1,
            }, timeout=self.timeout, retries=1)
            lyric = (data or {}).get("lyric")
            if lyric:
                return str(lyric)
        except QQMusicError:
            pass
        # 备用：fcg_query_lyric（JSONP + HTML 实体）
        try:
            raw = self._http_get_raw(_LYRIC_OLD_URL, {
                "songmid": songmid, "format": "json", "nobase64": 1,
            })
            start, end = raw.find("("), raw.rfind(")")
            if start >= 0 and end > start:
                data = json.loads(raw[start + 1:end])
                lyric = data.get("lyric")
                if lyric:
                    return html.unescape(str(lyric))
        except Exception:
            pass
        return ""

    def _http_get_raw(self, url: str, params: dict) -> str:
        """GET 返回原始文本（带重试），供 JSONP 类接口使用。"""
        qs = urllib.parse.urlencode(params)
        full = url + ("&" if "?" in url else "?") + qs
        last_err: Optional[Exception] = None
        for attempt in range(self.retries + 1):
            try:
                req = urllib.request.Request(
                    full, headers={"User-Agent": _UA, "Referer": _REFERER})
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return resp.read().decode("utf-8", "replace")
            except Exception as exc:
                last_err = exc
                if attempt < self.retries:
                    time.sleep(0.5 * (2 ** attempt) * (0.5 + random.random()))
        raise QQMusicError(f"请求失败: {full} -> {last_err}")

    def recognize_by_fingerprint(self, path: str, acoustid_key: str,
                                 limit: int = 10) -> List[SongMeta]:
        """音频指纹识别歌曲后回查 QQ 音乐，返回候选列表。

        链路：fpcalc 计算 Chromaprint 指纹 → AcoustID 识别歌名/歌手
        → 用识别结果搜索 QQ 音乐 → 返回带置信度的候选（可继续走自动/人工流程）。
        """
        from musicmeta.fingerprint import recognize
        recs = recognize(path, acoustid_key)
        if not recs:
            return []
        results: List[SongMeta] = []
        seen: Set[str] = set()
        for rec in recs[:3]:  # 尝试前几个识别结果
            for meta in self.search(rec["title"], rec["artist"], limit=limit):
                if meta.song_id and meta.song_id not in seen:
                    seen.add(meta.song_id)
                    meta.extra["fingerprint_title"] = rec["title"]
                    meta.extra["fingerprint_artist"] = rec["artist"]
                    results.append(meta)
        results.sort(key=lambda m: m.confidence, reverse=True)
        return results

    # ---------------- 备用接口 ----------------

    def _search_musicu(self, query: str) -> dict:
        """备用搜索接口（实验性）。实测当前网络返回 0 命中，仅作兜底。"""
        payload = {
            "comm": {"ct": 24, "cv": 0},
            "req_1": {
                "module": "music.search.SearchCgiService",
                "method": "DoSearchForQQMusicDesktop",
                "param": {"grp": 1, "num_per_page": 20, "page_num": 1,
                          "query": query, "search_type": 0},
            },
        }
        self._throttle()
        req = urllib.request.Request(
            _MUSICU_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "User-Agent": _UA, "Referer": _REFERER},
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
        data = json.loads(raw)
        req1 = data.get("req_1") or {}
        song = ((req1.get("data") or {}).get("body") or {}).get("song") or {}
        # 统一成与 client_search_cp 相同的结构
        return {"code": 0, "data": {"song": {"list": song.get("list") or []}}}


# 注册为内置元数据源（插件系统统一入口）
register_source("qqmusic", QQMusicSource)
