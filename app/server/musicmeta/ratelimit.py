# -*- coding: utf-8 -*-
"""应用层全局共享限速器（风控统一由应用层负责，数据源插件不再自带限速）。

设计：
- 所有数据源插件（QQ/网易/未来新源）的网络请求限速都走这里，
  插件本身保持"纯搜索"，不含任何限速/缓存逻辑。
- 全局锁 + 全局时间戳：无论多少个源实例、多少线程（刮削 worker、
  网页接口并发）都共享同一个限速节奏，不会并发打爆数据源接口。
- 随机抖动（0.5x~1.5x）：让请求间隔不规则，避免固定节奏被风控识别。

用法（应用层在调用数据源插件方法之前调用）::

    from musicmeta import ratelimit
    ratelimit.throttle(min_interval=0.3)   # 相邻两次真实网络请求间隔 >= 0.3s

    # 刮削/接口统一入口 scheduler.search_cached / main 的写入流程会调用本模块。
"""
from __future__ import annotations

import random
import threading
import time

_lock = threading.Lock()
_last_request_ts = 0.0


def throttle(min_interval: float = 0.3) -> None:
    """限速：距上一次请求至少 min_interval，并带 0.5x~1.5x 随机抖动。"""
    global _last_request_ts
    if min_interval is None or min_interval <= 0:
        return
    interval = min_interval * (0.5 + random.random())  # 0.5x ~ 1.5x
    with _lock:
        wait = _last_request_ts + interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_request_ts = time.monotonic()
