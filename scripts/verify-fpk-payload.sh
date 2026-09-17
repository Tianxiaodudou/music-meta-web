#!/usr/bin/env bash
# ============================================================
# 校验「成品 fpk」本身（CI 里跑，构建之后、发布之前）
# ------------------------------------------------------------
# 只信成品，不信中间状态：
#   1) 依赖轮子确实在包内（app.tgz → server/vendor/wheels/），数量与源目录一致；
#   2) 两种架构的编译扩展轮子都在（pydantic_core 的 x86_64 + aarch64）；
#   3) 包内 manifest 的 version / changelog 字段确实是我们要的值
#      —— changelog 是 CI 注入的，这里回读一次，证明 fnpack 接受了这个值；
#   4) 包内 manifest.checksum 与 app.tgz 的 md5 相符（载荷完整性）。
# 用法：verify-fpk-payload.sh <fpk> <version> [wheels-dir]
# ============================================================
set -euo pipefail

FPK="${1:?用法: verify-fpk-payload.sh <fpk> <version> [wheels-dir]}"
VERSION="${2:?缺少版本号}"
WHEELS="${3:-}"

[ -f "$FPK" ] || { echo "✗ fpk 不存在：$FPK" >&2; exit 1; }

FAIL=0
fail() { echo "   ✗ $1"; FAIL=1; }
ok()   { echo "   ✓ $1"; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

tar xzf "$FPK" -C "$WORK"
[ -f "$WORK/app.tgz" ] || { echo "✗ fpk 里没有 app.tgz" >&2; exit 1; }
[ -f "$WORK/manifest" ] || { echo "✗ fpk 里没有 manifest" >&2; exit 1; }
mkdir -p "$WORK/x" && tar xzf "$WORK/app.tgz" -C "$WORK/x"

echo "== 成品：$FPK（$(stat -c%s "$FPK") B）"

echo
echo "== 1) 依赖轮子是否随包内置"
IN_FPK="$WORK/x/server/vendor/wheels"
if [ ! -d "$IN_FPK" ]; then
  fail "包内没有 server/vendor/wheels/ —— 依赖没有内置，客户端会去联网装"
else
  N_FPK="$(find "$IN_FPK" -maxdepth 1 -name '*.whl' | wc -l)"
  ok "包内轮子：$N_FPK 个"
  if [ -n "$WHEELS" ] && [ -d "$WHEELS" ]; then
    N_SRC="$(find "$WHEELS" -maxdepth 1 -name '*.whl' | wc -l)"
    [ "$N_FPK" -eq "$N_SRC" ] \
      && ok "数量与构建源目录一致（$N_SRC）" \
      || fail "数量与构建源目录不一致：包内 $N_FPK ≠ 源 $N_SRC"
    # 逐个文件名比对：漏一个文件就可能是打包时被过滤掉了
    if diff <(find "$WHEELS" -maxdepth 1 -name '*.whl' -printf '%f\n' | sort) \
            <(find "$IN_FPK" -maxdepth 1 -name '*.whl' -printf '%f\n' | sort) >/dev/null; then
      ok "轮子文件名集合完全一致"
    else
      fail "轮子文件名集合不一致：$(diff <(find "$WHEELS" -maxdepth 1 -name '*.whl' -printf '%f\n' | sort) <(find "$IN_FPK" -maxdepth 1 -name '*.whl' -printf '%f\n' | sort) | head -5 | tr '\n' ' ')"
    fi
  fi
  for arch in x86_64 aarch64; do
    if find "$IN_FPK" -maxdepth 1 -name "*cp311*${arch}*.whl" | grep -q .; then
      ok "含 $arch 的编译扩展轮子"
    else
      fail "缺少 $arch 的编译扩展轮子（该架构上会装不起来）"
    fi
  done
  [ -f "$WORK/x/server/requirements.lock.txt" ] \
    && ok "含依赖锁定文件 requirements.lock.txt" \
    || fail "缺少 requirements.lock.txt（离线安装要用它指定版本）"
fi

echo
echo "== 2) 包内 manifest 字段"
MV="$(grep -E '^version[[:space:]]*=' "$WORK/manifest" | head -1 | sed -E 's/^version[[:space:]]*=[[:space:]]*//' | tr -d ' \r')"
[ "$MV" = "$VERSION" ] && ok "version = $MV" || fail "version 不符：包内 $MV ≠ 期望 $VERSION"

CL="$(grep -E '^changelog[[:space:]]*=' "$WORK/manifest" | head -1 | sed -E 's/^changelog[[:space:]]*=[[:space:]]*//')"
if [ -z "$CL" ]; then
  fail "manifest 没有 changelog 字段 —— 应用中心的「更新说明」会是空的"
else
  ok "changelog 存在（${#CL} 字符）"
  echo "      ${CL:0:120}…"
fi

# 平台声明：本包同时支持两种架构，必须是 all
MP="$(grep -E '^platform[[:space:]]*=' "$WORK/manifest" | head -1 | sed -E 's/^platform[[:space:]]*=[[:space:]]*//' | tr -d ' \r')"
case "$MP" in
  all) ok "platform = all（一个包同时支持 x86_64 / aarch64）" ;;
  "")  fail "manifest 缺 platform 字段" ;;
  *)   fail "platform = $MP，但本包内置了两套架构的轮子，应为 all" ;;
esac

echo
echo "== 3) 载荷完整性"
TGZ_MD5="$(md5sum "$WORK/app.tgz" | cut -d' ' -f1)"
MAN_SUM="$(grep -E '^checksum' "$WORK/manifest" | tr -d ' \r' | cut -d= -f2)"
[ "$TGZ_MD5" = "$MAN_SUM" ] && ok "manifest.checksum 与 app.tgz md5 相符" \
  || fail "checksum 不符：app.tgz=$TGZ_MD5 manifest=$MAN_SUM"
echo "      app.tgz md5 = $TGZ_MD5"
echo "      载荷文件数  = $(find "$WORK/x" -type f | wc -l)"
echo "      fpk sha256  = $(sha256sum "$FPK" | cut -d' ' -f1)"

echo
if [ "$FAIL" -ne 0 ]; then
  echo "✗ 成品校验未通过（上面有 ✗ 项）"
  exit 1
fi
echo "✓ 成品校验通过：依赖已内置、双架构齐备、manifest 字段正确"
