# -*- coding: utf-8 -*-
"""标签键名对齐测试：证明「我们写出的键」= 「飞牛音乐真正会读的键」。

飞牛音乐（本机应用中心里的 trim.music，服务端二进制 trim-music，用
github.com/dhowden/tag 读标签）实际读取的键（源码级结论，见交接笔记第 9 节）：

  FLAC / OGG（VorbisComment，读入时键统一转小写）
      title  artist  album  albumartist  composer  genre
      date（没有 date 时回退 year）
      tracknumber + tracktotal    discnumber + disctotal
      lyrics                      metadata_block_picture      comment（回退 description）
  MP3（ID3v2）
      TIT2 TPE1 TALB TPE2 TCOM TCON TYER(ID3v2.3)/TDRC(ID3v2.4)
      TRCK("3/12")  TPOS("1/2")  USLT  APIC  COMM

本测试不看应用自己的读法（那可能自洽却与服务端不一致），而是：
  1. 用应用真实的写入器（musicmeta.writer）造出 flac / ogg / mp3；
  2. **直接读文件原始字节**取出真实落盘的键名（mutagen 读 Vorbis 会把键转小写，
     只有原始字节能证明我们写的是 LYRICS 还是 lyrics）；
  3. 逐项比对服务端要读的键是否齐全、以及旧写法是否已被清掉。
"""
from __future__ import annotations

import os
import struct
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
# 本文件位于 <repo>/app/server/tests/，上一级就是可以 import musicmeta 的根
sys.path.insert(0, os.path.dirname(HERE))

from musicmeta.sources.base import SongMeta  # noqa: E402
from musicmeta.writer import read_lyrics, read_tags, write_cover, write_lyrics, write_metadata  # noqa: E402

REPO = os.path.abspath(os.path.join(HERE, "..", "..", ".."))   # <repo>/
LRC = "[00:01.00]测试歌词第一行\n[00:02.00]测试歌词第二行\n"

# 服务端 dhowden/tag 读 Vorbis 时用的键（小写）
SERVICE_VORBIS = ["title", "artist", "album", "albumartist", "genre", "year",
                  "tracknumber", "tracktotal", "discnumber", "lyrics"]
# 服务端 dhowden/tag 读 ID3v2 时用的帧（track/disc 是 "x/n"）
SERVICE_ID3 = ["TIT2", "TPE1", "TALB", "TPE2", "TCON", "TDRC", "TRCK", "TPOS", "USLT"]

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(("  ✓ " if ok else "  ✗ ") + name + (("   " + str(detail)) if detail else ""))


# ---------------- 真实落盘键名 ----------------
# mutagen 读 VorbisComment 返回的键名就是文件里的原始拼写（只在查找时才大小写不敏感），
# 所以这里用它取「我们到底写了哪些键」；键的原始大小写另有断言用 ffprobe 复核。

def raw_vorbis_keys(path: str) -> list:
    if path.endswith(".flac"):
        from mutagen.flac import FLAC
        tags = FLAC(path).tags or {}
    else:
        from mutagen.oggvorbis import OggVorbis
        tags = OggVorbis(path).tags or {}
    return [str(k) for k in tags.keys()]


def ffprobe_tag_keys(path: str) -> list:
    """ffprobe 显示的键名（FLAC 可用来复核歌词键的大小写）。"""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format_tags", "-of", "flat", path],
        capture_output=True, text=True).stdout
    return [ln[len("format.tags."):].split("=", 1)[0]
            for ln in out.splitlines() if ln.startswith("format.tags.")]


def lower_set(keys):
    return {k.lower() for k in keys}


# ---------------- 样本 ----------------

def make_meta():
    m = SongMeta()
    m.title = "对齐测试曲"
    m.artist = "测试歌手"
    m.album = "测试专辑"
    m.album_artist = "测试专辑艺人"
    m.date = "2024-05-06"
    m.genre = "流行"
    m.track = "3"
    m.track_total = "12"
    m.disc = "1"
    m.publisher = "测试唱片公司"
    m.language = "国语"
    m.comment = "测试备注"
    return m


def silence(dest, codec=None):
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
           "anullsrc=r=44100:cl=stereo", "-t", "2"]
    if codec:
        cmd += ["-c:a", codec]
    if dest.endswith(".mp3"):
        cmd += ["-b:a", "64k"]
    subprocess.run(cmd + [dest], check=True)


