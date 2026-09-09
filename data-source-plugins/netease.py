# ============================================================
# 单文件插件版（免配置数据源）
# 用法：把本文件放入应用的「插件目录」（安装时配置，默认
# 应用数据目录 plugins/），重启应用后在配置页勾选该源。
# 依赖应用内置的 musicmeta 框架（base/registry/fingerprint）。
# ============================================================
# -*- coding: utf-8 -*-
"""网易云音乐元数据源（内置）。

基于公开搜索接口（无需登录）：
- 搜索:   POST https://music.163.com/api/search/get/web  (s=歌名 歌手&type=1)
- 详情:   GET  https://music.163.com/api/song/detail?ids=[id]  (封面 album.picUrl)
- 歌词:   GET  https://music.163.com/api/song/lyric?id={id}&lv=-1  (LRC)

接口为公开/非官方，若失效请更新本文件（可整体替换为插件）。
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

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


class NeteaseSource(MetaSource):
    name = "netease"
    SEARCH_URL = "https://music.163.com/api/search/get/web"
    LYRIC_URL = "https://music.163.com/api/song/lyric"
    DETAIL_URL = "https://music.163.com/api/song/detail"

    def __init__(self, min_interval: float = 0.3, **kwargs):
        self.min_interval = min_interval

    def _throttle(self):
        """限速（应用层全局共享 + 随机抖动，所有源实例共用）。"""
        _ratelimit.throttle(getattr(self, "min_interval", 0.3))
    def _request(self, url: str, data: bytes = None) -> dict:
        self._throttle()
        headers = {"User-Agent": _UA, "Referer": "https://music.163.com/"}
        if data is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        req = urllib.request.Request(url, data=data, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))

    def search(self, title: str, artist: str = "", limit: int = 10) -> List[SongMeta]:
        query = f"{title} {artist}".strip()
        data = urllib.parse.urlencode(
            {"s": query, "type": 1, "limit": max(limit, 5), "offset": 0}).encode()
        d = self._request(self.SEARCH_URL, data)
        metas: List[SongMeta] = []
        for s in (d.get("result") or {}).get("songs", []) or []:
            name = (s.get("name") or "").replace("\ufeff", "").strip()
            artists = [a.get("name", "").strip()
                       for a in (s.get("artists") or []) if a.get("name")]
            if not name or not artists:
                continue
            album = s.get("album") or {}
            date_ms = album.get("publishTime") or 0
            meta = SongMeta(
                title=name,
                artist=" / ".join(artists),
                artists=artists,
                album=(album.get("name") or "").replace("\ufeff", "").strip(),
                date=time.strftime("%Y-%m-%d", time.localtime(date_ms / 1000))
                     if date_ms else "",
                duration=int(s.get("duration") or 0) // 1000,
                source=self.name,
                song_id=str(s.get("id") or ""),
                album_id=str(album.get("id") or ""),
                confidence=simple_score(title, artist, name, artists),
            )
            metas.append(meta)
        return metas

    def enrich(self, meta: SongMeta) -> SongMeta:
        """补全封面与歌词（网易云自带，无需回查 QQ）。"""
        if meta.song_id:
            try:
                d = self._request(self.DETAIL_URL + "?ids=[" + meta.song_id + "]")
                songs = d.get("songs") or []
                if songs:
                    al = songs[0].get("album") or {}
                    if al.get("picUrl"):
                        meta.extra["cover_url"] = al["picUrl"]
            except Exception:
                pass
            try:
                d = self._request(self.LYRIC_URL + "?id=" + meta.song_id +
                                  "&lv=-1&kv=-1&tv=-1")
                lrc = (d.get("lrc") or {}).get("lyric") or ""
                if lrc:
                    meta.extra["lyrics"] = lrc
            except Exception:
                pass
        return meta


register_source("netease", NeteaseSource)
