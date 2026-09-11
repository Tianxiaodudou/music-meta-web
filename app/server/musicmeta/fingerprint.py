# -*- coding: utf-8 -*-
"""`musicmeta.fingerprint` 兼容入口。

实现本体在 `musicmeta.sources.fingerprint`；早期发布的数据源插件写的是
`from musicmeta.fingerprint import recognize`（qpublicmusic 等插件至今如此），
为兼容**已安装的旧插件**（用户插件目录里的 .py 不会随应用升级而更新），
这里保留一层转发，避免指纹模式报 ModuleNotFoundError。

新代码请直接从 `musicmeta.sources.fingerprint` 导入。
"""
from __future__ import annotations

from musicmeta.sources.fingerprint import (  # noqa: F401
    ACOUSTID_URL,
    FPCALC_DEFAULT,
    fingerprint_file,
    lookup_acoustid,
    recognize,
)

__all__ = ["ACOUSTID_URL", "FPCALC_DEFAULT", "fingerprint_file",
           "lookup_acoustid", "recognize"]
