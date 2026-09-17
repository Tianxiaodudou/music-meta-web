#!/usr/bin/env python3
"""校验内置轮子集合能否同时满足 x86_64 与 aarch64 两种架构的离线安装。

为什么需要这个脚本：
  `pip download --platform A --platform B` 只要**任意一个**平台能满足就会把包下下来，
  所以「某个包只有 x86_64 轮子、没有 aarch64 轮子」这种情况 pip 不会报错，
  结果就是 fpk 装到 ARM 机器上才炸。这里逐个依赖、逐个架构做可满足性判定，
  把问题拦在 CI 里。

同时核对编译扩展的 ELF 机器类型（e_machine），确保标着 aarch64 的轮子里
装的确实是 AArch64 的 .so，而不是下错了架构。

用法：
  verify-wheel-set.py <wheels-dir> [--lock requirements.lock.txt]
                       [--arch x86_64 --arch aarch64] [--python-tag cp311]
"""
from __future__ import annotations

import argparse
import struct
import sys
import zipfile
from pathlib import Path

# ELF e_machine 取值（见 /usr/include/elf.h）
ELF_MACHINE = {62: "x86_64", 183: "aarch64", 40: "armv7l", 243: "riscv64"}
ARCH_ALIASES = {
    "x86_64": ("x86_64", "amd64"),
    "aarch64": ("aarch64", "arm64"),
}


def parse_wheel_name(filename: str) -> dict | None:
    """解析 wheel 文件名 → {name, version, pythons, abis, platforms}（标签为压缩集合展开后的列表）。"""
    if not filename.endswith(".whl"):
        return None
    parts = filename[: -len(".whl")].split("-")
    if len(parts) < 5:
        return None
    # {name}-{version}[-{build}]-{python}-{abi}-{platform}
    name, version = parts[0], parts[1]
    py_tag, abi_tag, plat_tag = parts[-3], parts[-2], parts[-1]
    return {
        "name": name,
        "version": version,
        "pythons": py_tag.split("."),
        "abis": abi_tag.split("."),
        "platforms": plat_tag.split("."),
        "file": filename,
    }


def norm(name: str) -> str:
    """PEP 503 规范化：python-multipart / python_multipart 视为同一个包。"""
    return name.lower().replace("-", "_").replace(".", "_")


def platform_ok(platforms: list[str], arch: str) -> bool:
    """该 wheel 的平台标签是否覆盖目标架构（含 pure python 的 any）。"""
    if "any" in platforms:
        return True
    aliases = ARCH_ALIASES.get(arch, (arch,))
    for p in platforms:
        if p in ("any", "none"):
            return True
        if any(a in p for a in aliases):
            return True
    return False


def python_ok(pythons: list[str], abi: str, target: str) -> bool:
    """Python/ABI 标签是否适用于目标解释器（默认 CPython 3.11）。"""
    short = target.replace("cp", "")          # cp311 -> 311
    accepted = {f"cp{short}", f"py{short}", "py3"}
    if any(t in accepted for t in pythons):
        return True
    # ABI 是 none/abi3 的纯实现无关轮子，只要 python 标签过得去就算过
    return False


