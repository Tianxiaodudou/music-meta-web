#!/usr/bin/env bash
# ============================================================
# 把运行期依赖的 wheel 拉到 app/server/vendor/wheels/
# ------------------------------------------------------------
# 只在 CI 里跑（.github/workflows/build-release.yml）。
# 规则：本仓库不在本地打包、也不在本地下载依赖包；打包与依赖拉取都只发生在 GitHub。
#
# 目标环境（实测）：
#   应用运行在飞牛 python311 应用提供的 Python 3.11.5（回退系统 python3 也是 3.11）
#   → 因此按 cp311 拉轮子。
# 架构：
#   同一个 fpk 要同时装到 x86_64 与 aarch64 机器上，所以两种架构的轮子都拉，
#   纯 Python 轮子（py3-none-any）只拉一份，编译扩展（pydantic_core）两种各一份。
# ============================================================
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOCK="${LOCK_FILE:-$ROOT/app/server/requirements.lock.txt}"
OUT="${WHEELS_DIR:-$ROOT/app/server/vendor/wheels}"
PY_VER="${PY_VER:-3.11}"
ABI="${ABI:-cp311}"

if [ ! -f "$LOCK" ]; then
  echo "✗ 找不到依赖锁定文件：$LOCK" >&2
  exit 1
fi

# 依赖版本清单里不该有注释行以外的空行歧义，先做个健全性检查
grep -qE '^[a-zA-Z0-9_.-]+==' "$LOCK" || { echo "✗ $LOCK 里没有 == 锁定项" >&2; exit 1; }

echo "== 依赖锁定文件：$(basename "$LOCK")（$(grep -cE '^[a-zA-Z0-9_.-]+==' "$LOCK") 项）"
echo "== 目标 Python: $PY_VER / ABI: $ABI"
echo "== 输出目录: $OUT"

rm -rf "$OUT"
mkdir -p "$OUT"

# ⚠ 必须「按架构分别拉取」，不能把两个 --platform 塞进同一次 pip download：
#   pip 对每个依赖只挑**一个**满足条件的轮子，多平台时它挑中一个就收工，
#   于是同一个包的另一个架构变体不会被下载 —— 结果就是 ARM 机器上装不起来。
#   实测：`pip download --platform x86_64 --platform aarch64` 只拿到 x86_64 那份。
#   纯 Python 轮子（py3-none-any）两次都会下到同一个文件名，落到同目录即天然去重。

echo "== 开始下载轮子（只取二进制 wheel，禁止源码包）"
# 依赖来源可用环境变量覆盖（默认走 PyPI）：
#   PIP_INDEX_URL   换镜像源，例如 https://pypi.tuna.tsinghua.edu.cn/simple
#   PIP_FIND_LINKS  从本地目录或镜像取包（配合 PIP_NO_INDEX=1 即完全离线）
#   PIP_NO_INDEX=1  禁止访问索引
# 这几个开关也让本脚本能用本地伪造的轮子做离线自测（不联网）。
SRC_ARGS=()
[ -n "${PIP_INDEX_URL:-}" ] && SRC_ARGS+=(-i "$PIP_INDEX_URL")
[ -n "${PIP_FIND_LINKS:-}" ] && SRC_ARGS+=(--find-links "$PIP_FIND_LINKS")
[ -n "${PIP_NO_INDEX:-}" ] && SRC_ARGS+=(--no-index)
[ "${#SRC_ARGS[@]}" -gt 0 ] && echo "== 依赖来源覆盖：${SRC_ARGS[*]}"

for ARCH in x86_64 aarch64; do
  case "$ARCH" in
    # 两套 manylinux 别名都写上：pip 对 manylinux2014_* 与 manylinux_2_17_*
    # 的展开规则随版本变化，而 pydantic_core 的轮子两个 tag 都带
    x86_64)  PLATFORMS="manylinux_2_17_x86_64 manylinux2014_x86_64" ;;
    aarch64) PLATFORMS="manylinux_2_17_aarch64 manylinux2014_aarch64" ;;
  esac
  PLATFORM_ARGS=()
  for p in $PLATFORMS; do PLATFORM_ARGS+=(--platform "$p"); done

  echo
  echo "== [$ARCH] 拉取该架构的轮子（平台标签：${PLATFORMS// /, }）"
  python3 -m pip download \
    --only-binary=:all: \
    --python-version "$PY_VER" \
    --implementation cp \
    --abi "$ABI" \
    "${PLATFORM_ARGS[@]}" \
    ${SRC_ARGS[@]+"${SRC_ARGS[@]}"} \
    --dest "$OUT" \
    --disable-pip-version-check \
    -r "$LOCK"
done

COUNT="$(find "$OUT" -maxdepth 1 -name '*.whl' | wc -l)"
echo
echo "== 下载完成：$COUNT 个轮子"
ls -l "$OUT"

if [ "$COUNT" -eq 0 ]; then
  echo "✗ 一个轮子都没下到" >&2
  exit 1
fi

# 双架构完整性 + ELF 机器类型核对（这一步是「一个包能装两种架构」的保证）
python3 "$ROOT/scripts/verify-wheel-set.py" "$OUT" --lock "$LOCK"

echo "== 内置轮子已就绪：$OUT"
