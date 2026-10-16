#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""校验内置 fpcalc 是否齐备、以及是不是「说好的那一份」。

为什么需要这个脚本：
  音频指纹依赖 app/server/vendor/bin/ 下的 fpcalc，它是 CI 构建时从
  GitHub 下载的**二进制**。二进制不像源码那样能靠肉眼 review，出错的方式
  还都很难在本地发现：
    * 下载被网络中间页截断 → 得到一个 html 小文件；
    * 只带了 x86_64，ARM 机器装完才炸；
    * 上游资产被替换 → 跑起来行为不明。
  所以这里做三件事：文件在不在、ELF 机器类型对不对、sha256 是不是标定的那份。

  同一个脚本既能校验构建目录（CI 里构建前），也能校验成品 fpk 解出来的载荷
  （verify-fpk-payload.sh 会再调一次）—— 只信成品，不信中间状态。

用法：
  scripts/verify-fpcalc-set.py [目录]        # 默认 app/server/vendor/bin
退出码：0 通过，1 有问题。
"""
from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

# ELF e_machine → 架构名（与轮子校验脚本保持一致的写法）
ELF_MACHINE = {0x3E: "x86_64", 0xB7: "aarch64"}

# 内置文件名 → (期望 ELF 架构, 二进制 sha256, 体积下限)
# sha256 由 scripts/vendor-fpcalc.sh 用同一份上游资产产生；上游改动即失败。
EXPECTED = {
    "fpcalc-x86_64": {
        "elf": "x86_64",
        "sha256": "4dc7e0bc8fff76d07b15fd124d1fe45ed6b08cdd4d9aa14c6948524d4d7a9d96",
        "min_size": 1 << 20,
        # x86_64 版为官方静态构建：不应有动态依赖
        "allow_needed": set(),
    },
    "fpcalc-aarch64": {
        "elf": "aarch64",
        "sha256": "3415a892be65246899bd574b848e86fea48bdb8719a7801a83e2d1835afe24f7",
        "min_size": 1 << 19,
        # 官方 aarch64 版是动态链接的，但只允许这几个系统库（fnOS/Debian 自带）
        "allow_needed": {
            "libm.so.6", "libstdc++.so.6", "libgcc_s.so.1", "libc.so.6",
            "ld-linux-aarch64.so.1",
        },
    },
}


def elf_machine_of(head: bytes) -> int | None:
    """从 ELF 头里取 e_machine（偏移 18，小端）。"""
    if len(head) < 20 or head[:4] != b"\x7fELF":
        return None
    return head[18] | (head[19] << 8)


def _read(path: Path, n: int = -1) -> bytes:
    with open(path, "rb") as fh:
        return fh.read(n)


def needed_libs(data: bytes) -> set[str]:
    """取出 DT_NEEDED 里的共享库名（解析 .dynamic 段）。

    静态可执行文件没有这一步，返回空集合。
    """
    import struct

    if len(data) < 64 or data[:4] != b"\x7fELF" or data[4] != 2 or data[5] != 1:
        return set()
    e_phoff = struct.unpack_from("<Q", data, 32)[0]
    e_phentsize = struct.unpack_from("<H", data, 54)[0]
    e_phnum = struct.unpack_from("<H", data, 56)[0]

    needed: set[str] = set()
    for i in range(e_phnum):
        off = e_phoff + i * e_phentsize
        p_type = struct.unpack_from("<I", data, off)[0]
        if p_type != 2:  # PT_DYNAMIC
            continue
        p_offset = struct.unpack_from("<Q", data, off + 8)[0]
        p_filesz = struct.unpack_from("<Q", data, off + 32)[0]
        # 先在段里找 DT_STRTAB/DT_STRSZ，再逐条 DT_NEEDED 取名字
        dyn = data[p_offset:p_offset + p_filesz]
        strtab_vaddr, strsz = 0, 0
        entries = []
        for j in range(0, len(dyn) - 15, 16):
            d_tag = struct.unpack_from("<Q", dyn, j)[0]
            d_val = struct.unpack_from("<Q", dyn, j + 8)[0]
            entries.append((d_tag, d_val))
            if d_tag == 5:      # DT_STRTAB
                strtab_vaddr = d_val
            elif d_tag == 10:   # DT_STRSZ
                strsz = d_val
        if not strtab_vaddr or not strsz:
            continue
        # 把虚地址换算成文件偏移（用 PT_LOAD 段表）
        file_off = None
        for k in range(e_phnum):
            o2 = e_phoff + k * e_phentsize
            if struct.unpack_from("<I", data, o2)[0] != 1:  # PT_LOAD
                continue
            p_off = struct.unpack_from("<Q", data, o2 + 8)[0]
            p_vaddr = struct.unpack_from("<Q", data, o2 + 16)[0]
            p_filesz2 = struct.unpack_from("<Q", data, o2 + 32)[0]
            if p_vaddr <= strtab_vaddr < p_vaddr + p_filesz2:
                file_off = p_off + (strtab_vaddr - p_vaddr)
                break
        if file_off is None:
            continue
        strtab = data[file_off:file_off + strsz]
        for d_tag, d_val in entries:
            if d_tag != 1:      # DT_NEEDED
                continue
            end = strtab.find(b"\0", d_val)
            if end > 0:
                needed.add(strtab[d_val:end].decode("utf-8", "replace"))
    return needed


def is_executable(path: Path) -> bool:
    """是否有执行权限。Windows 没有可执行位概念（os.access 恒真），按 POSIX 位判定。

    只把 +x 当作必要条件、不当作充分条件 —— 真正的可执行性还得靠机器架构核对。
    """
    try:
        mode = path.stat().st_mode
    except OSError:
        return False
    if os.name != "posix":
        return True          # Windows 上无意义，交给 ELF 架构核对兜底
    return bool(mode & 0o111)


def verify_dir(bin_dir: Path) -> int:
    errors: list[str] = []
    bin_dir = Path(bin_dir)
    print(f"== 校验内置 fpcalc：{bin_dir}")
    if not bin_dir.is_dir():
        print(f"✗ 目录不存在（CI 里应先跑 scripts/vendor-fpcalc.sh）")
        return 1

    for name, spec in EXPECTED.items():
        path = bin_dir / name
        if not path.is_file():
            errors.append(f"缺少 {name}")
            print(f"   ✗ {name}：文件不存在")
            continue
        data = _read(path)
        size = len(data)
        sha = hashlib.sha256(data).hexdigest()
        mode_ok = is_executable(path)
        machine = elf_machine_of(data)
        arch = ELF_MACHINE.get(machine, f"machine={machine}")

        problems: list[str] = []
        if size < spec["min_size"]:
            problems.append(f"体积只有 {size} 字节（小于下限 {spec['min_size']}，像是被截断/下载到了错误页）")
        if machine is None:
            problems.append("不是 ELF 文件")
        elif arch != spec["elf"]:
            problems.append(f"ELF 架构是 {arch}，但这台机器要 {spec['elf']}")
        if not mode_ok:
            problems.append("没有执行权限")
        if sha != spec["sha256"]:
            problems.append(f"sha256 不符（期望 {spec['sha256'][:16]}…）")

        libs = needed_libs(data)
        extra = libs - spec["allow_needed"]
        if extra:
            problems.append(f"多出未预期的动态依赖：{sorted(extra)}")

        if problems:
            errors.extend(f"{name}：{p}" for p in problems)
            print(f"   ✗ {name}  {size} 字节  ELF={arch}  +x={mode_ok}")
            for p in problems:
                print(f"       - {p}")
        else:
            note = "静态" if not libs else "依赖 " + " ".join(sorted(libs))
            print(f"   ✓ {name}  {size / 1048576:.2f} MiB  ELF={arch}  {note}")
        print(f"       sha256={sha}")

    if errors:
        print("\n✗ 内置 fpcalc 校验失败：")
        for e in errors:
            print(f"   - {e}")
        return 1
    print("\n✓ 内置 fpcalc 校验通过：x86_64 与 aarch64 齐备，且与标定版本一致")
    return 0


def main(argv: list[str]) -> int:
    target = Path(argv[1]) if len(argv) > 1 else Path("app/server/vendor/bin")
    return verify_dir(target)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
