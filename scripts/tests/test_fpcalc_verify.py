#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify-fpcalc-set.py 的自检：证明它真的能拦住「内置指纹工具不对」的情况。

fpcalc 是二进制，出错方式很隐蔽（下载到 html 错误页、只有一种架构、上游换文件），
所以这里用**合成文件**把这些情况逐个造出来，看校验脚本是否都能拦下：

  * 目录空 / 只有一个架构        → 必须失败
  * 文件是 html 错误页（截断）   → 必须失败
  * ELF 架构与文件名不符         → 必须失败
  * 内容对但 sha256 与标定不符   → 必须失败（合成文件天然命中这条）

运行环境里如果已有构建好的 app/server/vendor/bin/，再补一个真实目录的正例。
用法：python3 scripts/tests/test_fpcalc_verify.py
"""
from __future__ import annotations

import importlib.util
import os
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VERIFY = ROOT / "scripts" / "verify-fpcalc-set.py"
REAL_BIN = ROOT / "app" / "server" / "vendor" / "bin"

FAILED: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  ✓ " if cond else "  ✗ ") + msg)
    if not cond:
        FAILED.append(msg)


def load_verify():
    """按文件路径加载带连字符的脚本模块（不能直接 import）。"""
    spec = importlib.util.spec_from_file_location("verify_fpcalc_set", VERIFY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def fake_elf(machine: int, size: int) -> bytes:
    """造一个前 20 字节合法的 64 位小端 ELF 头，后面用 0 填充到指定体积。"""
    head = bytearray(b"\x7fELF\x02\x01\x01\x00" + b"\x00" * 8)
    head += struct.pack("<H", 2) + struct.pack("<H", machine)
    return bytes(head) + b"\x00" * max(0, size - len(head))


def run_verify(target: Path) -> tuple[int, str]:
    # 统一子进程输出编码：脚本里是中文提示，Windows 上用默认编码会炸
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    proc = subprocess.run([sys.executable, str(VERIFY), str(target)],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=env)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def main() -> int:
    mod = load_verify()
    X86, ARM = 0x3E, 0xB7

    print("== 解析器基本正确性")
    check(mod.elf_machine_of(fake_elf(X86, 64)) == X86, "x86_64 的 e_machine 认得出来")
    check(mod.elf_machine_of(fake_elf(ARM, 64)) == ARM, "aarch64 的 e_machine 认得出来")
    check(mod.elf_machine_of(b"<html>404</html>") is None, "html 不算 ELF")
    check(mod.needed_libs(fake_elf(X86, 4096)) == set(), "没有 PT_DYNAMIC 时依赖为空")

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)

        print("== 目录里什么都没有")
        rc, out = run_verify(tmp)
        check(rc == 1 and "缺少 fpcalc-x86_64" in out, "空目录必须失败并点名缺哪个")

        print("== 下载到 html 错误页（体积也不对）")
        (tmp / "fpcalc-x86_64").write_bytes(b"<html>Not Found</html>")
        rc, out = run_verify(tmp)
        check(rc == 1, "html 冒充 fpcalc 必须失败")
        check("不是 ELF 文件" in out, "报出「不是 ELF」")

        print("== 只有 x86_64、且架构与文件名不符")
        (tmp / "fpcalc-x86_64").write_bytes(fake_elf(ARM, 2 << 20))
        rc, out = run_verify(tmp)
        check(rc == 1, "只有一个架构必须失败")
        check("缺少 fpcalc-aarch64" in out, "点名缺 aarch64 那份")
        check("ELF 架构是 aarch64" in out, "点名架构不符（拿 aarch64 当 x86_64）")

        print("== 两个架构齐备但内容不是标定的那份")
        (tmp / "fpcalc-x86_64").write_bytes(fake_elf(X86, 2 << 20))
        (tmp / "fpcalc-aarch64").write_bytes(fake_elf(ARM, 1 << 20))
        for f in ("fpcalc-x86_64", "fpcalc-aarch64"):
            os.chmod(tmp / f, 0o755)
        rc, out = run_verify(tmp)
        check(rc == 1, "sha256 与标定不符必须失败")
        check(out.count("sha256 不符") >= 2, "两份都报出 sha256 不符")

        print("== 没有执行权限也要拦")
        if os.name == "posix":
            (tmp / "fpcalc-x86_64").chmod(0o644)
            rc, out = run_verify(tmp)
            check(rc == 1 and "没有执行权限" in out, "缺执行位必须失败")
        else:
            print("  （跳过：Windows 没有可执行位，这项只在 Linux 上成立）")

    print("== 真实构建目录（存在才测）")
    if (REAL_BIN / "fpcalc-x86_64").is_file():
        rc, out = run_verify(REAL_BIN)
        check(rc == 0, f"{REAL_BIN} 校验通过")
    else:
        print(f"  （跳过：{REAL_BIN} 里没有 fpcalc-x86_64）")

    print()
    if FAILED:
        print(f"✗ 未通过 {len(FAILED)} 项：")
        for f in FAILED:
            print(f"   - {f}")
        return 1
    print("✓ 全部通过：内置 fpcalc 校验脚本能拦住缺失/错架构/被截断/被掉包的情况")
    return 0


if __name__ == "__main__":
    sys.exit(main())
