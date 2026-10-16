#!/usr/bin/env bash
# ============================================================
# 把 fpcalc（Chromaprint 官方发布的静态可执行文件）拉进 app/server/vendor/bin/
# ------------------------------------------------------------
# 为什么需要：
#   音频指纹模式（文件名匹配不准时，用音频内容去 AcoustID 认歌）必须调用
#   fpcalc。以前靠用户在 NAS 上自己 `apt install libchromaprint-tools`，
#   装不上就整个模式报「fpcalc 失败」。这里在 CI 构建时把官方静态版直接
#   打进安装包 —— 装完就能用，不依赖系统里有没有这个命令。
#
#   两个架构各一份：
#     x86_64  ：官方静态构建（无任何动态依赖）
#     aarch64 ：官方构建，只依赖 glibc / libstdc++ / libgcc_s（fnOS 自带）
#
# 产物（不进版本库，见 .gitignore）：
#   app/server/vendor/bin/fpcalc-x86_64
#   app/server/vendor/bin/fpcalc-aarch64
#
# 版本号与 SHA256 写死在下面：上游资产被换掉/被篡改时构建直接失败，
# 保证打出来的包和这里标定的是同一份东西。
#
# 用法：scripts/vendor-fpcalc.sh [目标目录]     （默认 app/server/vendor/bin）
# ============================================================
set -euo pipefail

FPCALC_VERSION="1.6.2"
BASE_URL="https://github.com/acoustid/chromaprint/releases/download/v${FPCALC_VERSION}"
# 拿不到 github 时用镜像前缀重试（可留空）
MIRROR="${FPCALC_MIRROR:-}"

DEST="${1:-app/server/vendor/bin}"

# 架构 → "资产文件名 sha256"
#   chromaprint-fpcalc-1.6.2-linux-x86_64.tar.gz  内层路径 chromaprint-fpcalc-1.6.2-linux-x86_64/fpcalc
#   chromaprint-fpcalc-1.6.2-linux-arm64.tar.gz   内层路径 chromaprint-fpcalc-1.6.2-linux-arm64/fpcalc
asset_name() {
    case "$1" in
        x86_64)  echo "chromaprint-fpcalc-${FPCALC_VERSION}-linux-x86_64.tar.gz" ;;
        aarch64) echo "chromaprint-fpcalc-${FPCALC_VERSION}-linux-arm64.tar.gz" ;;
        *) echo "未知架构：$1" >&2; return 1 ;;
    esac
}
asset_sha() {
    case "$1" in
        x86_64)  echo "c968ad4a0821592a79b158fb2e1f46cbe0a22a0a1dbc0743c67d68e75d15d2bb" ;;
        aarch64) echo "3aa1498126ed272d805bc9d081c77965c9e1e1a0891bb112c13b56ef2ad47bad" ;;
        *) return 1 ;;
    esac
}

mkdir -p "$DEST"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

for arch in x86_64 aarch64; do
    tarball="$(asset_name "$arch")"
    want_sha="$(asset_sha "$arch")"
    out="$DEST/fpcalc-$arch"

    echo "== fpcalc $arch：下载 $tarball"
    ok=0
    for url in "${BASE_URL}/${tarball}" "${MIRROR:+$MIRROR/${BASE_URL}/${tarball}}"; do
        [ -n "$url" ] || continue
        for attempt in 1 2 3; do
            if curl -fsSL --retry 2 --retry-delay 2 --connect-timeout 20 \
                    -o "$WORK/$tarball" "$url"; then
                ok=1
                break
            fi
            echo "   第 $attempt 次下载失败，重试…" >&2
            sleep 2
        done
        [ "$ok" -eq 1 ] && break
    done
    if [ "$ok" -ne 1 ]; then
        echo "✗ fpcalc $arch 下载失败：${BASE_URL}/${tarball}" >&2
        echo "  可在能访问 github 的网络下先跑一次，再把 vendor/bin 带过去；" >&2
        echo "  或设置 FPCALC_MIRROR 指向镜像（如 https://ghfast.top）。" >&2
        exit 1
    fi

    got_sha="$(sha256sum "$WORK/$tarball" | cut -d' ' -f1)"
    if [ "$got_sha" != "$want_sha" ]; then
        echo "✗ fpcalc $arch 校验和不符" >&2
        echo "  期望 $want_sha" >&2
        echo "  实际 $got_sha" >&2
        exit 1
    fi

    # 包里只有一个顶层目录 + fpcalc，剥掉目录层取出可执行文件
    rm -rf "$WORK/x"
    mkdir -p "$WORK/x"
    tar -xzf "$WORK/$tarball" -C "$WORK/x" --strip-components=1
    if [ ! -f "$WORK/x/fpcalc" ]; then
        echo "✗ fpcalc $arch 压缩包里没找到 fpcalc（结构可能变了）" >&2
        exit 1
    fi
    install -m 0755 "$WORK/x/fpcalc" "$out"
    echo "   ✓ $out   $(stat -c %s "$out") 字节   sha256=$(sha256sum "$out" | cut -d' ' -f1)"
done

echo
echo "== 内置 fpcalc 就绪（Chromaprint v${FPCALC_VERSION}）"
ls -l "$DEST"
