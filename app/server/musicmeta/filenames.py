# -*- coding: utf-8 -*-
"""文件名解析：'歌曲名-歌手.后缀' -> 标题/歌手。

约定：
- 支持扩展名: .flac .mp3 .ogg .ape（大小写不敏感）
- 标题与歌手以 '-' 分隔；由于歌手名本身可能含 '-'（如 "O-Zone"），
  提供 parse_candidates() 返回多种切分候选，由上层按元数据匹配置信度择优。
- 文件名开头的曲目号（如 "01. 晴天-周杰伦.flac"）会被识别为 track_hint。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Tuple

SUPPORTED_EXTS = {".flac", ".mp3", ".ogg", ".ape"}

# 开头曲目号: "01. " / "01-" / "01_" / "01、" / "01 "（数字后跟分隔符或空格）
_TRACK_PREFIX_RE = re.compile(r"^\s*(\d{1,3})\s*[.\-_、]\s*")
_MAX_SPLITS = 3  # 最多尝试从最后 1..N 个 '-' 处切分


@dataclass
class ParsedName:
    """一个文件名切分候选。"""

    stem: str        # 原始文件名（去掉扩展名）
    title: str
    artist: str      # 可能为空（文件名里没有 '-'）
    ext: str         # ".flac" 等，小写；无扩展名为 ""
    track_hint: str  # 文件名开头的曲目号（可能为空）


def split_ext(filename: str) -> Tuple[str, str]:
    """返回 (stem, ext)。无扩展名时 ext=''。"""
    name = filename.strip()
    idx = name.rfind(".")
    if idx <= 0:
        return name, ""
    return name[:idx], name[idx:].lower()


def strip_track_prefix(stem: str) -> Tuple[str, str]:
    """去掉开头曲目号：'01. 晴天-周杰伦' -> ('01', '晴天-周杰伦')。"""
    m = _TRACK_PREFIX_RE.match(stem)
    if m:
        return m.group(1), stem[m.end():].strip()
    return "", stem.strip()


def dash_splits(stem: str, max_splits: int = _MAX_SPLITS) -> List[Tuple[str, str]]:
    """按 '-' 生成 (标题, 歌手) 候选切分，优先从最后一个 '-' 切。

    例: 'Dragostea Din Tei-O-Zone'
        -> [('Dragostea Din Tei-O', 'Zone'), ('Dragostea Din Tei', 'O-Zone')]
    """
    parts = [p.strip() for p in stem.split("-")]
    if len(parts) < 2:
        return [(stem, "")]
    out: List[Tuple[str, str]] = []
    start = max(len(parts) - 1 - max_splits, 0)
    for i in range(len(parts) - 1, start - 1, -1):
        title = "-".join(parts[:i]).strip()
        artist = "-".join(parts[i:]).strip()
        if title and artist:
            out.append((title, artist))
    if not out:
        out.append((stem, ""))
    return out


def parse_candidates(filename: str) -> List[ParsedName]:
    """把一个文件名解析为若干候选 (标题, 歌手)，按切分优先级排序。"""
    stem, ext = split_ext(filename)
    track_hint, rest = strip_track_prefix(stem)
    if not rest:
        rest = stem
    return [
        ParsedName(stem=stem, title=title, artist=artist, ext=ext,
                   track_hint=track_hint)
        for title, artist in dash_splits(rest)
    ]
