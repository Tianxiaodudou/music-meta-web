# -*- coding: utf-8 -*-
"""用 mutagen 把 SongMeta 写入 flac / mp3 / ogg / ape 文件。

**本应用的定位就是配合飞牛官方音乐：它读什么键，我们就写什么键；它不读的，一律不写。**
（用户 2026-09-12 拍板。）允许写入的字段集中在 `READ_BY_FEINIU` 里，由 `_guard()` 强制执行。

本机落地情况（`trim.music` 服务端 `trim-music`，字节级检索 + 真机对照实验）：

- FLAC / OGG (VorbisComment，读入时键统一转小写)
    title  artist  album  albumartist  genre
    **YEAR（纯数字年份，服务端只认这个）** + date（完整日期，别的工具用）
    tracknumber + tracktotal     discnumber
    lyrics                       metadata_block_picture
  注意：服务端读 FLAC/OGG 年份**不认 `date`**，只认 `YEAR` 且必须纯数字
  （2026-09-12 真机对照：只写 date 的文件服务端 year=null，写 year=2024 才有值）。
- MP3 (ID3v2): TIT2 TPE1 TALB TPE2 TCON TDRC TRCK("3/12") TPOS USLT APIC
- APE (APEv2): Title Artist Album "Album Artist" Date Genre Track
    —— 飞牛音乐不索引 .ape，这里只是不让批量处理报错，键名按最通行写法写。

**不写的字段（飞牛音乐从来不看这些键，写了也是白写）**：
    publisher / TPUB（唱片公司）、language / TLAN（语言）、comment / COMM（备注）
    以及 composer / lyricist / grouping（服务端查表里没有这些键）
服务端二进制里 `PUBLISHER` / `LABEL` / `LANGUAGE` / `COMPOSER` / `LYRICIST` 出现 0 次，
而 `TITLE` / `ARTIST` / `ALBUM` / `ALBUMARTIST` / `YEAR` / `GENRE` / `TRACKNUMBER` /
`TRACKTOTAL` / `DISCNUMBER` / `LYRIC`+`LYRICS` 都能查到，所以按后者写。

歌词另有 UNSYNCEDLYRICS 兜底（服务端只认 LYRICS，其它播放器认 UNSYNCEDLYRICS）。

只写入非空字段；已有标签会被更新，不会删除其它字段。
键名回归见 app/server/tests/test_tag_keys.py。
"""
from __future__ import annotations

import os
from typing import Dict, Optional

from .sources.base import SongMeta


def _year_only(date: str) -> str:
    """从日期里取 4 位年份（"2024-05-06" → "2024"）。

    飞牛音乐服务端读 FLAC / OGG 的年份时**只认 `YEAR` 键，且必须是纯数字**：
    只有 `date=2024-05-06` 的文件，服务端返回的 year 是 null；写成 `year=2024`
    才读得到（2026-09-12 真机对照实验验证）。所以 Vorbis 侧固定写一份 `YEAR=年份`。
    """
    text = (date or "").strip()
    if not text:
        return ""
    # 开头就是 4 位数字、后面不再接数字（"2024" / "2024-05-06" / "2024年5月"）
    if len(text) >= 4 and text[:4].isdigit() and (len(text) == 4 or not text[4].isdigit()):
        return text[:4]
    # 其它写法（"05/06/2024" 之类）：挑第一段 4 位数字，但整段必须正好 4 位（不是 "12345"）
    for token in text.replace("/", "-").replace(".", "-").replace("年", "-").split("-"):
        token = token.strip()
        if len(token) == 4 and token.isdigit():
            return token
    return ""


