# -*- coding: utf-8 -*-
"""飞牛网关启动器：让 uvicorn 监听 Unix Socket，由 fnOS 统一网关转发 /app/music-meta-web。

用法: python run_server.py [socket_path]
环境:
- MMW_DB             SQLite 路径（由 cmd/main 注入 $TRIM_PKGVAR/app.db）
- MMW_PLUGINS_DIR    数据源插件目录（安装向导 wizard_plugins_dir，cmd/main 转发）
- MMW_MUSIC_DIR / MMW_ACOUSTID_KEY / MMW_CACHE_DIR
                     安装向导 music_dir / acoustid_key（cmd/main 转发，首次启动写入配置）

本安装包不内置任何数据源：所有源从插件目录加载（单文件 .py）。
"""
import os
import sys

_APP_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _APP_DIR)

# 数据源插件目录：安装向导配置（cmd/main 转发），否则回退应用数据目录 plugins/。
# 首次启动时创建目录并写入开发规范（SPEC.md），不内置任何数据源。
_SPEC = '''# 元数据源插件开发规范（SPEC）

本安装包**不内置任何数据源**。官方免配置数据源（单文件插件版）在本仓库的
`data-source-plugins/` 目录（github.com/Tianxiaodudou/music-meta-web）：
qqmusic / netease / kugou / kuwo / lrclib / theaudiodb / itunes。
把需要的 `.py` 复制到本目录，重启应用后在配置页「元数据源」勾选。

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
        # 1) 构造搜索词（文件名模式下 artist 为空、title 是候选关键词）
        # 2) HTTP 请求你的数据渠道（GET+urllib 即可）
        # 3) 解析 JSON → 构造 SongMeta 列表 4) confidence 可留 0（应用层会重算排序分）
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
song_id/album_id 源内ID | confidence 源内参考分(0~100)
extra 附加: cover_url(封面URL)、lyrics(LRC歌词)、duration

## 命中判定与打分（v1.4 起）
- 命中判定不看分数：文件名模式下，应用把文件名拆成候选关键词逐个搜索（命中即停），
  再用搜索结果反推——结果的 trackName 与 artistName 都要出现在原始文件名中才算命中
  （顺序无关，兼容「歌手-歌名」与「歌名-歌手」）。因此插件收到的 search() 中
  artist 为空、title 是候选关键词，插件不必、也无法自行判断谁是歌手。
- 打分只用于候选排序，由应用层统一重算（校验通过 +40 / 时长接近度 +40 / 关键词吻合 +15+5）。
  插件内部的 confidence 仅作参考，可留 0；`simple_score()` 仍可用于插件自己筛结果。
  参考实现：
data-source-plugins/qqmusic.py（最完整）。

## 封面/歌词兜底
插件在 extra 提供 cover_url/lyrics 则直接使用；否则应用尝试回查已安装的 qqmusic 插件。

## 生效
文件放入本目录后重启应用；加载失败打印到应用日志，不影响其他源。
'''

_VAR = os.environ.get("TRIM_PKGVAR", "")
_plugins_dir = (os.environ.get("MMW_PLUGINS_DIR", "").strip()
                or (os.path.join(_VAR, "plugins") if _VAR else ""))
if _plugins_dir:
    try:
        os.makedirs(_plugins_dir, exist_ok=True)
        os.environ.setdefault("MMW_PLUGINS_DIR", _plugins_dir)
        for _name, _content in (("__init__.py", "# -*- coding: utf-8 -*-\n"),
                                ("SPEC.md", _SPEC)):
            _dst = os.path.join(_plugins_dir, _name)
            if not os.path.exists(_dst):
                with open(_dst, "w", encoding="utf-8") as _fh:
                    _fh.write(_content)
    except OSError:
        pass

import uvicorn  # noqa: E402

SOCK = sys.argv[1] if len(sys.argv) > 1 else "/var/apps/music-meta-web/target/app.sock"


