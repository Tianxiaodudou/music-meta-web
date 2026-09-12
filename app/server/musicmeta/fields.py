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

#: **本应用只服务飞牛官方音乐**：它读的标签我们才写，它不读的一律不写
#: （用户 2026-09-12 拍板）。所以字段分成两类：
#:
#: - 可写：飞牛音乐真的会读这个标签（写入器按这些键写，见 musicmeta/writer.py）
#: - 只读：飞牛音乐从来不读这个键（服务端二进制里 PUBLISHER / LABEL / LANGUAGE
#:   一次都没出现），刮削拿到也**不会写进文件**；界面上显示为不可勾选，避免
#:   「勾了却没效果」的误解。
WRITABLE_FIELDS: Tuple[str, ...] = (
    "title", "artist", "album", "album_artist", "year", "genre",
    "track", "track_total", "disc", "cover", "lyrics",
)
#: 飞牛音乐不读的标签：保留字段是为了界面仍能显示/筛选这些信息，但永不写入
READONLY_FIELDS: Tuple[str, ...] = ("publisher", "language")


def is_writable(key: str) -> bool:
    """该字段写进文件后，飞牛音乐会读到吗？（决定它能不能被勾选）"""
    return key in WRITABLE_FIELDS


FIELD_MAP: Dict[str, FieldDef] = {f.key: f for f in FIELDS}

#: 默认生效字段：歌名、歌手、封面图、歌词
DEFAULT_ACTIVE: Tuple[str, ...] = ("title", "artist", "cover", "lyrics")


def parse_active(value) -> List[str]:
    """把配置值（逗号分隔字符串或列表）解析为生效字段列表（按 FIELDS 顺序）。

    只保留**可写字段**：飞牛音乐不读的标签（唱片公司 / 语言）即使被旧配置勾着，
    也不会出现在生效字段里 —— 它们本来就写不进文件。
    """
    if isinstance(value, (list, tuple, set)):
        raw = {str(x).strip() for x in value}
    else:
        raw = {x.strip() for x in str(value or "").split(",")}
    raw &= set(WRITABLE_FIELDS)
    picked = [f.key for f in FIELDS if f.key in raw]
    return picked or list(DEFAULT_ACTIVE)


def active_fields(cfg: dict) -> List[str]:
    """从配置字典取出生效字段（缺省用默认值）。"""
    return parse_active(cfg.get("active_fields"))