#: 本应用存在的意义就是配合飞牛官方音乐：**它读什么键，我们就写什么键**。
#: 下面这份「键 → 中文名」是唯一允许写入的清单，其它键一律不写（用户 2026-09-12 拍板）。
#:
#: 判定依据（`/usr/local/apps/@appcenter/trim.music/trim-music` 字节级检索 + 真机对照实验）：
#:   有（会被读）：TITLE ARTIST ALBUM ALBUMARTIST YEAR GENRE TRACKNUMBER TRACKTOTAL
#:                 DISCNUMBER LYRIC/LYRICS COMMENT DESCRIPTION PERFORMER ISRC BARCODE
#:   没有（从不查）：PUBLISHER LABEL LANGUAGE COMPOSER LYRICIST GROUPING
#: 所以 `publisher`（唱片公司）、`language`（语言）、`comment`（备注）写了也是白写，
#: 一律不写；`composer` 虽然 dhowden/tag 有 Composer()，但服务端查表里没有这个键，
#: 而且本应用的字段开关里也没有它，同样不写。
READ_BY_FEINIU = {
    "title", "artist", "album", "album_artist",
    "YEAR", "genre", "track", "track_total", "disc", "lyrics", "cover",
}

#: 兜底告警：万一以后有人加了新字段却忘了确认「飞牛音乐读不读」，动静要看得见
_UNCHECKED_WRITE_FIELDS: set = set()


def _guard(key: str) -> bool:
    """写入前的最后一道闸：不在 READ_BY_FEINIU 清单里的字段直接丢弃（并留一行日志）。"""
    if key in READ_BY_FEINIU:
        return True
    if key not in _UNCHECKED_WRITE_FIELDS:
        _UNCHECKED_WRITE_FIELDS.add(key)
        print(f"[writer] 跳过字段 {key!r}：飞牛音乐不读这个标签，按约定不写")
    return False


def _vorbis_fields(meta: SongMeta) -> Dict[str, str]:
    """VorbisComment 字段（flac / ogg 共用）。

    年份写两份：`date` 保留完整日期（别的播放器/工具用），
    `YEAR` 是飞牛音乐服务端真正读的那份（必须是纯数字年份）。
    """
    fields: Dict[str, str] = {}
    if meta.title and _guard("title"):
        fields["title"] = meta.title
    if meta.artist and _guard("artist"):
        fields["artist"] = meta.artist
    if meta.album and _guard("album"):
        fields["album"] = meta.album
    if meta.album_artist and _guard("album_artist"):
        fields["albumartist"] = meta.album_artist
    if meta.date and _guard("YEAR"):
        fields["date"] = meta.date
        year = _year_only(meta.date)
        if year:
            fields["YEAR"] = year
    if meta.genre and _guard("genre"):
        fields["genre"] = meta.genre
    if meta.track and _guard("track"):
        fields["tracknumber"] = meta.track
    if meta.track_total and _guard("track_total"):
        fields["tracktotal"] = meta.track_total
    if meta.disc and _guard("disc"):
        fields["discnumber"] = meta.disc
    return fields


def _track_str(meta: SongMeta) -> str:
    """'3/11' 形式曲目号（mp3 / ape 用）。"""
    if meta.track and meta.track_total:
        return f"{meta.track}/{meta.track_total}"
    return meta.track or ""


def _write_vorbis(audio, meta: SongMeta) -> None:
    if audio.tags is None:
        audio.add_tags()
    for key, value in _vorbis_fields(meta).items():
        audio.tags[key] = value
    audio.save()


def _write_id3(audio, meta: SongMeta) -> None:
    from mutagen.id3 import TALB, TCON, TDRC, TIT2, TPE1, TPE2, TPOS, TRCK

    if audio.tags is None:
        audio.add_tags()
    tags = audio.tags
    enc = 3  # UTF-8
    if meta.title and _guard("title"):
        tags.add(TIT2(encoding=enc, text=[meta.title]))
    if meta.artist and _guard("artist"):
        tags.add(TPE1(encoding=enc, text=[meta.artist]))
    if meta.album and _guard("album"):
        tags.add(TALB(encoding=enc, text=[meta.album]))
    if meta.album_artist and _guard("album_artist"):
        tags.add(TPE2(encoding=enc, text=[meta.album_artist]))
    if meta.date and _guard("YEAR"):
        tags.add(TDRC(encoding=enc, text=[meta.date]))
    if meta.genre and _guard("genre"):
        tags.add(TCON(encoding=enc, text=[meta.genre]))
    track = _track_str(meta)
    if track and _guard("track"):
        tags.add(TRCK(encoding=enc, text=[track]))
    if meta.disc and _guard("disc"):
        tags.add(TPOS(encoding=enc, text=[meta.disc]))
    audio.save()


