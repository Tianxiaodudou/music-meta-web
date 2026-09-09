"""元数据源包（框架部分；数据源均为插件，从插件目录加载）。"""

from .base import MetaSource, SongMeta, normalize_text, simple_score
from .registry import register_source, get_source, list_sources

__all__ = ["MetaSource", "SongMeta", "normalize_text", "simple_score",
           "register_source", "get_source", "list_sources"]
