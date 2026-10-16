# -*- coding: utf-8 -*-
"""音频指纹识别：Chromaprint(fpcalc) 计算指纹 + AcoustID 服务识别歌曲。

用途：文件名匹配失败/不准确时，用音频内容本身识别真实歌曲
（歌名/歌手/专辑），再交给 QQ 音乐源回查封面/年份/歌词元数据。

需要：
- fpcalc 可执行文件（chromaprint 工具）。**安装包已内置**两套官方静态版本
  （`server/vendor/bin/fpcalc-x86_64` / `fpcalc-aarch64`，由 scripts/vendor-fpcalc.sh
  在 CI 构建时拉取），所以 NAS 上不需要用户自己 apt 装；
  万一内置文件缺失，再按顺序回退：显式参数 → `$FPCALC` → PATH → 常见系统路径。
- AcoustID API key（免费注册：https://acoustid.org/new-application）
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import urllib.parse
import urllib.request
from typing import List, Optional, Tuple

ACOUSTID_URL = "https://api.acoustid.org/v2/lookup"
# 保留旧常量：历史上调用方把它当默认值传入（见 resolve_fpcalc 的兼容处理）
FPCALC_DEFAULT = "/usr/bin/fpcalc"

# 包内置 fpcalc 目录：<app>/server/vendor/bin/
# 本文件位于 <app>/server/musicmeta/sources/ 下，往上三级才是 <app>/server。
_HERE = os.path.dirname(os.path.abspath(__file__))          # .../server/musicmeta/sources
_SERVER_DIR = os.path.dirname(os.path.dirname(_HERE))       # .../server
VENDOR_BIN_DIR = os.path.join(_SERVER_DIR, "vendor", "bin")

# 兜底的系统路径（PATH 里找不到时再看一眼常见位置）
_SYSTEM_FALLBACKS = (
    "/usr/bin/fpcalc",
    "/usr/local/bin/fpcalc",
    "/bin/fpcalc",
    "/opt/homebrew/bin/fpcalc",
    "/usr/local/opt/chromaprint/bin/fpcalc",
)


def _arch_tags() -> List[str]:
    """本机架构对应的内置文件名后缀（多写几个别名，兼容不同平台的 machine()）。"""
    m = (platform.machine() or "").lower()
    if m in ("x86_64", "amd64"):
        return ["x86_64", "amd64"]
    if m in ("aarch64", "arm64"):
        return ["aarch64", "arm64"]
    return [m] if m else []


def fpcalc_candidates(fpcalc: Optional[str] = None) -> List[str]:
    """按优先级列出所有候选 fpcalc 路径（只列表，不检查是否存在）。"""
    cands: List[str] = []
    if fpcalc and fpcalc != FPCALC_DEFAULT:
        cands.append(fpcalc)
    env = (os.environ.get("FPCALC") or "").strip()
    if env:
        cands.append(env)
    for tag in _arch_tags():
        cands.append(os.path.join(VENDOR_BIN_DIR, "fpcalc-" + tag))
    cands.append(os.path.join(VENDOR_BIN_DIR, "fpcalc"))
    found = shutil.which("fpcalc")
    if found:
        cands.append(found)
    cands.extend(_SYSTEM_FALLBACKS)
    # 去重且保持顺序
    out, seen = [], set()
    for c in cands:
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _usable(path: str) -> bool:
    """可执行且不是空文件；顺手补一次执行位（fnpack 打包可能丢 mode）。"""
    try:
        if not os.path.isfile(path) or os.path.getsize(path) == 0:
            return False
        if not os.access(path, os.X_OK):
            try:
                os.chmod(path, 0o755)
            except OSError:
                pass
        return os.access(path, os.X_OK)
    except OSError:
        return False


def resolve_fpcalc(fpcalc: Optional[str] = None) -> str:
    """定位可用的 fpcalc，返回其路径；一个都找不到则抛 RuntimeError。"""
    tried = fpcalc_candidates(fpcalc)
    for cand in tried:
        if _usable(cand):
            return cand
    raise RuntimeError(
        "未找到可用的 fpcalc（音频指纹需要它）。已尝试："
        + "、".join(tried)
        + "；可设环境变量 FPCALC 指定路径，或在系统上安装 libchromaprint-tools。")


def recognize(path: str, api_key: str,
              fpcalc: Optional[str] = FPCALC_DEFAULT) -> List[dict]:
    """一步完成：计算指纹 → AcoustID 识别 → 返回歌曲候选列表。

    数据源插件（qqmusic 的指纹模式）通过 musicmeta.fingerprint 调用它，
    不要删除。`fpcalc` 留空/用默认值时自动定位（优先用包内置的那份）。
    """
    duration, fp = fingerprint_file(path, fpcalc)
    return lookup_acoustid(fp, duration, api_key)


def fingerprint_file(path: str, fpcalc: Optional[str] = FPCALC_DEFAULT,
                     timeout: int = 180) -> Tuple[float, str]:
    """计算音频指纹，返回 (时长秒, fingerprint)。

    `fpcalc` 为空或等于历史默认值 `/usr/bin/fpcalc` 时走自动定位，
    这样旧插件（写死默认参数）也能用上包内置的可执行文件。
    """
    exe = resolve_fpcalc(fpcalc)
    proc = subprocess.run([exe, "-json", path],
                          capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"fpcalc 失败: {(proc.stderr or proc.stdout)[:200]}")
    data = json.loads(proc.stdout)
    return float(data["duration"]), data["fingerprint"]


def lookup_acoustid(fingerprint: str, duration: float, api_key: str,
                    timeout: int = 25) -> List[dict]:
    """提交指纹到 AcoustID，返回录音候选 [{title, artists, album, score}]。

    只取歌名/歌手/专辑用于回查 QQ；年份由 QQ 专辑详情提供更准确。
    """
    params = {
        "client": api_key,
        "duration": int(round(duration)),
        "fingerprint": fingerprint,
        "meta": "recordings+releasegroups",
        "format": "json",
    }
    url = ACOUSTID_URL + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url, headers={"User-Agent": "music-meta-web/1.0 (audio fingerprint)"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8", "replace"))

    if data.get("status") != "ok":
        err = data.get("error") or {}
        raise RuntimeError(
            f"AcoustID 查询失败: {err.get('message') or data.get('status')}")

    results: List[dict] = []
    seen: set = set()
    for result in data.get("results", []):
        for rec in result.get("recordings", []):
            title = (rec.get("title") or "").strip()
            artists = [a.get("name", "") for a in rec.get("artists", [])]
            artist = " / ".join(a for a in artists if a)
            if not title or not artist:
                continue
            rgs = rec.get("releasegroups", [])
            album = rgs[0].get("title", "") if rgs else ""
            key = (title.lower(), artist.lower())
            if key in seen:
                continue
            seen.add(key)
            results.append({
                "title": title,
                "artist": artist,
                "album": album,
                "score": float(result.get("score", 0)),
            })
    # 按匹配分数降序
    results.sort(key=lambda r: r["score"], reverse=True)
    return results