def _write_ape(audio, meta: SongMeta) -> None:
    """APE（`.ape`）写入。

    注意：飞牛音乐**根本不索引 .ape 文件**，所以这里的键名没有一个会被官方音乐读到，
    保留只是为了让「批量处理 ape 文件」不至于报错。按「不读就不写」的原则，
    只有 title/artist/album/album_artist/date/genre/track 这些最常见的写，
    publisher / language / comment 一律不写（官方不读）。
    """
    if audio.tags is None:
        audio.add_tags()
    tags = audio.tags
    if meta.title and _guard("title"):
        tags["Title"] = meta.title
    if meta.artist and _guard("artist"):
        tags["Artist"] = meta.artist
    if meta.album and _guard("album"):
        tags["Album"] = meta.album
    if meta.album_artist and _guard("album_artist"):
        tags["Album Artist"] = meta.album_artist
    if meta.date and _guard("YEAR"):
        tags["Date"] = meta.date
    if meta.genre and _guard("genre"):
        tags["Genre"] = meta.genre
    track = _track_str(meta)
    if track and _guard("track"):
        tags["Track"] = track
    audio.save()


def write_metadata(path: str, meta: SongMeta) -> None:
    """把元数据写入音频文件。format 由扩展名决定。"""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".flac":
        from mutagen.flac import FLAC
        _write_vorbis(FLAC(path), meta)
    elif ext == ".ogg":
        from mutagen.oggvorbis import OggVorbis
        _write_vorbis(OggVorbis(path), meta)
    elif ext == ".mp3":
        from mutagen.mp3 import MP3
        _write_id3(MP3(path), meta)
    elif ext == ".ape":
        from mutagen.monkeysaudio import MonkeysAudio
        _write_ape(MonkeysAudio(path), meta)
    else:
        raise ValueError(f"不支持的音频格式: {ext}")


def _image_mime(data: bytes) -> str:
    """按魔数判断图片 MIME（默认 JPEG）。"""
    if data[:4] == b"\x89PNG":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return "image/jpeg"


def write_cover(path: str, image_bytes: bytes) -> None:
    """把封面图写入音频文件（各格式对应标准字段）。"""
    ext = os.path.splitext(path)[1].lower()
    mime = _image_mime(image_bytes)
    if ext == ".flac":
        from mutagen.flac import FLAC, Picture
        audio = FLAC(path)
        pic = Picture()
        pic.type = 3  # front cover
        pic.mime = mime
        pic.desc = ""
        pic.data = image_bytes
        audio.clear_pictures()
        audio.add_picture(pic)
        audio.save()
    elif ext == ".mp3":
        from mutagen.id3 import APIC
        from mutagen.mp3 import MP3
        audio = MP3(path)
        if audio.tags is None:
            audio.add_tags()
        audio.tags.delall("APIC")
        audio.tags.add(APIC(encoding=3, mime=mime, type=3, desc="", data=image_bytes))
        audio.save()
    elif ext == ".ogg":
        import base64
        from mutagen.flac import Picture
        from mutagen.oggvorbis import OggVorbis
        pic = Picture()
        pic.type = 3
        pic.mime = mime
        pic.desc = ""
        pic.data = image_bytes
        audio = OggVorbis(path)
        if audio.tags is None:
            audio.add_tags()
        audio.tags["metadata_block_picture"] = [
            base64.b64encode(pic.write()).decode("ascii")]
        audio.save()
    elif ext == ".ape":
        from mutagen.apev2 import APEBinaryValue
        from mutagen.monkeysaudio import MonkeysAudio
        audio = MonkeysAudio(path)
        if audio.tags is None:
            audio.add_tags()
        # APE 封面约定：键 "Cover Art (Front)"，值 = 文件名(空) + \x00 + 图片数据
        audio.tags["Cover Art (Front)"] = APEBinaryValue(b"\x00" + image_bytes)
        audio.save()
    else:
        raise ValueError(f"不支持的音频格式: {ext}")