def _ensure_multipart() -> None:
    """确保 python-multipart 已安装（封面上传功能需要）。

    以应用用户运行，可直接 pip 装进自己的 venv；装不上不阻塞启动。
    """
    try:
        import multipart  # noqa: F401
        return
    except ImportError:
        pass
    import subprocess
    mirrors = [
        "https://mirrors.cloud.tencent.com/pypi/simple",
        "https://repo.huaweicloud.com/repository/pypi/simple",
        "https://pypi.tuna.tsinghua.edu.cn/simple",
        "https://mirrors.aliyun.com/pypi/simple",
    ]
    for m in mirrors:
        try:
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "--quiet",
                 "--timeout", "20", "--retries", "1", "-i", m, "python-multipart"],
                check=True, timeout=180)
            import multipart  # noqa: F401
            print("[run_server] python-multipart 已安装", flush=True)
            return
        except Exception:  # noqa: BLE001
            continue


def _apply_wizard_env() -> None:
    """把安装向导（wizard/install、wizard/config）的值写入应用配置。

    cmd/main 把 wizard_* 环境变量转发为 MMW_MUSIC_DIR / MMW_PLUGINS_DIR /
    MMW_ACOUSTID_KEY。仅当配置表中该键当前为空（未设置）时写入——
    这样用户通过网页改过的值不会被覆盖；wizard/config 修改时由
    cmd/config_callback 先清空对应行，重启后按新向导值重新写入。
    """
    env_map = {
        "MMW_MUSIC_DIR": "music_dir",
        "MMW_PLUGINS_DIR": "plugins_dir",
        "MMW_ACOUSTID_KEY": "acoustid_key",
        "MMW_CACHE_DIR": "cache_dir",
    }
    try:
        from webapp import db
        db.init_db()
        with db.connect() as c:
            rows = {r["key"]: (r["value"] or "")
                    for r in c.execute("SELECT key, value FROM config").fetchall()}
        updates = {}
        for env, key in env_map.items():
            val = os.environ.get(env, "").strip()
            if val and not rows.get(key, "").strip():
                updates[key] = val
        if updates:
            db.set_config(updates)
            print(f"[run_server] 已应用安装向导配置: {sorted(updates)}", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[run_server] 应用安装向导配置失败: {exc}", flush=True)


def _stale_pids(sock_file: str) -> list:
    """找出持有同一 socket 的旧 run_server 实例（残留进程）。

    背景：fnOS 停止应用时只 kill runuser 包装进程（PID_FILE 记录的是它），
    实际 Python 进程会残留并持有已失效的 socket，导致每次重启都累积一个实例。
    本函数扫描 /proc 命令行，匹配 run_server.py + 同一 sock 路径的旧进程。
    """
    import glob
    pids = []
    for cfile in glob.glob("/proc/[0-9]*/cmdline"):
        try:
            with open(cfile, "rb") as fh:
                cmd = fh.read().replace(b"\x00", b" ").decode("utf-8", "replace")
        except OSError:
            continue
        if "run_server.py" in cmd and sock_file in cmd:
            try:
                pids.append(int(cfile.split("/")[2]))
            except (ValueError, IndexError):
                continue
    return pids


def main() -> None:
    import signal
    import time
    # 清理本应用残留的旧实例（历史多实例泄漏）。新实例与应用用户相同，
    # 可向旧实例发 SIGTERM；宽限 2 秒后仍未退出则强杀。
    stale = [p for p in _stale_pids(SOCK) if p != os.getpid()]
    for _pid in stale:
        try:
            os.kill(_pid, signal.SIGTERM)
            print(f"[run_server] 清理残留实例 pid={_pid}", flush=True)
        except OSError:
            pass
    if stale:
        time.sleep(2)
        for _pid in stale:
            try:
                os.kill(_pid, signal.SIGKILL)
            except OSError:
                pass
    # 首次启动：写入安装向导配置（音乐目录/插件目录/AcoustID Key）
    _ensure_multipart()
    _apply_wizard_env()
    # 清理残留 socket（上次未正常退出时）
    if os.path.exists(SOCK):
        try:
            os.unlink(SOCK)
        except OSError:
            pass
    # 空闲自动退出守护线程（daemon，随主进程结束）
    uvicorn.run("webapp.main:app", uds=SOCK, log_level="info")


if __name__ == "__main__":
    main()
