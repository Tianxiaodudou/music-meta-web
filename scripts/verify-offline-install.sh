#!/usr/bin/env bash
# ============================================================
# 用包内轮子做「真·离线安装」验证（CI 里跑）
# ------------------------------------------------------------
# 分三段，可单独执行（CI 里拆成三个步骤跑，失败时看步骤名就知道是哪一段）：
#   install   x86_64：完全按 cmd/main 的调用方式建 venv + --no-index 安装 + import
#   apptests  用这套内置依赖跑应用自己的测试（需要 ffmpeg）
#   arm       aarch64：用 --target 解包 ARM 轮子，并核对 pydantic_core 的 .so 是 AArch64
#   all       三段依次跑（默认，本地用）
#
# 为什么拆开：CI 日志要仓库管理员权限才能看，失败时「步骤名」是我唯一能拿到的线索，
# 一段一步就能直接定位，不必靠猜。
#
# 用法：verify-offline-install.sh [install|apptests|arm|all]
# ============================================================
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WHEELS="${WHEELS_DIR:-$ROOT/app/server/vendor/wheels}"
LOCK="${LOCK_FILE:-$ROOT/app/server/requirements.lock.txt}"
PY="${PYTHON:-python3}"
# 固定路径：CI 里 install 与 apptests 是两个步骤、两个进程，venv 要能跨步骤用
VENV="${OFFLINE_VENV:-$ROOT/.tmp/offline-venv}"
PART="${1:-all}"

need_wheels() {
  [ -d "$WHEELS" ] || { echo "✗ 内置轮子目录不存在：$WHEELS" >&2; exit 1; }
  [ -f "$LOCK" ] || { echo "✗ 锁定文件不存在：$LOCK" >&2; exit 1; }
}

part_install() {
  need_wheels
  echo "== 本机 Python: $("$PY" -V 2>&1)"
  echo "== 轮子目录: $WHEELS（$(find "$WHEELS" -maxdepth 1 -name '*.whl' | wc -l) 个）"
  echo "== venv: $VENV"
  rm -rf "$VENV"
  mkdir -p "$(dirname "$VENV")"
  "$PY" -m venv "$VENV"
  echo "== pip install --no-index（完全断网，只用包内轮子）"
  "$VENV/bin/python" -m pip install \
    --no-index --find-links "$WHEELS" --no-cache-dir --disable-pip-version-check \
    -r "$LOCK"
  # 与 cmd/main 的 DEPS_CHECK 保持一致，并额外把版本打出来存档
  "$VENV/bin/python" - <<'PY'
import fastapi, uvicorn, mutagen, multipart, pydantic, starlette, pydantic_core
print("   ✓ 关键模块导入成功")
for m in (fastapi, uvicorn, mutagen, pydantic, starlette, pydantic_core):
    print(f"     {m.__name__:<14} {getattr(m, '__version__', '?')}")
import multipart as _mp
print(f"     {'multipart':<14} {getattr(_mp, '__version__', '?')}")
PY
  echo "== ✓ x86_64 离线安装与导入通过"
}

part_apptests() {
  [ -x "$VENV/bin/python" ] || {
    echo "✗ 找不到离线 venv：$VENV（请先跑 install 段）" >&2; exit 1; }
  # 这些测试会真的用 ffmpeg 造样本音频；缺 ffmpeg 要明确报错，不能悄悄跳过
  if ! command -v ffmpeg >/dev/null 2>&1; then
    echo "✗ 测试需要 ffmpeg 造样本音频，但当前环境没有 ffmpeg" >&2
    echo "  CI 里请先 apt-get install -y ffmpeg；本地请装上 ffmpeg 后重跑" >&2
    exit 1
  fi
  echo "== ffmpeg: $(command -v ffmpeg)（ffprobe: $(command -v ffprobe || echo 缺失)）"
  # 意义不只是「测试通过」：还证明包里这套依赖足以支撑应用真实功能
  # （这些测试会真的写出 flac/ogg/mp3，再读原始字节核对标签键）
  PYTHONPATH="$ROOT/app/server" "$VENV/bin/python" "$ROOT/app/server/tests/test_tag_keys.py"
  echo "== ✓ 应用测试通过（内置依赖集合够用）"
}

part_arm() {
  need_wheels
  local target="$ROOT/.tmp/arm-target"
  rm -rf "$target"; mkdir -p "$target"
  # 用 venv 里的 pip：不依赖 runner 的系统/工具缓存 pip 是否被标成 externally-managed
  local PIP_PY="$PY"
  [ -x "$VENV/bin/python" ] && PIP_PY="$VENV/bin/python"
  echo "== 用 $("$PIP_PY" -V 2>&1) 解包 aarch64 轮子到 $target"
  "$PIP_PY" -m pip install \
    --no-index --find-links "$WHEELS" --only-binary=:all: \
    --platform manylinux_2_17_aarch64 --python-version 3.11 \
    --implementation cp --abi cp311 \
    --target "$target" --no-cache-dir --disable-pip-version-check \
    -r "$LOCK"
  "$PIP_PY" - "$target" <<'PY'
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
        print(f"   ✗ {so.name} 不是 ELF"); sys.exit(1)
    (machine,) = struct.unpack_from("<H", blob, 18)
    got = elf_machine.get(machine, f"machine={machine}")
    if got != "aarch64":
        print(f"   ✗ {so.name} 的 ELF 是 {got}，期望 aarch64"); sys.exit(1)
    print(f"   ✓ {so.name}  ELF={got}")
PY
  echo "== ✓ aarch64 轮子集合可解包且架构正确"
}

case "$PART" in
  install)  part_install ;;
  apptests) part_apptests ;;
  arm)      part_arm ;;
  all)      part_install; echo; part_apptests; echo; part_arm
            echo; echo "== 离线安装验证全部通过（未使用任何网络索引）" ;;
  *) echo "用法: $0 [install|apptests|arm|all]" >&2; exit 2 ;;
esac