#: FLAC / OGG（VorbisComment）里歌词写两份键名：
#   - LYRICS          通行的写法（foobar2000 / MusicBee / Picard 都写这个）
#   - UNSYNCEDLYRICS  「非同步歌词」的约定键，只认它的播放器靠它兜底
# Vorbis 规范规定注释键大小写不敏感，所以 LYRICS 与 lyrics 是同一个字段，
# 不能两个都写（会成为同一字段的两份）。实测飞牛音乐两种写法都能读到，
# 但 UNSYNCEDLYRICS 它不认，所以必须是「LYRICS + UNSYNCEDLYRICS」这套组合。
_VORBIS_LYRICS_KEYS = ("LYRICS", "UNSYNCEDLYRICS")

#: 同一字段的其它写法（含旧版本写入的小写 lyrics）：写入前先清掉，避免文件里出现两份歌词。
# mutagen 的 Vorbis 标签查找/删除是大小写不敏感的，写一个小写名即可清掉所有大小写变体。
_VORBIS_LYRICS_ALIASES = ("lyrics", "unsyncedlyrics", "unsynced lyrics", "lyric")


def _write_vorbis_lyrics(tags, lrc: str) -> None:
    """把歌词写进 FLAC / OGG 的 VorbisComment（先清旧写法，再写规范键）。"""
    for key in _VORBIS_LYRICS_ALIASES:
        try:
            del tags[key]
        except KeyError:
            pass
    for key in _VORBIS_LYRICS_KEYS:
        tags[key] = lrc


def write_lyrics(path: str, lrc: str) -> None:
    """把 LRC 歌词写入音频文件。"""
    if not lrc:
        return
    ext = os.path.splitext(path)[1].lower()
    if ext == ".mp3":
        from mutagen.id3 import USLT
        from mutagen.mp3 import MP3
        audio = MP3(path)
        if audio.tags is None:
            audio.add_tags()
        audio.tags.delall("USLT")
        audio.tags.add(USLT(encoding=3, lang="chi", desc="", text=lrc))
        audio.save()
    elif ext in (".flac", ".ogg"):
        if ext == ".flac":
            from mutagen.flac import FLAC
            audio = FLAC(path)
        else:
            from mutagen.oggvorbis import OggVorbis
            audio = OggVorbis(path)
        if audio.tags is None:
            audio.add_tags()
        _write_vorbis_lyrics(audio.tags, lrc)
        audio.save()
    elif ext == ".ape":
        from mutagen.monkeysaudio import MonkeysAudio
        audio = MonkeysAudio(path)
        if audio.tags is None:
            audio.add_tags()
        audio.tags["Lyrics"] = lrc
        audio.save()
    else:
        raise ValueError(f"不支持的音频格式: {ext}")


def read_picture(path: str) -> Optional[bytes]:
    """读取封面图字节；无封面返回 None。"""
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".flac":
            from mutagen.flac import FLAC
            pics = FLAC(path).pictures
            return pics[0].data if pics else None
        if ext == ".mp3":
            from mutagen.id3 import ID3
            apics = ID3(path).getall("APIC")
            return apics[0].data if apics else None
        if ext == ".ogg":
            import base64
            from mutagen.flac import Picture
            from mutagen.oggvorbis import OggVorbis
            tags = OggVorbis(path).tags
            if tags is None or "metadata_block_picture" not in tags:
                return None
            pic = Picture(base64.b64decode(tags["metadata_block_picture"][0]))
            return pic.data
        if ext == ".ape":
            from mutagen.monkeysaudio import MonkeysAudio
            tags = MonkeysAudio(path).tags
            if tags is None or "Cover Art (Front)" not in tags:
                return None
            value = bytes(tags["Cover Art (Front)"].value)
            # 跳过开头的 文件名\x00
            idx = value.find(b"\x00")
            return value[idx + 1:] if idx >= 0 else value
    except Exception:
        return None
    return None


