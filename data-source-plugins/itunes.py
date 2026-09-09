# ============================================================
# 单文件插件版：iTunes 搜索（示例源）
# 用法：放入应用插件目录，重启后在配置页勾选 itunes。
# ============================================================
# -*- coding: utf-8 -*-
"""示例元数据源插件：iTunes 搜索（真实搜索引擎演示）。

复制本文件改名，实现自己的搜索逻辑（完整规范见 SPEC.md；
QQ 音乐源是更完整的参考：musicmeta/sources/qqmusic.py）。
"""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from typing import List

from musicmeta.sources.base import MetaSource, SongMeta
from musicmeta.sources.registry import register_source
from musicmeta import ratelimit as _ratelimit


class ITunesSource(MetaSource):
    name = "itunes"
    SEARCH_URL = "https://itunes.apple.com/search"

    def __init__(self, min_interval: float = 0.3, **kwargs):
        self.min_interval = min_interval

    def _throttle(self):
        """限速（应用层全局共享 + 随机抖动，所有源实例共用）。"""
        _ratelimit.throttle(getattr(self, "min_interval", 0.3))
    def _get_json(self, url: str) -> dict:
        self._throttle()
        req = urllib.request.Request(
            url, headers={"User-Agent": "music-meta-web/1.0 (source plugin)"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))

    def search(self, title: str, artist: str = "", limit: int = 10) -> List[SongMeta]:
        term = f"{title} {artist}".strip()
        url = self.SEARCH_URL + "?" + urllib.parse.urlencode({
            "term": term, "media": "music", "limit": max(limit, 5),
            "country": "CN", "entity": "song"})
        data = self._get_json(url)
        metas: List[SongMeta] = []
        for item in data.get("results", []):
            title_ = (item.get("trackName") or "").strip()
            artist_ = (item.get("artistName") or "").strip()
            if not title_ or not artist_:
                continue
            meta = SongMeta(
                title=title_, artist=artist_,
                album=item.get("collectionName") or "",
                date=(item.get("releaseDate") or "")[:10],
                genre=item.get("primaryGenreName") or "",
                duration=int(item.get("trackTimeMillis") or 0) // 1000,
                source=self.name,
                song_id=str(item.get("trackId") or ""),
                album_id=str(item.get("collectionId") or ""),
                confidence=80.0,
            )
            art = item.get("artworkUrl100") or ""
            if art:
                meta.extra["cover_url"] = art.replace("100x100", "300x300")
            metas.append(meta)
        return metas


register_source("itunes", ITunesSource)
