# ============================================================
# 单文件插件版（免配置数据源）
# 用法：把本文件放入应用的「插件目录」（安装时配置，默认
# 应用数据目录 plugins/），重启应用后在配置页勾选该源。
# 依赖应用内置的 musicmeta 框架（base/registry/fingerprint）。
# ============================================================
# -*- coding: utf-8 -*-
"""酷狗音乐元数据源（内置）。

基于公开搜索接口（无需登录）：
- 搜索:   GET https://songsearch.kugou.com/song_search_v2?keyword=歌名 歌手
- 封面:   https://imgessl.kugou.com/stdmusic/400/{FileHash}.jpg（按 hash 拼，无需请求）
- 歌词:   http://lyrics.kugou.com/search + /download（LRC，base64）

接口为公开/非官方，若失效请更新本文件（可整体替换为插件）。
"""
from __future__ import annotations

import base64
import json
import time
import urllib.parse
import urllib.request
from typing import List

from musicmeta.sources.base import MetaSource, SongMeta, simple_score
from musicmeta.sources.registry import register_source
from musicmeta import ratelimit as _ratelimit

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


class KugouSource(MetaSource):
    name = "kugou"
    SEARCH_URL = "https://songsearch.kugou.com/song_search_v2"
    LYRIC_SEARCH_URL = "http://lyrics.kugou.com/search"
    LYRIC_DOWN_URL = "http://lyrics.kugou.com/download"

    def __init__(self, min_interval: float = 0.3, **kwargs):
        self.min_interval = min_interval

    def _throttle(self):
        """限速（应用层全局共享 + 随机抖动，所有源实例共用）。"""
        _ratelimit.throttle(getattr(self, "min_interval", 0.3))
    def _get_json(self, url: str) -> dict:
        self._throttle()
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))

    def search(self, title: str, artist: str = "", limit: int = 10) -> List[SongMeta]:
        query = f"{title} {artist}".strip()
        url = self.SEARCH_URL + "?" + urllib.parse.urlencode(
            {"keyword": query, "page": 1, "pagesize": max(limit, 5)})
        d = self._get_json(url)
        # 酷狗歌名常带版本后缀/合成歌手名，按原唱优先过滤排序
        import re as _re
        _FAKE_SINGERS = ("歌单", "热门", "DJ", "网友", "翻唱", "伴奏", "抖音", "快手")
        def _is_fake_singer(sg):
            low = sg.lower()
            return any(x.lower() in low for x in _FAKE_SINGERS) or sg == "群星"
        metas: List[SongMeta] = []
        for s in (d.get("data") or {}).get("lists", []) or []:
            name = (s.get("SongName") or "").strip()
            singer = (s.get("SingerName") or "").strip()
            if not name or not singer or _is_fake_singer(singer):
                continue
            # 去掉 "歌手《歌名》" 的包装格式（酷狗歌单条目常见）
            m2 = _re.search(r"[《《（(]([^》》）)]+)[》》）)]", name)
            low_name = name.lower()
            # 版本标记：DJ/改编/伴奏等 → 扣分（原唱优先，但保留作后备）
            version_penalty = 0
            if any(v in low_name for v in ("dj", "live", "现场", "remix", "伴奏",
                                           "改编", "mv", "mv版", "慢摇", "串烧",
                                           "加快", "变调")):
                version_penalty = 15
            fhash = s.get("FileHash") or ""
            # 歌手一致性：期望歌手应包含在结果歌手里（多歌手分隔兼容）
            want_artists = {x.strip().lower() for x in _re.split(r"[/;、，,&_]", artist or "") if x.strip()}
            got_artist_l = singer.lower()
            artist_penalty = 0
            if want_artists and not any(w in got_artist_l or got_artist_l in w for w in want_artists):
                artist_penalty = 25   # 歌手完全对不上，基本淘汰
            meta = SongMeta(
                title=name,
                artist=singer,
                artists=[singer],
                album=s.get("AlbumName") or "",
                duration=int(s.get("Duration") or 0),
                source=self.name,
                song_id=fhash,
                album_id=str(s.get("AlbumID") or ""),
                confidence=max(1.0, simple_score(title, artist, name, [singer])
                               - version_penalty - artist_penalty),
            )
            # 封面：按 hash 拼 URL（酷狗封面规则），enrich 时再取歌词
            if fhash:
                meta.extra["cover_url"] = (
                    f"https://imgessl.kugou.com/stdmusic/400/{fhash}.jpg")
            metas.append(meta)
        # 原唱优先：标题/歌手完全匹配的排前面
        metas.sort(key=lambda m: m.confidence, reverse=True)
        return metas

    def enrich(self, meta: SongMeta) -> SongMeta:
        """补全歌词（酷狗 lyrics 接口）。"""
        if not meta.song_id:
            return meta
        try:
            d = self._get_json(self.LYRIC_SEARCH_URL + "?" + urllib.parse.urlencode(
                {"ver": 1, "man": "yes", "client": "pc",
                 "keyword": f"{meta.title} {meta.artist}"}))
            cand = ((d.get("candidates") or [{}])[0])
            cid, cak = cand.get("id"), cand.get("accesskey")
            if cid and cak:
                d2 = self._get_json(self.LYRIC_DOWN_URL + "?" + urllib.parse.urlencode(
                    {"ver": 1, "client": "pc", "id": cid,
                     "accesskey": cak, "fmt": "lrc"}))
                content = d2.get("content") or ""
                if content:
                    lrc = base64.b64decode(content).decode("utf-8", "replace")
                    if lrc.strip():
                        meta.extra["lyrics"] = lrc
        except Exception:
            pass
        return meta


register_source("kugou", KugouSource)