def read_lyrics(path: str) -> str:
    """读取 LRC 歌词；无则返回空串。"""
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".mp3":
            from mutagen.id3 import ID3
            uslts = ID3(path).getall("USLT")
            return str(uslts[0].text) if uslts else ""
        if ext in (".flac", ".ogg"):
            if ext == ".flac":
                from mutagen.flac import FLAC
                tags = FLAC(path).tags
            else:
                from mutagen.oggvorbis import OggVorbis
                tags = OggVorbis(path).tags
            if tags is None:
                return ""
            # LYRICS 为主（大小写不敏感，能同时命中旧版写入的小写 lyrics）；
            # 只有 UNSYNCEDLYRICS 的文件（别的工具写的）也认。
            for key in ("lyrics", "unsyncedlyrics", "unsynced lyrics"):
                try:
                    vals = tags.get(key)
                except Exception:
                    vals = None
                if vals:
                    return str(vals[0]) if isinstance(vals, (list, tuple)) else str(vals)
            return ""
        if ext == ".ape":
            from mutagen.monkeysaudio import MonkeysAudio
            tags = MonkeysAudio(path).tags
            if tags is None:
                return ""
            return str(tags.get("Lyrics", ""))
    except Exception:
        return ""
    return ""


def _get(tags: dict, key: str) -> str:
    """从 read_tags() 返回的字典取值。"""
    val = tags.get(key)
    if isinstance(val, (list, tuple)):
        return str(val[0]) if val else ""
    return str(val or "")


# 各格式标签键 -> 规范键（read_tags 输出统一用小写规范键；查找时键也转小写）
_CANONICAL_KEYS = {
    # MP3 ID3v2 帧
    "tit2": "title", "tpe1": "artist", "talb": "album", "tpe2": "album_artist",
    "tdrc": "date", "tyer": "year", "tcon": "genre", "trck": "track",
    "tpos": "disc", "comm": "comment",
    # VorbisComment / APEv2（原样小写后映射）
    "title": "title", "artist": "artist", "album": "album",
    "albumartist": "album_artist", "album artist": "album_artist",
    "date": "date", "year": "year", "genre": "genre",
    "tracknumber": "track", "tracktotal": "track_total",
    "discnumber": "disc", "disctotal": "disc_total",
    "track": "track", "disc": "disc", "comment": "comment",
    "language": "language", "lyricist": "lyricist", "composer": "composer",
    "publisher": "publisher", "tpub": "publisher", "tlan": "language",
    "organization": "publisher", "label": "publisher",
    # 歌词 / 封面
    "lyrics": "lyrics", "unsyncedlyrics": "lyrics", "unsynced lyrics": "lyrics",
    "uslt": "lyrics",
    "metadata_block_picture": "picture", "apic": "picture",
    "cover art (front)": "picture",
}


def _fmt_tag_value(value) -> str:
    """把标签值转为字符串；兼容 list/tuple 与单值（APEv2 常见）。"""
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value)
    return str(value)


def read_tags(path: str) -> Dict[str, str]:
    """读取标签为规范键 dict（title/artist/album/date/genre/track...），
    主要用于验证与日志。"""
    ext = os.path.splitext(path)[1].lower()
    out: Dict[str, str] = {}
    try:
        if ext == ".flac":
            from mutagen.flac import FLAC
            tags = FLAC(path).tags
        elif ext == ".ogg":
            from mutagen.oggvorbis import OggVorbis
            tags = OggVorbis(path).tags
        elif ext == ".mp3":
            from mutagen.id3 import ID3
            tags = ID3(path)
        elif ext == ".ape":
            from mutagen.monkeysaudio import MonkeysAudio
            tags = MonkeysAudio(path).tags
        else:
            return out
        if tags is None:
            return out
        for key, value in tags.items():
            norm = _CANONICAL_KEYS.get(str(key).lower(), str(key).lower())
            if norm not in out:  # 同键多值取第一个
                out[norm] = _fmt_tag_value(value)
        # 优先取精确的 date（如 QQ 专辑日期），YEAR 只作兜底
        if "date" not in out and "year" in out:
            out["date"] = out["year"]
        # mp3/ape 的曲目号是 "7/12" 形式（写入时也是这么合并的），这里拆回两个字段，
        # 与 flac/ogg 的「曲目号 / 总曲目」读法保持一致
        track = out.get("track") or ""
        if "/" in track:
            head, _, tail = track.partition("/")
            out["track"] = head.strip()
            if tail.strip():
                out.setdefault("track_total", tail.strip())
    except Exception:
        return {}
    return out
