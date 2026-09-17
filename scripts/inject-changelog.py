#!/usr/bin/env python3
"""把 CHANGELOG 当前版本段落注入 manifest 的 `changelog` 字段。

这个逻辑原来只活在本地打包脚本里，而 CI 是直接 `fnpack build` 的 ——
于是 GitHub Release 上的 fpk 一直没有 `changelog` 字段，应用中心的「更新说明」是空的。
既然打包只允许在 GitHub 上进行，这段逻辑必须搬进 CI。

规则（与原本地脚本保持一致的约定）：
  * 取 CHANGELOG 里 `## [<version>]` 段落的列表项，最多 4 条，总长上限 600 字符；
  * 去掉 Markdown 的 **粗体** 与 `反引号`（fnpack 遇到反引号会把整个值包成三引号）；
  * 列表项的续行折进同一条（否则应用中心里会看到一句被腰斩的话）；
  * 放不下整条就丢掉整条，不在句子中间截断。

用法：
  inject-changelog.py <manifest> <CHANGELOG.md> <version> [--check]
  --check 只校验段落是否存在、生成结果是什么，不写文件（CI 预检用）。
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

MAX_BULLETS = 4
MAX_CHARS = 600


def extract_section(text: str, version: str) -> str:
    """取出 `## [version]` 到下一个 `## [` 之间的正文。"""
    pattern = r"^## \[" + re.escape(version) + r"\].*?$(.*?)(?=^## \[|\Z)"
    m = re.search(pattern, text, re.S | re.M)
    return m.group(1) if m else ""


def bullets_of(section: str) -> list[str]:
    """把段落里的列表项抽成一行一条，续行折进上一条。"""
    bullets: list[str] = []
    for raw in section.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("###"):        # 小标题（### 修复 / ### 说明）跳过
            continue
        m = re.match(r"^\s*[-*]\s+(.*)$", line)
        if m:
            bullets.append(m.group(1).strip())
        elif bullets and not re.match(r"^\s*#{1,6}\s", line):
            bullets[-1] = (bullets[-1] + " " + stripped).strip()   # 续行
    return bullets


def clean(text: str) -> str:
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)   # **粗体**
    text = text.replace("`", "")                    # 反引号：fnpack 见之会把整个值包成三引号
    text = re.sub(r"\[(.+?)\]\((.+?)\)", r"\1", text)  # [文字](链接) -> 文字
    text = text.replace('"', "")                    # 直引号：manifest 是 key = value 单行格式，
                                                    # 值里带引号的解析行为没有实测定论，索性不产生
    return re.sub(r"\s+", " ", text).strip()


def build_line(section: str) -> str:
    picked: list[str] = []
    total = 0
    for b in bullets_of(section):
        b = clean(b)
        if not b:
            continue
        added = len(b) + (len("<br/>") if picked else 0)
        if total + added > MAX_CHARS:
            break                      # 放不下就停，不在句子中间截断
        picked.append(b)
        total += added
        if len(picked) >= MAX_BULLETS:
            break
    return "<br/>".join(picked)


def write_field(manifest_path: Path, value: str) -> bool:
    s = manifest_path.read_text(encoding="utf-8")
    if re.search(r"^changelog\s*=", s, re.M):
        s2 = re.sub(r"^changelog\s*=.*$", "changelog             = " + value,
                    s, count=1, flags=re.M)
        changed = s2 != s
    else:
        s2 = s.rstrip("\n") + "\nchangelog             = " + value + "\n"
        changed = True
    manifest_path.write_text(s2, encoding="utf-8")
    return changed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("manifest", type=Path)
    ap.add_argument("changelog", type=Path)
    ap.add_argument("version")
    ap.add_argument("--check", action="store_true", help="只校验与打印，不写文件")
    args = ap.parse_args()

    if not args.manifest.is_file():
        print(f"✗ manifest 不存在：{args.manifest}", file=sys.stderr)
        return 1
    if not args.changelog.is_file():
        print(f"✗ CHANGELOG 不存在：{args.changelog}", file=sys.stderr)
        return 1

    text = args.changelog.read_text(encoding="utf-8")
    section = extract_section(text, args.version)
    if not section.strip():
        print(f"✗ CHANGELOG 里找不到 ## [{args.version}] 段落："
              f"应用中心会拿不到更新说明，拒绝继续", file=sys.stderr)
        return 1

    line = build_line(section)
    if not line:
        print(f"✗ ## [{args.version}] 段落里没有可用列表项", file=sys.stderr)
        return 1

    # 健全性：manifest 是 `key = value` 单行格式，值里绝不能有换行
    if any(c in line for c in "\r\n"):
        print("✗ 生成的 changelog 含换行，会破坏 manifest 格式", file=sys.stderr)
        return 1
    if len(line) > MAX_CHARS:
        print(f"✗ 生成的 changelog 超长（{len(line)} > {MAX_CHARS}）", file=sys.stderr)
        return 1

    print(f"== 版本 {args.version}：changelog {len(line)} 字符 / "
          f"{line.count('<br/>') + 1} 条")
    print(f"   {line}")

    if args.check:
        print("== --check：未写入 manifest")
        return 0

    write_field(args.manifest, line)
    print(f"== 已写入 {args.manifest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
