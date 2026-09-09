# ============================================================
# 单文件插件版（免配置数据源）
# 用法：把本文件放入应用的「插件目录」（安装时配置，默认
# 应用数据目录 plugins/），重启应用后在配置页勾选该源。
# 依赖应用内置的 musicmeta 框架（base/registry/fingerprint）。
# ============================================================
# -*- coding: utf-8 -*-
"""LRC Lib 歌词元数据源（内置，免配置）。

基于公开接口 https://lrclib.net（无需注册、无需 API key、开源社区维护）：
- 搜索:   GET https://lrclib.net/api/search?artist_name=..&track_name=..&limit=N
- 精确:   GET https://lrclib.net/api/get?artist_name=..&track_name=..

特点：收录海量中文/日韩/欧美曲目的**同步歌词（LRC）与纯文本歌词**，
同时给出专辑名与时长。不提供封面，选中后由应用自动回查 QQ 音乐补封面。

限速：官方建议低频访问，本类默认 ≥1 秒/次。
"""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from typing import List

from musicmeta.sources.base import MetaSource, SongMeta, normalize_text, simple_score
from musicmeta.sources.registry import register_source
from musicmeta import ratelimit as _ratelimit

_API = "https://lrclib.net/api"
_UA = ("MusicMetaScraper/1.0 (local NAS music tag tool; contact: local@nas.local) "
       "+https://lrclib.net")


class LrclibSource(MetaSource):
    name = "lrclib"

    def __init__(self, min_interval: float = 1.0, **kwargs):
        self.min_interval = max(float(min_interval), 1.0)  # 至少 1 秒/次

    def _throttle(self):
        """限速（应用层全局共享 + 随机抖动，所有源实例共用）。"""
        _ratelimit.throttle(getattr(self, "min_interval", 0.3))
    def _request(self, path: str, params: dict) -> list:
        self._throttle()
        url = f"{_API}{path}?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))

    def _to_meta(self, item: dict, want_title: str, want_artist: str) -> SongMeta:
        name = (item.get("trackName") or "").strip()
        artists = [a.strip() for a in (item.get("artistName") or "").split(",") if a.strip()]
        if not name:
            return None
        meta = SongMeta(
            title=name,
            artist=" / ".join(artists),
            artists=artists or [item.get("artistName") or ""],
            album=(item.get("albumName") or "").strip(),
            duration=int(item.get("duration") or 0),
            source=self.name,
            song_id=str(item.get("id") or ""),
            confidence=simple_score(want_title, want_artist, name, artists),
        )
        # 同步歌词优先，纯文本兜底
        lyrics = (item.get("syncedLyrics") or "").strip() or (item.get("plainLyrics") or "").strip()
        if lyrics:
            meta.extra["lyrics"] = lyrics
        return meta

    def search(self, title: str, artist: str = "", limit: int = 10) -> List[SongMeta]:
        params = {"limit": max(limit, 5)}
        if artist.strip():
            params["artist_name"] = artist.strip()
        params["track_name"] = title.strip()
        try:
            data = self._request("/search", params)
        except Exception:
            data = None
        metas = []
        seen = set()
        for item in data or []:
            meta = self._to_meta(item, title, artist)
            if meta is None:
                continue
            key = meta.song_id or meta.title
            if key in seen:
                continue
            seen.add(key)
            metas.append(meta)
            if len(metas) >= limit:
                break
        # 精确兜底：/search 没搜到（冷门歌未被收录到搜索索引）时，
        # 用 /api/get 按 歌名+歌手 精确查询（一次返回 dict）
        if not metas and artist.strip():
            try:
                item = self._request("/get", {
                    "artist_name": artist.split("/")[0].strip() or artist.strip(),
                    "track_name": title.strip(),
                })
                if isinstance(item, dict):
                    meta = self._to_meta(item, title, artist)
                    if meta is not None:
                        metas.append(meta)
            except Exception:
                pass
        return metas

    def enrich(self, meta: SongMeta) -> SongMeta:
        """补全歌词（搜索结果未带歌词时回查 /api/get）。"""
        if meta.extra.get("lyrics"):
            return meta
        params = {"track_name": meta.title}
        if meta.artist:
            params["artist_name"] = meta.artist.replace(" / ", ",").split(",")[0].strip()
        try:
            data = self._request("/get", params)
            if isinstance(data, dict):
                lyrics = (data.get("syncedLyrics") or "").strip() or \
                         (data.get("plainLyrics") or "").strip()
                if lyrics:
                    meta.extra["lyrics"] = lyrics
                if not meta.album and data.get("albumName"):
                    meta.album = data["albumName"]
                if not meta.duration and data.get("duration"):
                    meta.duration = int(data["duration"])
        except Exception:
            pass
        return meta


register_source("lrclib", LrclibSource)
