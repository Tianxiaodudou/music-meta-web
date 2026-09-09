# -*- coding: utf-8 -*-
"""元数据源公共基类与数据模型。

任何元数据源（QQ 音乐、网易云、iTunes 等）都实现 MetaSource 接口，
返回统一的 SongMeta 数据模型，方便后续写入器（mutagen）统一消费。
"""
from __future__ import annotations

import re
import unicodedata
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Optional


def normalize_text(text: str) -> str:
    """归一化文本用于比对：NFKC 统一全半角、去空白、去标点、转小写。

    例: " 晴天 (Live) " -> "晴天live"；"L.A.Boyz" -> "laboyz"。
    """
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", str(text)).strip().lower()
    # 只保留字母/数字/下划线/中日韩文字，去掉空白与标点
    return re.sub(r"[^\w]+", "", text, flags=re.UNICODE)


def simple_score(want_title: str, want_artist: str,
                 got_title: str, got_artists) -> float:
    """轻量匹配打分（供第三方源使用）：0~100，≥阈值(默认90)自动写入。

    规则：基础 40 + 标题完全一致 30/互相包含 12 + 歌手一致/包含 20。
    """
    score = 40.0
    wt, gt = normalize_text(want_title), normalize_text(got_title)
    if gt and wt:
        if gt == wt:
            score += 30
        elif min(len(gt), len(wt)) >= 4 and (gt in wt or wt in gt):
            score += 12
    wa = normalize_text(want_artist)
    ga = "".join(normalize_text(a) for a in (got_artists or []))
    if wa and ga:
        if wa == ga or wa in ga or ga in wa:
            score += 20
    return round(min(score, 100.0), 1)


@dataclass
class SongMeta:
    """一首歌的标准化元数据。空字符串表示未知。"""

    title: str = ""                                  # 歌曲名
    artist: str = ""                                 # 显示用歌手，多歌手用 " / " 连接
    artists: List[str] = field(default_factory=list)  # 歌手列表
    album: str = ""                                  # 专辑名
    album_artist: str = ""                           # 专辑艺人
    date: str = ""                                   # 发行日期 "YYYY" 或 "YYYY-MM-DD"
    genre: str = ""                                  # 流派
    track: str = ""                                  # 曲目号
    track_total: str = ""                            # 专辑总曲目
    disc: str = ""                                   # 碟片号
    publisher: str = ""                              # 唱片公司
    language: str = ""                               # 语言
    duration: int = 0                                # 时长（秒）
    comment: str = ""
    source: str = ""                                 # 来源名，如 "qqmusic"
    song_id: str = ""                                # 源内歌曲 ID（如 songmid）
    album_id: str = ""                               # 源内专辑 ID（如 albummid）
    confidence: float = 0.0                          # 匹配置信度，<=0 视为不匹配
    extra: dict = field(default_factory=dict)        # 源特有附加信息

    def is_complete(self) -> bool:
        return bool(self.title and self.artist)


class MetaSource(ABC):
    """音乐元数据源接口。"""

    name: str = "base"
    #: lookup() 接受的最低置信度，低于该值视为无匹配
    min_confidence: float = 0.0

    @abstractmethod
    def search(self, title: str, artist: str = "", limit: int = 10) -> List[SongMeta]:
        """按歌名（必填）+ 歌手（可选）搜索，返回候选列表（按置信度降序）。"""

    def enrich(self, meta: SongMeta) -> SongMeta:
        """可选：补全专辑/发行日期/流派/曲目号等详情。默认原样返回。"""
        return meta

    def lookup(self, title: str, artist: str = "", limit: int = 10) -> Optional[SongMeta]:
        """搜索并返回最佳匹配（已 enrich 补全）。无可信匹配返回 None。"""
        candidates = self.search(title, artist, limit=limit)
        if not candidates:
            return None
        best = max(candidates, key=lambda m: m.confidence)
        if best.confidence < self.min_confidence:
            return None
        return self.enrich(best)
