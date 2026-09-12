# -*- coding: utf-8 -*-
"""可刮削字段定义与「生效字段」解析。

「生效字段」= 应用里唯一一份字段开关（配置项 active_fields）：
- 勾选的字段才会在各界面显示（队列卡片、手动刮削窗口、筛选条件…）
- 也只有勾选的字段会被写入音乐文件

本模块是字段定义的单一来源（key / 显示名 / 标签键 / 候选属性），
webapp 与 scheduler 都从这里取，避免字段清单散落多处。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Tuple


@dataclass(frozen=True)
class FieldDef:
    """一个可刮削字段。"""

    key: str        # 配置/接口里的字段名，如 title
    label: str      # 界面显示名，如 歌名
    tag: str        # 读取内嵌标签时用的键（writer.read_tags 的键）
    cand: str       # 候选 SongMeta 里取值的属性名
    special: str = ""   # "cover"/"lyrics" 表示需要特殊处理（图片/多行）


FIELDS: Tuple[FieldDef, ...] = (
    FieldDef("title", "歌名", "title", "title"),
    FieldDef("artist", "歌手", "artist", "artist"),
    FieldDef("album", "专辑", "album", "album"),
    FieldDef("album_artist", "专辑艺人", "album_artist", "album_artist"),
    FieldDef("year", "年份", "date", "date"),
    FieldDef("genre", "流派", "genre", "genre"),
    FieldDef("track", "曲目号", "track", "track"),
    FieldDef("track_total", "总曲目数", "track_total", "track_total"),
    FieldDef("disc", "碟号", "disc", "disc"),
    FieldDef("publisher", "唱片公司", "publisher", "publisher"),
    FieldDef("language", "语言", "language", "language"),
    FieldDef("cover", "封面图", "", "cover_url", special="cover"),
    FieldDef("lyrics", "歌词", "", "lyrics", special="lyrics"),
)

FIELD_MAP: Dict[str, FieldDef] = {f.key: f for f in FIELDS}

#: 默认生效字段：歌名、歌手、封面图、歌词
DEFAULT_ACTIVE: Tuple[str, ...] = ("title", "artist", "cover", "lyrics")


def parse_active(value) -> List[str]:
    """把配置值（逗号分隔字符串或列表）解析为生效字段列表（按 FIELDS 顺序）。"""
    if isinstance(value, (list, tuple, set)):
        raw = {str(x).strip() for x in value}
    else:
        raw = {x.strip() for x in str(value or "").split(",")}
    picked = [f.key for f in FIELDS if f.key in raw]
    return picked or list(DEFAULT_ACTIVE)


def active_fields(cfg: dict) -> List[str]:
    """从配置字典取出生效字段（缺省用默认值）。"""
    return parse_active(cfg.get("active_fields"))
