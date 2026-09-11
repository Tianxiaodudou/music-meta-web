# -*- coding: utf-8 -*-
"""元数据源插件注册表。

让用户可以自主开发元数据获取渠道（插件）：
1. 把插件 .py 文件放入「插件目录」（安装时配置，默认应用数据目录 plugins/）
2. 插件内定义一个继承 MetaSource 的类，并在模块导入时调用 register_source()
3. 在应用配置页选择该元数据源即可生效（应用每次刮削时通过注册表创建实例）

本安装包**不内置任何数据源**：所有源都从插件目录加载（单文件 .py）。
官方免配置数据源见本仓库 data-source-plugins/ 目录（qqmusic/netease/kugou/
kuwo/lrclib/theaudiodb/itunes）。开发规范见插件目录 SPEC.md。
"""
from __future__ import annotations

import importlib
import os
from typing import Callable, Dict, List

# 源名 -> 工厂函数（返回 MetaSource 实例）
_REGISTRY: Dict[str, Callable] = {}

_PACKAGE_PLUGINS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "plugins")
# 用户插件目录：优先环境变量（安装向导配置，用户可写、可用文件管理器直接放插件），
# 否则回退到包内目录。
_USER_PLUGINS = os.environ.get("MMW_PLUGINS_DIR", "").strip() or ""


def _ensure_dir(path: str) -> bool:
    try:
        os.makedirs(path, exist_ok=True)
        return True
    except OSError:
        return False


# 本安装包不内置数据源：所有源均为插件（空元组，保留 _load_builtins 框架以便未来扩展）
_BUILTIN_MODULES = ()


def _load_builtins() -> None:
    for _m in _BUILTIN_MODULES:
        try:
            importlib.import_module(f"musicmeta.sources.{_m}")
        except Exception as _exc:  # noqa: BLE001
            print(f"[source-registry] 内置源 {_m} 加载失败: {_exc}")


def register_source(name: str, factory: Callable) -> None:
    """注册一个元数据源。factory 接收配置 kwargs，返回 MetaSource 实例。"""
    _REGISTRY[name] = factory


def get_source(name: str, **kwargs):
    """按名称创建元数据源实例；未注册则先加载内置源。"""
    if name not in _REGISTRY:
        _load_builtins()
    if name not in _REGISTRY:
        raise KeyError(f"未知元数据源: {name}（可用: {list_sources()}）")
    return _REGISTRY[name](**kwargs)


def list_sources() -> List[str]:
    """列出所有可用源名称（内置 + 已加载插件）。"""
    _load_builtins()  # 幂等：已导入的内置模块不会重复执行
    return sorted(_REGISTRY)


def discover_plugins() -> List[str]:
    """扫描插件目录并导入所有插件模块，返回成功加载的模块名。

    插件目录：MMW_PLUGINS_DIR（推荐，应用数据目录）优先；其次包内 plugins/。
    任何目录不可读/损坏都不会让应用崩溃（跳过并打印日志）。
    """
    loaded = []
    dirs = []
    if _USER_PLUGINS:
        if _ensure_dir(_USER_PLUGINS):
            dirs.append(_USER_PLUGINS)
    if _PACKAGE_PLUGINS and os.path.isdir(_PACKAGE_PLUGINS):
        dirs.append(_PACKAGE_PLUGINS)

    seen = set()
    for pdir in dirs:
        try:
            names = sorted(os.listdir(pdir))
        except OSError as exc:
            print(f"[source-registry] 插件目录不可读，跳过: {pdir} ({exc})")
            continue
        for fname in names:
            if fname.endswith(".py") and fname != "__init__.py":
                mod = fname[:-3]
                if mod in seen:
                    continue
                seen.add(mod)
                fpath = os.path.join(pdir, fname)
                try:
                    # 按文件路径直接加载（不依赖包路径，插件可用绝对导入）
                    import importlib.util
                    spec = importlib.util.spec_from_file_location(
                        f"musicmeta_source_plugin_{mod}", fpath)
                    if spec is None or spec.loader is None:
                        continue
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)
                    loaded.append(mod)
                except Exception as exc:  # noqa: BLE001
                    print(f"[source-registry] 插件 {fname} 加载失败: {exc}")
    return loaded


# 启动时扫描一次插件目录（新插件需重启应用生效）
discover_plugins()
