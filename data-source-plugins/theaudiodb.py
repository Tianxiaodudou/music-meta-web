# ============================================================
# 单文件插件版（免配置数据源）
# 用法：把本文件放入应用的「插件目录」（安装时配置，默认
# 应用数据目录 plugins/），重启应用后在配置页勾选该源。
# 依赖应用内置的 musicmeta 框架（base/registry/fingerprint）。
# ============================================================
# -*- coding: utf-8 -*-
"""TheAudioDB 元数据源（内置，免配置）。

基于公开接口 https://www.theaudiodb.com（免费、无需注册；demo key "2" 随 API 公开）：
- 搜索:   GET https://www.theaudiodb.com/api/v1/json/2/searchtrack.php?s=歌手&t=歌名
- 详情:   GET https://www.theaudiodb.com/api/v1/json/2/track.php?i=曲目ID

特点：收录大量**欧美/日韩国际曲目**，提供流派、唱片公司、发行年份、专辑、
封面与简介——这些字段 QQ/网易等中文源常缺失，正好互补。
中文曲目基本查不到（数据库以国际音乐为主），属补充型源。

限速：demo key 官方限制 1 次/秒，本类默认 ≥1 秒/次。
"""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from typing import List

from musicmeta.sources.base import MetaSource, SongMeta, simple_score
from musicmeta.sources.registry import register_source
from musicmeta import ratelimit as _ratelimit

_API = "https://www.theaudiodb.com/api/v1/json/2"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


class TheAudioDBSource(MetaSource):
    name = "theaudiodb"

    def __init__(self, min_interval: float = 1.0, **kwargs):
        self.min_interval = max(float(min_interval), 1.0)  # demo key 限 1 次/秒

    def _throttle(self):
        """限速（应用层全局共享 + 随机抖动，所有源实例共用）。"""
        _ratelimit.throttle(getattr(self, "min_interval", 0.3))
    def _request(self, path: str) -> dict:
        self._throttle()
        url = f"{_API}/{path}"
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))

    def _to_meta(self, item: dict, want_title: str, want_artist: str) -> SongMeta:
        name = (item.get("strTrack") or "").strip()
        artist = (item.get("strArtist") or "").strip()
        if not name:
            return None
        cover = (item.get("strTrackThumb") or "").strip() or \
                (item.get("strAlbumThumb") or "").strip()
        meta = SongMeta(
            title=name,
            artist=artist,
            artists=[artist] if artist else [],
            album=(item.get("strAlbum") or "").strip(),
            date=str(item.get("intYearReleased") or "").strip(),
            genre=(item.get("strGenre") or "").strip(),
            publisher=(item.get("strLabel") or "").strip(),
            source=self.name,
            song_id=str(item.get("idTrack") or ""),
            confidence=simple_score(want_title, want_artist, name, [artist]),
        )
        if cover:
            meta.extra["cover_url"] = cover
        if item.get("strDescriptionEN"):
            meta.comment = item["strDescriptionEN"][:500]
        return meta

    def search(self, title: str, artist: str = "", limit: int = 10) -> List[SongMeta]:
        params = {"s": artist.strip() or title.strip()}
        params["t"] = title.strip()
        try:
            d = self._request("searchtrack.php?" + urllib.parse.urlencode(params))
        except Exception:
            return []
        metas = []
        seen = set()
        for item in d.get("track") or []:
            meta = self._to_meta(item, title, artist)
            if meta is None:
                continue
            key = meta.song_id or (meta.title + meta.artist)
            if key in seen:
                continue
            seen.add(key)
            metas.append(meta)
            if len(metas) >= limit:
                break
        return metas

    def enrich(self, meta: SongMeta) -> SongMeta:
        """用 track.php 补全流派/厂牌/年份/封面（字段缺失时）。"""
        if not meta.song_id:
            return meta
        try:
            d = self._request("track.php?" + urllib.parse.urlencode({"i": meta.song_id}))
            t = (d.get("track") or [{}])[0]
            if not meta.genre:
                meta.genre = (t.get("strGenre") or "").strip()
            if not meta.publisher:
                meta.publisher = (t.get("strLabel") or "").strip()
            if not meta.date:
                meta.date = str(t.get("intYearReleased") or "").strip()
            if not meta.extra.get("cover_url"):
                cover = (t.get("strTrackThumb") or "").strip() or \
                        (t.get("strAlbumThumb") or "").strip()
                if cover:
                    meta.extra["cover_url"] = cover
        except Exception:
            pass
        return meta


register_source("theaudiodb", TheAudioDBSource)
