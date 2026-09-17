#!/usr/bin/env python3
"""verify-wheel-set.py 的自检：用合成轮子证明它真的能拦住「双架构缺一条腿」的情况。

全部数据本地伪造（zipfile 里塞一段合法 ELF 头），不联网、不下载任何依赖包。
用法：python3 scripts/tests/test_wheel_verify.py
"""
from __future__ import annotations

import shutil
import struct
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VERIFY = ROOT / "scripts" / "verify-wheel-set.py"
LOCK = ROOT / "app" / "server" / "requirements.lock.txt"

FAILED: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  ✓ " if cond else "  ✗ ") + msg)
    if not cond:
        FAILED.append(msg)


def elf_header(machine: int) -> bytes:
    """造一个足够以假乱真的 64 位小端 ELF 头（前 20 字节）。"""
    head = bytearray(b"\x7fELF\x02\x01\x01\x00" + b"\x00" * 8)
    head += struct.pack("<H", 2)          # e_type = ET_EXEC
    head += struct.pack("<H", machine)    # e_machine
    return bytes(head)


def make_wheel(path: Path, so_name: str | None = None, machine: int = 62) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("pkg/__init__.py", "")
        if so_name:
            zf.writestr(so_name, elf_header(machine))


def requirements() -> list[tuple[str, str]]:
    out = []
    for line in LOCK.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if "==" in line:
            n, v = line.split("==", 1)
            out.append((n.strip(), v.strip()))
    return out


def build_set(root: Path, *, include_aarch64: bool = True,
              wrong_elf_in_aarch64: bool = False) -> None:
    """按锁定清单造一整套轮子；pydantic_core 是唯一的编译扩展。"""
    for name, ver in requirements():
        dist = name.replace("-", "_")
        if dist == "pydantic_core":
            make_wheel(root / f"{dist}-{ver}-cp311-cp311-manylinux_2_17_x86_64."
                             f"manylinux2014_x86_64.whl",
                       so_name="pydantic_core/_pydantic_core.cpython-311-x86_64-linux-gnu.so",
                       machine=62)
            if include_aarch64:
                # 故意塞错架构的 .so，用来验证 ELF 核对是否真的在起作用
                machine = 62 if wrong_elf_in_aarch64 else 183
                make_wheel(root / f"{dist}-{ver}-cp311-cp311-manylinux_2_17_aarch64."
                                 f"manylinux2014_aarch64.whl",
                           so_name="pydantic_core/_pydantic_core.cpython-311-aarch64-linux-gnu.so",
                           machine=machine)
        else:
            make_wheel(root / f"{dist}-{ver}-py3-none-any.whl")


def run(d: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(VERIFY), str(d), "--lock", str(LOCK), *extra],
        capture_output=True, text=True,
    )


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="wheelverify-"))
    try:
        print("\n[1] 正常齐全的轮子集合（双架构都在，ELF 正确）")
        d1 = tmp / "ok"
        d1.mkdir()
        build_set(d1)
        r = run(d1)
        check(r.returncode == 0, "校验通过（returncode=0）")
        check("pydantic_core" in r.stdout, "输出里列出了 pydantic_core")
        check(r.stdout.count("✓ 校验通过") == 1, "打印了通过结论")

        print("\n[2] 缺少 aarch64 轮子（pip 自己不会报错的那种情况）")
        d2 = tmp / "no-arm"
        d2.mkdir()
        build_set(d2, include_aarch64=False)
        r = run(d2)
        check(r.returncode != 0, "校验失败（returncode≠0）")
        check("aarch64" in r.stdout and "没有可用轮子" in r.stdout,
              "明确指出 aarch64 上没有可用轮子")

        print("\n[3] aarch64 轮子里装的是 x86_64 的 .so（下错架构）")
        d3 = tmp / "wrong-elf"
        d3.mkdir()
        build_set(d3, wrong_elf_in_aarch64=True)
        r = run(d3)
        check(r.returncode != 0, "校验失败（returncode≠0）")
        check("ELF" in r.stdout, "明确指出 ELF 架构不符")

        print("\n[4] 混进源码包（离线安装时需要编译，必须拦住）")
        d4 = tmp / "sdist"
        d4.mkdir()
        build_set(d4)
        (d4 / "somepkg-1.0.tar.gz").write_bytes(b"not a wheel")
        r = run(d4)
        check(r.returncode != 0, "校验失败（returncode≠0）")
        check("非 wheel 文件" in r.stdout, "明确指出混入了非 wheel 文件")

        print("\n[5] 单独校验某一架构")
        d5 = tmp / "arm-only"
        d5.mkdir()
        build_set(d5)
        r = run(d5, "--arch", "aarch64")
        check(r.returncode == 0, "--arch aarch64 通过")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if FAILED:
        print(f"结果：{len(FAILED)} 项失败")
        for f in FAILED:
            print(f"   - {f}")
        return 1
    print("结果：全部通过（合成数据，未下载任何依赖包）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
