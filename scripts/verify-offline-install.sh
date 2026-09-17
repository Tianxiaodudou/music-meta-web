#!/usr/bin/env bash
# ============================================================
# 用包内轮子做「真·离线安装」验证（CI 里跑）
# ------------------------------------------------------------
# 三件事：
#   1) x86_64：完全按 cmd/main 的调用方式建 venv + --no-index 安装 + import，
#      证明客户端那条路径真的能在断网下跑通（不是只看轮子列表好不好看）；
#   2) 用这套内置依赖跑应用自己的测试，证明包里这套依赖足以支撑真实功能；
#   3) aarch64：用 --target 把 ARM 轮子解包出来，并核对 pydantic_core 的 .so
#      确实是 AArch64 的 ELF —— 交叉架构没法在本机执行，但架构对不对是可以验的。
# ============================================================
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WHEELS="${WHEELS_DIR:-$ROOT/app/server/vendor/wheels}"
LOCK="${LOCK_FILE:-$ROOT/app/server/requirements.lock.txt}"
PY="${PYTHON:-python3}"

[ -d "$WHEELS" ] || { echo "✗ 内置轮子目录不存在：$WHEELS" >&2; exit 1; }
[ -f "$LOCK" ] || { echo "✗ 锁定文件不存在：$LOCK" >&2; exit 1; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

echo "== 本机 Python: $("$PY" -V 2>&1)"
echo "== 轮子目录: $WHEELS（$(find "$WHEELS" -maxdepth 1 -name '*.whl' | wc -l) 个）"

echo
echo "== [1/2] x86_64：venv + --no-index 离线安装 + import"
"$PY" -m venv "$WORK/venv"
"$WORK/venv/bin/python" -m pip install \
  --no-index --find-links "$WHEELS" --no-cache-dir --disable-pip-version-check \
  -q -r "$LOCK"
"$WORK/venv/bin/python" - <<'PY'
# 与 cmd/main 的 DEPS_CHECK 保持一致，并额外把版本打出来存档
import fastapi, uvicorn, mutagen, multipart, pydantic, starlette, pydantic_core
print("   ✓ 关键模块导入成功")
for m in (fastapi, uvicorn, mutagen, pydantic, starlette, pydantic_core):
    print(f"     {m.__name__:<14} {getattr(m, '__version__', '?')}")
import multipart as _mp
print(f"     {'multipart':<14} {getattr(_mp, '__version__', '?')}")
PY
echo "   ✓ x86_64 离线安装与导入通过"

echo
echo "== [2/3] 用这套内置依赖跑应用自己的测试"
# 意义不只是「测试通过」：还证明包里这套依赖足以支撑应用真实功能
# （这些测试会真的写出 flac/ogg/mp3，再读原始字节核对标签键）
PYTHONPATH="$ROOT/app/server" "$WORK/venv/bin/python" "$ROOT/app/server/tests/test_tag_keys.py"
echo "   ✓ 应用测试通过（内置依赖集合够用）"

echo
echo "== [3/3] aarch64：解包 ARM 轮子并核对 ELF 架构"
"$PY" -m pip install \
  --no-index --find-links "$WHEELS" --only-binary=:all: \
  --platform manylinux_2_17_aarch64 --python-version 3.11 \
  --implementation cp --abi cp311 \
  --target "$WORK/arm" --no-cache-dir --disable-pip-version-check \
  -q -r "$LOCK"

"$PY" - "$WORK/arm" <<'PY'
import struct, sys
from pathlib import Path

root = Path(sys.argv[1])
elf_machine = {62: "x86_64", 183: "aarch64"}
sos = [p for p in root.rglob("*.so") if "pydantic_core" in p.name]
if not sos:
    print("   ✗ aarch64 目标目录里没有 pydantic_core 的 .so")
    sys.exit(1)
for so in sos:
    with open(so, "rb") as fh:
        blob = fh.read(20)
    if blob[:4] != b"\x7fELF":
        print(f"   ✗ {so.name} 不是 ELF")
        sys.exit(1)
    (machine,) = struct.unpack_from("<H", blob, 18)
    got = elf_machine.get(machine, f"machine={machine}")
    if got != "aarch64":
        print(f"   ✗ {so.name} 的 ELF 是 {got}，期望 aarch64")
        sys.exit(1)
    print(f"   ✓ {so.name}  ELF={got}")
PY
echo "   ✓ aarch64 轮子集合可解包且架构正确"

echo
echo "== 离线安装验证全部通过（未使用任何网络索引）"
