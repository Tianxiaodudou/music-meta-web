# ============================================================
# 单文件插件版（免配置数据源）
# 用法：把本文件放入应用的「插件目录」（安装时配置，默认
# 应用数据目录 plugins/），重启应用后在配置页勾选该源。
# 依赖应用内置的 musicmeta 框架（base/registry/fingerprint）。
# ============================================================
# -*- coding: utf-8 -*-
"""酷我音乐元数据源（内置）。

基于公开搜索接口（无需登录）：
- 搜索:   GET http://search.kuwo.cn/r.s?client=kt&all=歌名 歌手（返回 JS 字面量，用 ast 解析）
- 封面:   https://img2.kuwo.cn/star/albumcover/{web_albumpic_short}（按返回路径拼 URL）
- 歌词:   官方歌词接口已限制（返回"音乐查询失败"），由应用自动回查 QQ 源补全

接口为公开/非官方，若失效请更新本文件（可整体替换为插件）。
"""
from __future__ import annotations

import ast
import re
import time
import urllib.parse
import urllib.request
from typing import List

from musicmeta.sources.base import MetaSource, SongMeta, simple_score
from musicmeta.sources.registry import register_source
from musicmeta import ratelimit as _ratelimit

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


class KuwoSource(MetaSource):
    name = "kuwo"
    SEARCH_URL = "http://search.kuwo.cn/r.s"
    # 标题里 "-《xxx》" 形式的副标题后缀（网络电影插曲等）
    _SUFFIX_RE = re.compile(r"[-—–]\s*《.*?》.*$")

    def __init__(self, min_interval: float = 0.3, **kwargs):
        self.min_interval = min_interval

    def _throttle(self):
        """限速（应用层全局共享 + 随机抖动，所有源实例共用）。"""
        _ratelimit.throttle(getattr(self, "min_interval", 0.3))
    def _get_text(self, url: str) -> str:
        self._throttle()
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.read().decode("utf-8", "replace")

    def search(self, title: str, artist: str = "", limit: int = 10) -> List[SongMeta]:
        query = f"{title} {artist}".strip()
        url = self.SEARCH_URL + "?" + urllib.parse.urlencode({
            "client": "kt", "all": query, "pn": 0, "rn": max(limit, 5), "uid": 0,
            "ver": "kwplayer_ar_9.2.2.1", "vipver": 1, "show_copyright_off": 1,
            "new_format": 1, "ft": "music", "encoding": "utf8", "rformat": "json"})
        raw = self._get_text(url)
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            return []
        d = ast.literal_eval(raw[start:end + 1])  # 返回 JS 字面量，非严格 JSON

        metas: List[SongMeta] = []
        for s in (d.get("abslist") or [])[: max(limit, 5)]:
            raw_name = (s.get("SONGNAME") or "").strip()
            name = self._SUFFIX_RE.sub("", raw_name).strip() or raw_name
            singer = (s.get("ARTIST") or "").strip()
            if not name or not singer:
                continue
            meta = SongMeta(
                title=name,
                artist=singer,
                artists=[singer],
                album=s.get("ALBUM") or "",
                duration=int(s.get("DURATION") or 0),
                source=self.name,
                song_id=s.get("MUSICRID") or "",
                album_id=str(s.get("ALBUMID") or ""),
                confidence=simple_score(title, artist, name, [singer]),
            )
            pic_short = s.get("web_albumpic_short") or ""
            if pic_short:
                meta.extra["cover_url"] = (
                    "https://img2.kuwo.cn/star/albumcover/" + pic_short)
            metas.append(meta)
        return metas

    # 歌词：酷我歌词接口已限制，交由应用回查 QQ 源补全（见 scheduler._write_fields）


register_source("kuwo", KuwoSource)