def elf_machine_of(blob: bytes) -> int | None:
    """从 ELF 头部读 e_machine。"""
    if len(blob) < 20 or blob[:4] != b"\x7fELF":
        return None
    endian = "<" if blob[5] == 1 else ">"
    (machine,) = struct.unpack_from(endian + "H", blob, 18)
    return machine


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("wheels_dir", type=Path)
    ap.add_argument("--lock", type=Path, default=None, help="requirements.lock.txt")
    ap.add_argument("--arch", action="append", default=None)
    ap.add_argument("--python-tag", default="cp311")
    args = ap.parse_args()

    archs = args.arch or ["x86_64", "aarch64"]
    wheels_dir: Path = args.wheels_dir
    errors: list[str] = []
    notes: list[str] = []

    files = sorted(p for p in wheels_dir.iterdir() if p.is_file())
    if not files:
        print(f"✗ {wheels_dir} 为空")
        return 1

    # 1) 只允许 wheel，不许源码包混进来（否则目标机离线安装时需要编译）
    non_wheel = [f.name for f in files if not f.name.endswith(".whl")]
    if non_wheel:
        errors.append(f"目录里出现了非 wheel 文件（离线安装时无法处理）：{non_wheel}")

    parsed = [w for w in (parse_wheel_name(f.name) for f in files) if w]
    print(f"== 轮子总数：{len(parsed)}（目录 {wheels_dir}）")

    # 2) 依赖锁定项 → 每个架构都必须有可用轮子
    requirements: list[str] = []
    if args.lock and args.lock.is_file():
        for line in args.lock.read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if "==" in line:
                requirements.append(norm(line.split("==", 1)[0]))
    if not requirements:
        notes.append("未提供 --lock，跳过「按锁定清单逐项核对」，只做整体统计")
        requirements = sorted({norm(w["name"]) for w in parsed})

    by_name: dict[str, list[dict]] = {}
    for w in parsed:
        by_name.setdefault(norm(w["name"]), []).append(w)

    print("\n== 逐依赖 × 逐架构可满足性")
    header = f"   {'依赖':<22}{'版本':<12}" + "".join(f"{a:<12}" for a in archs) + "说明"
    print(header)
    print("   " + "-" * (len(header) - 3))

    for req in requirements:
        cands = by_name.get(req, [])
        if not cands:
            errors.append(f"锁定清单里的 {req} 没有任何轮子")
            print(f"   {req:<22}{'—':<12}" + "".join(f"{'缺失':<12}" for _ in archs) + "✗ 完全没有轮子")
            continue
        versions = sorted({c["version"] for c in cands})
        ver = versions[0] if len(versions) == 1 else "/".join(versions)
        cells = []
        for arch in archs:
            hit = [
                c for c in cands
                if platform_ok(c["platforms"], arch)
                and python_ok(c["pythons"], c["abis"][0], args.python_tag)
            ]
            if hit:
                cells.append("✓")
            else:
                cells.append("✗")
                errors.append(f"{req} 在 {arch} 上没有可用轮子（只有 {[c['file'] for c in cands]}）")
        kind = "纯 Python" if all("any" in c["platforms"] for c in cands) else "含编译扩展"
        print(f"   {req:<22}{ver:<12}" + "".join(f"{c:<12}" for c in cells) + kind)

    # 3) 编译扩展的 ELF 机器类型核对
    print("\n== 编译扩展的 ELF 架构核对")
    compiled = [w for w in parsed if "any" not in w["platforms"]]
    if not compiled:
        notes.append("没有编译扩展轮子（全部为纯 Python），ELF 核对跳过")
        print("   （无）")
    for w in compiled:
        expect = None
        for arch in archs:
            if platform_ok(w["platforms"], arch):
                expect = arch
                break
        seen: list[tuple[str, int | None]] = []
        with zipfile.ZipFile(wheels_dir / w["file"]) as zf:
            for info in zf.infolist():
                if info.filename.endswith(".so"):
                    with zf.open(info) as fh:
                        seen.append((info.filename, elf_machine_of(fh.read(20))))
        if not seen:
            notes.append(f"{w['file']} 没有 .so（可能只是标记了平台标签）")
            print(f"   {w['file']}: 无 .so")
            continue
        for so_name, machine in seen:
            got = ELF_MACHINE.get(machine, f"machine={machine}")
            if expect and got != expect:
                errors.append(f"{w['file']} 内 {so_name} 的 ELF 是 {got}，但平台标签声称 {expect}")
                print(f"   ✗ {w['file']}\n       {so_name}: ELF={got} 期望={expect}")
            else:
                print(f"   ✓ {w['file']}\n       {Path(so_name).name}: ELF={got}")

    # 4) 汇总
    total = sum(f.stat().st_size for f in files)
    print(f"\n== 体积：{total / 1048576:.2f} MiB（{len(files)} 个文件）")
    for n in notes:
        print(f"== 提示：{n}")

    if errors:
        print("\n✗ 校验失败：")
        for e in errors:
            print(f"   - {e}")
        return 1
    print("\n✓ 校验通过：内置轮子可同时满足 " + " / ".join(archs) + " 的离线安装")
    return 0


if __name__ == "__main__":
    sys.exit(main())
