# 元数据源插件开发规范（SPEC）

本安装包**不内置任何数据源**。官方免配置数据源（单文件插件版）在本仓库的
`data-source-plugins/` 目录（github.com/Tianxiaodudou/music-meta-web）：
qqmusic / netease / kugou / kuwo / lrclib / theaudiodb / itunes。
把需要的 `.py` 复制到插件目录，重启应用后在配置页「元数据源」勾选。

## 最小插件模板

```python
# -*- coding: utf-8 -*-
"""我的数据源：单文件插件。复制到插件目录，重启后勾选。"""
from __future__ import annotations
from typing import List

from musicmeta.sources.base import MetaSource, SongMeta
from musicmeta.sources.registry import register_source


class MySource(MetaSource):
    name = "my_source"

    def __init__(self, min_interval: float = 0.3, **kwargs):
        self.min_interval = min_interval

    def search(self, title: str, artist: str = "", limit: int = 10) -> List[SongMeta]:
        # 1) 构造搜索词（歌名+歌手） 2) HTTP 请求你的数据渠道（GET+urllib 即可）
        # 3) 解析 JSON → 构造 SongMeta 列表 4) confidence 打分(0~100)
        # 5) 需要时 enrich() 用 song_id/album_id 补详情
        return []   # ← 你的实现

    def enrich(self, meta: SongMeta) -> SongMeta:
        return meta   # 可选：补封面(extra["cover_url"])/歌词(extra["lyrics"])


register_source("my_source", MySource)   # 小写英文名
```

## SongMeta 字段
title 歌曲名 | artist 歌手 | album 专辑 | album_artist 专辑艺人
date 发行日期(YYYY 或 YYYY-MM-DD) | genre 流派
track 曲目号 | track_total 总曲目 | disc 碟号
publisher 唱片公司 | language 语言 | comment 注释
song_id/album_id 源内ID | confidence 置信度(0~100，/100 与阈值比较)
extra 附加: cover_url(封面URL)、lyrics(LRC歌词)、duration

## 打分
可用 musicmeta.sources.base.simple_score(want_title, want_artist, got_title, got_artists)
（0~100：基础40 + 标题一致/包含 + 歌手一致/包含）。参考实现：
data-source-plugins/qqmusic.py（最完整）。

## 封面/歌词兜底
插件在 extra 提供 cover_url/lyrics 则直接使用；否则应用尝试回查已安装的 qqmusic 插件。

## 生效
文件放入本目录后重启应用；加载失败打印到应用日志，不影响其他源。
