# -*- coding: utf-8 -*-
"""文件名清洗 + 候选查询词生成 + 搜索结果反推校验。

核心原则（不再从文件名里「猜」哪个是歌手、哪个是歌名）：
    把文件名拆成候选关键词 → 直接搜索 → 用搜索结果反推歌手和歌名。
    搜索本身就是解析手段。

对外接口（对应刮削四步）：
- clean_filename(filename) -> CleanedName        第 1 步：清洗文件名
- build_candidates(cleaned) -> List[Candidate]   第 2 步：生成候选查询词
- verify_by_result(name, title, artist) -> bool  第 4 步：反推校验（包含式，顺序无关）

兼容保留（旧「猜方向」链路，仅供 auto_ok 缓存导出等历史调用）：
parse_candidates() / dash_splits() / normalize_separators() / split_ext() / strip_track_prefix()。
新代码请用 build_candidates() + verify_by_result()。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Tuple

from musicmeta.sources.base import normalize_text

SUPPORTED_EXTS = {".flac", ".mp3", ".ogg", ".ape"}

# ---- 第 1 步：清洗 ----------------------------------------------------------

# 音轨号前缀："01." / "01 -" / "01_" / "01、" / "Track01" / "Track 01 -"
# 注意：必须带分隔符，否则「2002 年的第一场雪」这类歌名会被误当音轨号。
_TRACK_PREFIX_RE = re.compile(
    r"^\s*track\s*[.\-_]?\s*(?P<t1>\d{1,3})\s*[.\-_、]?\s*"
    r"|^\s*(?P<t2>\d{1,3})\s*[.\-_、]\s*",
    re.IGNORECASE,
)

# 噪音标签（音质/格式/来源水印）：整段丢弃，不参与搜索
_NOISE_WORDS = (
    "320k", "320kbps", "192k", "256k", "128k", "64k",
    "flac", "ape", "wav", "mp3", "m4a", "aac", "ogg",
    "hires", "hi-res", "hq", "sq", "dts", "dsd",
    "无损", "母带", "臻品", "全景声", "official", "官方版", "官方", "mv", "mv版",
    "高清", "高清版",
)

# 版本标记（(Live)/(Remix)/(Acoustic)/(伴奏) 等）：
# 搜索前移除；反推校验针对「原始文件名」，所以这些词仍可出现在搜索结果里。
_VERSION_WORDS = (
    "live", "现场", "演唱会", "remix", "mix", "混音", "伴奏", "纯音乐",
    "instrumental", "acoustic", "unplugged", "不插电", "铃声", "karaoke",
    "翻唱", "cover", "dj", "慢摇", "加快", "变调", "串烧", "重制", "remaster",
    "复刻", "黑胶", "纪念版", "周年", "demo", "radio edit", "drumless",
    "8d", "环绕", "合唱版",
)


def _bracket_re(words) -> re.Pattern:
    """匹配「(词)」「[词]」「（词）」「【词】」形式的整段括号标签。"""
    body = "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))
    return re.compile(r"[\[【(（]\s*(?:" + body + r")\s*[\]】)）]", re.IGNORECASE)


_NOISE_BRACKET_RE = _bracket_re(_NOISE_WORDS)
_VERSION_BRACKET_RE = _bracket_re(_VERSION_WORDS)

# 尾部裸标签（无括号）："歌名-官方版" / "歌名 MV" / "歌名 320K" / "歌名-伴奏版"
_NOISE_SUFFIX_RE = re.compile(
    r"(?:[\s\-_]+(?:" + "|".join(re.escape(w) for w in _NOISE_WORDS) + r"))+\s*$",
    re.IGNORECASE)
_VERSION_SUFFIX_RE = re.compile(
    r"(?:[\s\-_—–]+(?:" + "|".join(re.escape(w) for w in _VERSION_WORDS)
    + r"))[版曲]?\s*$",
    re.IGNORECASE)

# 书名号/方括号/圆括号：括号内内容单独作为候选（第 2 步），括号本身从主串去掉
_BRACKET_INNER_RE = re.compile(r"[《<\[(（【]([^》>\]）)】]+)[》>\]）)】]")
# 括号内如果是纯数字（如 "[01]"）或过短，不当作候选
_BRACKET_MIN_LEN = 2

# ---- 第 2 步：分隔符优先级 ------------------------------------------------
# 优先级：' - ' > ' – ' > ' — ' > '-' > '_' > '|'
_SEP_GROUPS: List[Tuple[str, Tuple[str, ...]]] = [
    (" - ", (" - ",)),
    (" – ", (" – ", "\u2013", "\u2014", "\u2015", "\u2212")),   # – — ― −
    ("-", ("-",)),
    ("_", ("_", "\uff3f")),                                    # _ ＿
    ("|", ("|", "\uff5c")),                                     # | ｜
]
# 生成候选的硬上限（控制在线请求数）
_MAX_SPLITS = 3
_MAX_CANDIDATES = 8

# 归一化分隔符（兼容旧链路）：全角/半角常见分隔统一成 '-'
_SEP_NORM = {
    "\u2013": "-", "\u2014": "-", "\u2015": "-", "\u2212": "-",
    "_": "-", "\uff3f": "-", "|": "-", "\uff5c": "-", "\u3000": " ",
}


@dataclass
class ParsedName:
    """【兼容旧链路】一个文件名切分候选。"""

    stem: str        # 原始文件名（去掉扩展名）
    title: str
    artist: str      # 可能为空（文件名里没有 '-'）
    ext: str         # ".flac" 等，小写；无扩展名为 ""
    track_hint: str  # 文件名开头的曲目号（可能为空）


@dataclass
class CleanedName:
    """第 1 步产出：清洗后的文件名。"""

    original: str                    # 原始文件名（含扩展名）
    stem: str                        # 原始文件名去掉扩展名（第 4 步反推校验用）
    clean: str                       # 清洗后主串（去音轨号/噪音标签/书名号）
    ext: str = ""                    # 小写扩展名，无则 ""
    track_hint: str = ""             # 识别出的音轨号
    bracket_hits: List[str] = field(default_factory=list)   # 括号内内容（独立候选）
    removed: List[str] = field(default_factory=list)        # 被移除的标签（排查用）


@dataclass
class Candidate:
    """第 2 步产出：一个候选查询词。"""

    text: str            # 直接送去搜索的候选词
    kind: str            # full（整串）/ left（左段）/ right（右段）/ bracket（括号内）
    sep: str = ""        # 生成该候选所用的分隔符（full/bracket 为空）


# ---- 兼容旧链路的工具（历史调用仍在用）------------------------------------

def split_ext(filename: str) -> Tuple[str, str]:
    """返回 (stem, ext)。无扩展名时 ext=''。"""
    name = (filename or "").strip()
    idx = name.rfind(".")
    if idx <= 0:
        return name, ""
    return name[:idx], name[idx:].lower()


def normalize_separators(stem: str) -> str:
    """【兼容旧链路】把常见全角/半角分隔统一成 '-'。不做空格/冒号归一。"""
    return "".join(_SEP_NORM.get(ch, ch) for ch in stem)


def strip_track_prefix(stem: str) -> Tuple[str, str]:
    """去掉开头音轨号：'01. 晴天-周杰伦' -> ('01', '晴天-周杰伦')。

    支持 '01.' / '01 -' / '01_' / '01、' / 'Track01' / 'Track 01.'。
    必须带分隔符，避免把「2002 年的第一场雪」误判成音轨号。
    """
    m = _TRACK_PREFIX_RE.match(stem or "")
    if m:
        num = m.group("t1") or m.group("t2") or ""
        return num, stem[m.end():].strip()
    return "", (stem or "").strip()


def dash_splits(stem: str, max_splits: int = _MAX_SPLITS) -> List[Tuple[str, str]]:
    """【兼容旧链路】按 '-' 生成 (标题, 歌手) 候选切分，优先从最后一个 '-' 切。"""
    stem = normalize_separators(stem)
    parts = [p.strip() for p in stem.split("-")]
    if len(parts) < 2:
        return [(stem, "")]
    out: List[Tuple[str, str]] = []
    start = max(len(parts) - 1 - max_splits, 0)
    for i in range(len(parts) - 1, start - 1, -1):
        title = "-".join(parts[:i]).strip()
        artist = "-".join(parts[i:]).strip()
        if title and artist:
            out.append((title, artist))
    if not out:
        out.append((stem, ""))
    return out


# ---- 第 1 步：清洗文件名 ----------------------------------------------------

def clean_filename(filename: str) -> CleanedName:
    """第 1 步：去扩展名 → 去音轨号 → 去噪音标签 → 取出括号内内容。

    - 噪音标签（[320K]/(Hi-Res)/官方版/MV…）整段丢弃；
    - 版本标记（(Live)/(Remix)/(Acoustic)…）同样丢弃，**但反推校验用的是原始
      文件名**，所以这些词仍允许出现在搜索结果里（见 verify_by_result）；
    - 书名号/方括号/圆括号内非噪音的内容单独记为 bracket_hits（第 2 步候选）。
    """
    stem, ext = split_ext(filename)
    track_hint, rest = strip_track_prefix(stem)
    if not rest:
        rest = stem
    work = rest
    removed: List[str] = []

    # 1) 括号形式的噪音/版本标签（先噪音、后版本，逐个记录被移除内容）
    for rx in (_NOISE_BRACKET_RE, _VERSION_BRACKET_RE):
        def _note(m, _rx=rx):
            removed.append(m.group(0).strip())
            return " "
        work = rx.sub(_note, work)

    # 2) 括号内其余内容 → 独立候选；括号本身从主串去掉
    bracket_hits: List[str] = []

    def _take(m):
        inner = m.group(1).strip()
        if len(inner) >= _BRACKET_MIN_LEN and not inner.isdigit():
            bracket_hits.append(inner)
            return " "
        return " "

    work = _BRACKET_INNER_RE.sub(_take, work)

    # 3) 尾部裸标签
    for rx in (_NOISE_SUFFIX_RE, _VERSION_SUFFIX_RE):
        def _note2(m, _rx=rx):
            removed.append(m.group(0).strip())
            return ""
        work = rx.sub(_note2, work)

    # 4) 收尾：压缩空白、去掉首尾残余分隔符
    work = re.sub(r"\s{2,}", " ", work).strip()
    work = work.strip(" -_—–|\u3000").strip()
    if not work:
        work = rest.strip()

    return CleanedName(original=(filename or "").strip(), stem=stem, clean=work,
                       ext=ext, track_hint=track_hint,
                       bracket_hits=bracket_hits, removed=removed)


# ---- 第 2 步：生成候选查询词 ------------------------------------------------

_SPLIT_MARK = "\x00"   # 内部占位符：标记「这一组分隔符」的切分位置


def _as_query(text: str) -> str:
    """把一段文本变成搜索词：分隔符/多余空白折成单空格。

    候选词只影响搜索召回，不参与「谁是谁」的判断，所以这里不做方向猜测。
    """
    out = text.replace(_SPLIT_MARK, " ")
    for _name, chars in _SEP_GROUPS:
        for ch in chars:
            out = out.replace(ch, " ")
    out = re.sub(r"\s{2,}", " ", out).strip()
    return out


def build_candidates(cleaned: CleanedName) -> List[Candidate]:
    """第 2 步：把清洗后的文件名拆成候选查询词。

    顺序（也是搜索顺序，命中即停）：
    1. 整串
    2. 按分隔符优先级 ' - ' > '–' > '—' > '-' > '_' > '|' 切出的左段 / 右段
       （同一分隔符出现多次时，逐个切分位置各给一组，最多 _MAX_SPLITS 组）
    3. 括号内内容（《》<>[]()）

    不判断哪个是歌手、哪个是歌名——由第 4 步的搜索结果反推。
    """
    text = cleaned.clean or cleaned.stem
    out: List[Candidate] = []

    def _add(t: str, kind: str, sep: str = "") -> None:
        q = _as_query(t)
        if not q:
            return
        if any(c.text == q for c in out):
            return
        out.append(Candidate(text=q, kind=kind, sep=sep))

    _add(text, "full")

    # 只使用「最高优先级且确实存在」的那组分隔符来切分：
    # 把该组的所有写法统一换成内部占位符，再按占位符位置逐个切分。
    for name, chars in _SEP_GROUPS:
        if not any(ch in text for ch in chars):
            continue
        work = text
        for ch in chars:
            work = work.replace(ch, _SPLIT_MARK)
        positions = [i for i, c in enumerate(work) if c == _SPLIT_MARK]
        for pos in positions[:_MAX_SPLITS]:
            _add(work[:pos], "left", name)
            _add(work[pos + 1:], "right", name)
        break

    for b in cleaned.bracket_hits:
        _add(b, "bracket")

    return out[:_MAX_CANDIDATES]


# ---- 第 4 步：用搜索结果反推歌手 / 歌名 -------------------------------------

# 结果的 artistName 里可能是多歌手（"A/B"、"A、B"、"A feat. B"）
_MULTI_ARTIST_RE = re.compile(r"[/;、,&]|feat\.?|ft\.?", re.IGNORECASE)


def verify_detail(name: str, title: str, artist: str) -> Tuple[bool, str]:
    """第 4 步：校验一条搜索结果是否可信，返回 (是否命中, 原因)。

    规则：结果的 trackName 与 artistName 都要（不区分大小写地）出现在
    原始文件名中。匹配不依赖顺序，因此天然兼容「歌手 - 歌名」与
    「歌名 - 歌手」两种命名，也能接受结果里带着 (Live)/(Remix) 这类词
    （因为校验用的是原始文件名，不是清洗后的搜索词）。

    name: 原始文件名（去扩展名）；title/artist: 搜索结果的字段。
    """
    n = normalize_text(name)
    t = normalize_text(title)
    a = normalize_text(artist)
    if not n:
        return False, "文件名为空"
    if not t:
        return False, "结果缺少歌名"
    if not a:
        return False, "结果缺少歌手"
    if t not in n:
        return False, f"歌名「{title}」未出现在文件名中"
    if a in n:
        return True, "歌名+歌手都命中"
    # 多歌手/合作：整体串不在文件名里时，退一步只要求其中一个歌手名出现
    # （如结果 artistName="周杰伦/费玉清" vs 文件名 "千里之外-费玉清"）。
    parts = [normalize_text(x) for x in _MULTI_ARTIST_RE.split(artist) if x.strip()]
    if any(len(p) >= 2 and p in n for p in parts):
        return True, "歌名命中 + 多歌手中有一位命中"
    return False, f"歌手「{artist}」未出现在文件名中"


def verify_by_result(name: str, title: str, artist: str) -> bool:
    """第 4 步（布尔版）：结果歌名与歌手是否都出现在原始文件名中。"""
    return verify_detail(name, title, artist)[0]


# ---- 兼容旧链路 ------------------------------------------------------------

def parse_candidates(filename: str) -> List[ParsedName]:
    """【兼容旧链路】把文件名解析为若干 (标题, 歌手) 候选。"""
    stem, ext = split_ext(filename)
    track_hint, rest = strip_track_prefix(stem)
    if not rest:
        rest = stem
    return [
        ParsedName(stem=stem, title=title, artist=artist, ext=ext,
                   track_hint=track_hint)
        for title, artist in dash_splits(rest)
    ]
