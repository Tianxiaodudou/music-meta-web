# -*- coding: utf-8 -*-
"""用 mutagen 把 SongMeta 写入 flac / mp3 / ogg / ape 文件。

字段映射：
- FLAC / OGG (VorbisComment): title, artist, album, albumartist, date,
  genre, tracknumber, tracktotal, discnumber, comment
- MP3 (ID3v2.4): TIT2, TPE1, TALB, TPE2, TDRC, TCON, TRCK(3/11), TPOS, COMM
- APE (APEv2): Title, Artist, Album, Album Artist, Date, Genre, Track(3/11), Comment

只写入非空字段；已有标签会被更新，不会删除其它字段。
"""
from __future__ import annotations

import os
from typing import Dict, Optional

from .sources.base import SongMeta


def _vorbis_fields(meta: SongMeta) -> Dict[str, str]:
    """VorbisComment 字段（flac / ogg 共用）。"""
    fields: Dict[str, str] = {}
    if meta.title:
        fields["title"] = meta.title
    if meta.artist:
        fields["artist"] = meta.artist
    if meta.album:
        fields["album"] = meta.album
    if meta.album_artist:
        fields["albumartist"] = meta.album_artist
    if meta.date:
        fields["date"] = meta.date
    if meta.genre:
        fields["genre"] = meta.genre
    if meta.track:
        fields["tracknumber"] = meta.track
    if meta.track_total:
        fields["tracktotal"] = meta.track_total
    if meta.disc:
        fields["discnumber"] = meta.disc
    if meta.publisher:
        fields["publisher"] = meta.publisher
    if meta.language:
        fields["language"] = meta.language
    if meta.comment:
        fields["comment"] = meta.comment
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
    from mutagen.id3 import (COMM, TALB, TCON, TDRC, TIT2, TLAN, TPE1, TPE2,
                             TPOS, TPUB, TRCK)

    if audio.tags is None:
        audio.add_tags()
    tags = audio.tags
    enc = 3  # UTF-8
    if meta.title:
        tags.add(TIT2(encoding=enc, text=[meta.title]))
    if meta.artist:
        tags.add(TPE1(encoding=enc, text=[meta.artist]))
    if meta.album:
        tags.add(TALB(encoding=enc, text=[meta.album]))
    if meta.album_artist:
        tags.add(TPE2(encoding=enc, text=[meta.album_artist]))
    if meta.date:
        tags.add(TDRC(encoding=enc, text=[meta.date]))
    if meta.genre:
        tags.add(TCON(encoding=enc, text=[meta.genre]))
    track = _track_str(meta)
    if track:
        tags.add(TRCK(encoding=enc, text=[track]))
    if meta.disc:
        tags.add(TPOS(encoding=enc, text=[meta.disc]))
    if meta.publisher:
        tags.add(TPUB(encoding=enc, text=[meta.publisher]))
    if meta.language:
        tags.add(TLAN(encoding=enc, text=[meta.language]))
    if meta.comment:
        tags.add(COMM(encoding=enc, lang="chi", desc="", text=[meta.comment]))
    audio.save()


def _write_ape(audio, meta: SongMeta) -> None:
    if audio.tags is None:
        audio.add_tags()
    tags = audio.tags
    if meta.title:
        tags["Title"] = meta.title
    if meta.artist:
        tags["Artist"] = meta.artist
    if meta.album:
        tags["Album"] = meta.album
    if meta.album_artist:
        tags["Album Artist"] = meta.album_artist
    if meta.date:
        tags["Date"] = meta.date
    if meta.genre:
        tags["Genre"] = meta.genre
    track = _track_str(meta)
    if track:
        tags["Track"] = track
    if meta.publisher:
        tags["Publisher"] = meta.publisher
    if meta.language:
        tags["Language"] = meta.language
    if meta.comment:
        tags["Comment"] = meta.comment
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
        audio.tags["lyrics"] = lrc
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
            vals = tags.get("lyrics") or []
            return str(vals[0]) if vals else ""
        if ext == ".ape":
            from mutagen.monkeysaudio import MonkeysAudio
            tags = MonkeysAudio(path).tags
            if tags is None:
                return ""
            return str(tags.get("Lyrics", ""))
    except Exception:
        return ""
    return ""


def has_metadata(path: str) -> bool:
    """文件是否已写入标题与歌手（用于幂等跳过）。"""
    try:
        tags = read_tags(path)
    except Exception:
        return False
    title = _get(tags, "title")
    artist = _get(tags, "artist")
    return bool(title and artist)


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
    "lyrics": "lyrics", "uslt": "lyrics",
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
    except Exception:
        return {}
    return out
