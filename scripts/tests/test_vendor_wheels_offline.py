#!/usr/bin/env python3
"""vendor-wheels.sh 的离线端到端自检：用本地伪造的「索引」跑完整流程。

为什么要有这个测试：
  `pip download` 一次给多个 `--platform` 时，对每个依赖只挑**一个**满足条件的轮子，
  同一个包的另一个架构变体不会被下载。也就是说「两种架构的轮子都进包」这件事
  并不会因为写了两个 --platform 就成立 —— 必须按架构分两次拉取。
  这个测试用合成轮子把这条行为钉死：
    * 正常情况：两种架构的编译扩展轮子都要落到输出目录，校验必须通过；
    * 索引里缺 aarch64 那份：脚本必须失败（而不是产出一个只能在 x86 上装的包）。

全部数据本地伪造、PIP_NO_INDEX=1，不联网、不下载任何真实依赖包。
用法：python3 scripts/tests/test_vendor_wheels_offline.py
"""
from __future__ import annotations

import os
import shutil
import struct
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VENDOR = ROOT / "scripts" / "vendor-wheels.sh"

FAILED: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  ✓ " if cond else "  ✗ ") + msg)
    if not cond:
        FAILED.append(msg)


def elf_header(machine: int) -> bytes:
    head = bytearray(b"\x7fELF\x02\x01\x01\x00" + b"\x00" * 8)
    head += struct.pack("<H", 2) + struct.pack("<H", machine)
    return bytes(head)


def make_wheel(path: Path, entries: dict[str, bytes | str],
               dist: str, version: str, tag: str) -> None:
    """造一个结构合法的 wheel（带 dist-info，pip 才认）。"""
    body = {
        f"{dist}/__init__.py": "VALUE = 1\n",
        f"{dist}-{version}.dist-info/METADATA":
            f"Metadata-Version: 2.1\nName: {dist}\nVersion: {version}\n",
        f"{dist}-{version}.dist-info/WHEEL":
            f"Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: "
            f"{'true' if 'any' in tag else 'false'}\nTag: {tag}\n",
        f"{dist}-{version}.dist-info/RECORD": "",
    }
    body.update(entries)
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in body.items():
            zf.writestr(name, data)


ARCH_TAGS = {
    "x86_64": ("manylinux_2_17_x86_64.manylinux2014_x86_64", 62),
    "aarch64": ("manylinux_2_17_aarch64.manylinux2014_aarch64", 183),
}


def build_index(idx: Path, *, with_aarch64: bool = True) -> None:
    """合成索引：一个纯 Python 包 + 一个带编译扩展的包（按需给两种架构）。"""
    make_wheel(idx / "demo_pure-1.0.0-py3-none-any.whl", {}, "demo_pure", "1.0.0",
               "py3-none-any")
    for arch, (plat, machine) in ARCH_TAGS.items():
        if arch == "aarch64" and not with_aarch64:
            continue
        so = f"demo_compiled/_core.cpython-311-{arch}-linux-gnu.so"
        make_wheel(idx / f"demo_compiled-1.0.0-cp311-cp311-{plat}.whl",
                   {so: elf_header(machine)}, "demo_compiled", "1.0.0",
                   f"cp311-cp311-{plat}")


def run_vendor(work: Path, idx: Path, out: Path) -> subprocess.CompletedProcess:
    lock = work / "lock.txt"
    lock.write_text("demo_pure==1.0.0\ndemo_compiled==1.0.0\n", encoding="utf-8")
    env = dict(os.environ,
               LOCK_FILE=str(lock),
               WHEELS_DIR=str(out),
               PIP_NO_INDEX="1",
               PIP_FIND_LINKS=str(idx))
    return subprocess.run(["bash", str(VENDOR)], capture_output=True, text=True,
                          env=env, cwd=str(ROOT))


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="vendorwheels-"))
    try:
        print("\n[1] 索引齐备：两种架构的编译扩展轮子都必须被拉下来")
        idx = tmp / "index"
        idx.mkdir()
        build_index(idx)
        out = tmp / "out"
        r = run_vendor(tmp, idx, out)
        names = sorted(p.name for p in out.glob("*.whl")) if out.is_dir() else []
        check(r.returncode == 0, "脚本成功结束（returncode=0）")
        check(any("x86_64" in n for n in names), f"拿到 x86_64 编译轮子（{len(names)} 个文件）")
        check(any("aarch64" in n for n in names),
              "拿到 aarch64 编译轮子（若只有 x86_64，说明又退回成一次 pip download 了）")
        check(sum(1 for n in names if n.endswith("py3-none-any.whl")) == 1,
              "纯 Python 轮子只保留一份（没被重复下载成两个文件）")
        if r.returncode != 0:
            print(r.stdout[-2000:], r.stderr[-2000:])

        print("\n[2] 索引里缺 aarch64 那份：必须失败，不能产出只能在 x86 上装的包")
        idx2 = tmp / "index-noarm"
        idx2.mkdir()
        build_index(idx2, with_aarch64=False)
        r2 = run_vendor(tmp, idx2, tmp / "out2")
        check(r2.returncode != 0, "脚本以失败结束（returncode≠0）")
        check("aarch64" in r2.stdout, "输出里点明了 aarch64 缺轮子")

        print("\n[3] 锁定文件不存在：必须失败")
        env = dict(os.environ, LOCK_FILE=str(tmp / "nope.txt"),
                   WHEELS_DIR=str(tmp / "out3"), PIP_NO_INDEX="1")
        r3 = subprocess.run(["bash", str(VENDOR)], capture_output=True, text=True,
                            env=env, cwd=str(ROOT))
        check(r3.returncode != 0, "脚本以失败结束（returncode≠0）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if FAILED:
        print(f"结果：{len(FAILED)} 项失败")
        for f in FAILED:
            print(f"   - {f}")
        return 1
    print("结果：全部通过（合成索引，未联网、未下载任何真实依赖包）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