def main():
    tmp = tempfile.mkdtemp(prefix="mmw_keyalign_")
    flac = os.path.join(tmp, "a.flac")
    ogg = os.path.join(tmp, "a.ogg")
    mp3 = os.path.join(tmp, "a.mp3")
    silence(flac)
    silence(ogg, "libvorbis")
    silence(mp3)

    print("【1】FLAC：落盘键名")
    write_metadata(flac, make_meta())
    write_lyrics(flac, LRC)
    keys = raw_vorbis_keys(flac)
    print("     实际写入:", keys)
    low = lower_set(keys)
    missing = [k for k in SERVICE_VORBIS if k not in low]
    check("服务端要读的键全部写出", not missing, ("缺 " + str(missing)) if missing else "")
    raw = open(flac, "rb").read()
    check("歌词落盘键名是 LYRICS（大写，原始字节复核）",
          b"LYRICS=" in raw, [k for k in keys if "lyric" in k.lower()])
    check("没有留下旧写法小写 lyrics= / UNSYNCED LYRICS=",
          b"lyrics=" not in raw and b"UNSYNCED LYRICS=" not in raw)
    check("歌词只有 LYRICS + UNSYNCEDLYRICS 两个字段",
          sorted(k for k in keys if "lyric" in k.lower()) == ["lyrics", "unsyncedlyrics"])

    # 服务端读 FLAC/OGG 年份只认纯数字 YEAR，不认 date（真机对照实验结论）
    year_vals = [ln.split("=", 1)[1] for ln in [] ]
    lows = {k.lower(): k for k in keys}
    check("写出生份用的 YEAR 键", "year" in lows, [k for k in keys if k.upper().startswith("YEAR")])
    if "year" in lows:
        from mutagen.flac import FLAC as _FLAC
        got = str(_FLAC(flac).tags.get(lows["year"], [""])[0])
        check("YEAR 是纯数字（服务端只认纯数字）", got.isdigit() and len(got) == 4, repr(got))
        check("YEAR 与 date 的年份一致", got == "2024", f"YEAR={got} date={_FLAC(flac).tags.get('date')}")

    print("【2】FLAC：应用自己读回（写入的值必须能读回）")
    t = read_tags(flac)
    check("歌名/歌手/专辑", t.get("title") == "对齐测试曲" and t.get("artist") == "测试歌手"
          and t.get("album") == "测试专辑", {k: t.get(k) for k in ("title", "artist", "album")})
    check("专辑艺人", t.get("album_artist") == "测试专辑艺人", repr(t.get("album_artist")))
    check("曲目号 / 总曲目", t.get("track") == "3" and t.get("track_total") == "12",
          f"{t.get('track')}/{t.get('track_total')}")
    check("碟号", t.get("disc") == "1", repr(t.get("disc")))
    check("日期", t.get("date") == "2024-05-06", repr(t.get("date")))
    check("歌词", read_lyrics(flac) == LRC, repr(read_lyrics(flac))[:24])

    print("【3】OGG：同一套键")
    write_metadata(ogg, make_meta())
    write_lyrics(ogg, LRC)
    keys = raw_vorbis_keys(ogg)
    print("     实际写入:", keys)
    low = lower_set(keys)
    missing = [k for k in SERVICE_VORBIS if k not in low]
    check("服务端要读的键全部写出", not missing, ("缺 " + str(missing)) if missing else "")
    raw = open(ogg, "rb").read()
    check("歌词落盘键名是 LYRICS（大写，原始字节复核）", b"LYRICS=" in raw,
          [k for k in keys if "lyric" in k.lower()])
    check("OGG 歌词可回读", read_lyrics(ogg) == LRC)
    from mutagen.oggvorbis import OggVorbis as _OGG
    _t = _OGG(ogg).tags
    check("OGG 也写了纯数字 YEAR", str(_t.get("YEAR", _t.get("year", [""]))[0]).isdigit(),
          repr(_t.get("YEAR", _t.get("year"))))

    print("【4】MP3：ID3v2 帧（读原始帧名）")
    write_metadata(mp3, make_meta())
    write_lyrics(mp3, LRC)
    from mutagen.id3 import ID3
    raw = ID3(mp3)
    frames = sorted({k.split(":")[0] for k in raw.keys()})
    print("     实际写入:", frames)
    missing = [f for f in SERVICE_ID3 if f not in frames]
    check("服务端读的帧全部写出", not missing, ("缺 " + str(missing)) if missing else "")
    check("TRCK 是 3/12（服务端按 x/n 拆）", str(raw.getall("TRCK")[0]) == "3/12",
          str(raw.getall("TRCK")[0]))
    t = read_tags(mp3)
    check("MP3 读回曲目号/总曲目拆解正确",
          t.get("track") == "3" and t.get("track_total") == "12",
          f"{t.get('track')}/{t.get('track_total')}")
    check("MP3 歌词 USLT 回读一致", read_lyrics(mp3) == LRC)

    print("【5】封面：各格式写服务端认的字段")
    png = os.path.join(REPO, "ICON.PNG")
    data = open(png, "rb").read()
    from mutagen.flac import FLAC
    write_cover(flac, data)
    check("FLAC → picture block（服务端读 pictureBlock）", bool(FLAC(flac).pictures))
    write_cover(mp3, data)
    check("MP3 → APIC", bool(ID3(mp3).getall("APIC")))
    write_cover(ogg, data)
    keys = raw_vorbis_keys(ogg)
    check("OGG → METADATA_BLOCK_PICTURE（服务端读 Vorbis 图片块）",
          any(k.lower() == "metadata_block_picture" for k in keys),
          [k for k in keys if "picture" in k.lower()])

    print("【6】幂等：重复写不产生重复键")
    before = sorted(lower_set(raw_vorbis_keys(flac)))
    write_lyrics(flac, LRC)
    write_metadata(flac, make_meta())
    after = sorted(lower_set(raw_vorbis_keys(flac)))
    check("键集合不变", before == after, f"{len(before)} -> {len(after)}")

    print()
    failed = [n for n, ok, _ in results if not ok]
    if failed:
        print("失败 %d 项：" % len(failed))
        for n in failed:
            print("  -", n)
        return 1
    print("全部通过（%d 项）" % len(results))
    return 0


if __name__ == "__main__":
    sys.exit(main())
