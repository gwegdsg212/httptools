#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HTML 内嵌 Base64 图片批量替换工具（图形界面）

支持：
- 普通 HTML 中的 data:image/...;base64,...
- window.__adapter_zip__：多段 <script data-id="adapter-zip-N"> 内 Base64 负载，
  先解码再按 gzip 或 zlib 解压为 JSON，替换后再压回并写回原 script 区域。
- window.__zip（Cocos 等）：Base64 解码后为 ZIP 包。包内图片替换**仅作用于 __res 与 assets 下 .json**，
  不会改写 cocos-js 等引擎脚本（避免误替换 data:image 片段导致白屏）。
- 代码精确替换：在 adapter 解压后的 JSON 与整份 HTML 中，按原文查找子串并全部替换为新内容。
- 可将 HTML 内嵌的 adapter 字段导出为 ZIP 或 JSON 文件（「导出 HTML 内嵌 ZIP」）。
- 可将 HTML 中的图片与 MP3 等资源批量导出（「导出图片/MP3」）；内嵌 ZIP 会先解压再提取。

用法：python html_base64_image_replacer.py
依赖：Python 3.8+，tkinter（Windows/macOS 通常自带）
Windows 下拖入图片到「新图片」框需安装：pip install windnd（无则仍可用「选择图片文件」按钮）
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import io
import json
import mimetypes
import os
import re
import tkinter as tk
import zipfile
import zlib
from dataclasses import dataclass
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable, List, Optional, Tuple

try:
    import windnd  # type: ignore

    _HAS_WINDND = True
except ImportError:
    windnd = None  # type: ignore
    _HAS_WINDND = False

try:
    from PIL import Image as _PILImage, ImageTk as _PILImageTk  # type: ignore

    _HAS_PIL = True
except ImportError:
    _PILImage = None  # type: ignore
    _PILImageTk = None  # type: ignore
    _HAS_PIL = False


# 匹配 data URL 中的 base64 段（双引号或单引号属性内）
_DATA_URL_RE = re.compile(
    r'(data:image/[\w.+-]+;base64,)([^"\'>\s]+)',
    re.IGNORECASE,
)

# Cocos / 适配器：多段 script 拼接的 adapter 字段
# 预设字段名（按优先级），auto-detect 时会先尝试这些
_ADAPTER_FIELD_PRESETS = [
    "window.__adapter_zip__",
    "window.__zip",
    "window.__zip__",
    "window.__resources__",
    "window.__bundle__",
]

# 扫描 HTML 中疑似 adapter 赋值的字段名（保留原始大小写）
_ADAPTER_FIELD_SCAN_RE = re.compile(
    r'(window\s*\.\s*__\w+__?)\s*\+?=\s*["\'][A-Za-z0-9+/=\s]{64,}',
)

# 与常见导出 HTML 一致，避免单段字符串超过 JS 引擎限制
_ADAPTER_B64_CHUNK_SIZE = 1677722


def _build_adapter_script_re() -> Any:
    """匹配 data-id=\"adapter-zip-N\" 分片 script 块。"""
    return re.compile(
        r'<script[^>]*\bdata-id="adapter-zip-(\d+)"[^>]*>(.*?)</script>',
        re.IGNORECASE | re.DOTALL,
    )


@dataclass
class _AdapterLiteral:
    """script 内一处 adapter 字段赋值。"""

    op: str  # "=" 或 "+="
    b64: str
    quote_char: str
    start: int
    end: int


def _extract_adapter_literals(body: str, field_name: str) -> List[_AdapterLiteral]:
    """
    从 script 正文中提取 field_name 的赋值（避免对 MB 级 Base64 使用正则）。
    支持 window.__adapter_zip__ = "..." 与 window.__adapter_zip__ += "..." 。
    """
    results: List[_AdapterLiteral] = []
    if not body or not field_name:
        return results

    body_lower = body.lower()
    field_lower = field_name.lower()
    pos = 0
    while True:
        idx = body_lower.find(field_lower, pos)
        if idx < 0:
            break
        j = idx + len(field_name)
        while j < len(body) and body[j] in " \t\r\n":
            j += 1
        op = "="
        if j + 1 < len(body) and body[j] == "+" and body[j + 1] == "=":
            op = "+="
            j += 2
        elif j < len(body) and body[j] == "=":
            j += 1
        else:
            pos = idx + 1
            continue
        while j < len(body) and body[j] in " \t\r\n":
            j += 1
        if j >= len(body) or body[j] not in "\"'":
            pos = idx + 1
            continue
        quote = body[j]
        j += 1
        start_b64 = j
        while j < len(body) and body[j] != quote:
            j += 1
        if j >= len(body):
            break
        b64 = body[start_b64:j]
        b64_clean = "".join(c for c in b64 if c not in " \t\r\n")
        if b64_clean and all(c in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=" for c in b64_clean):
            results.append(
                _AdapterLiteral(
                    op=op,
                    b64=b64_clean,
                    quote_char=quote,
                    start=idx,
                    end=j + 1,
                )
            )
        pos = j + 1
    return results


def _merge_adapter_b64_literals(literals: List[_AdapterLiteral]) -> str:
    """按 JS 执行顺序合并：= 覆盖，+= 追加。"""
    current = ""
    for lit in literals:
        if lit.op == "+=":
            current += lit.b64
        else:
            current = lit.b64
    return current


def _decode_adapter_b64(b64_full: str) -> Optional[bytes]:
    if not b64_full:
        return None
    try:
        return base64.b64decode(b64_full, validate=False)
    except Exception:
        return None

_IMAGE_EXTS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".bmp",
    ".ico",
    ".svg",
    ".jfif",
    ".avif",
}

_EXT_MIME_FALLBACK = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".jfif": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".ico": "image/x-icon",
    ".svg": "image/svg+xml",
    ".avif": "image/avif",
}

_AUDIO_EXTS = {".mp3", ".mpeg", ".mpga", ".wav", ".ogg", ".m4a", ".aac", ".mp4"}

# Cocos 2.x 单文件 HTML 常见：window.resMap 内为纯 Base64（无 data: 前缀）
_RES_MAP_FIELD_NAMES = ["window.resMap", "window.__resMap__"]

_MIME_EXT_FALLBACK = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
    "image/x-icon": ".ico",
    "image/svg+xml": ".svg",
    "image/avif": ".avif",
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/x-mpeg": ".mp3",
    "audio/mp4": ".m4a",
    "audio/wav": ".wav",
    "audio/wave": ".wav",
    "audio/x-wav": ".wav",
    "audio/vnd.wave": ".wav",
    "audio/ogg": ".ogg",
    "audio/webm": ".webm",
    "audio/aac": ".aac",
    "audio/x-m4a": ".m4a",
}


@dataclass
class MediaExportStats:
    images: int = 0
    audio: int = 0
    duplicates: int = 0


class _MediaExporter:
    """将图片/音频写入目标目录，按内容哈希去重。"""

    def __init__(self, out_dir: str) -> None:
        self.images_dir = os.path.join(out_dir, "images")
        self.audio_dir = os.path.join(out_dir, "audio")
        os.makedirs(self.images_dir, exist_ok=True)
        os.makedirs(self.audio_dir, exist_ok=True)
        self._seen: set[str] = set()
        self.stats = MediaExportStats()
        self._img_seq = 0
        self._aud_seq = 0

    def _digest(self, data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()[:16]

    def _unique_path(self, folder: str, base: str, ext: str) -> str:
        ext = ext if ext.startswith(".") else f".{ext}"
        candidate = os.path.join(folder, f"{base}{ext}")
        if not os.path.exists(candidate):
            return candidate
        n = 2
        while True:
            candidate = os.path.join(folder, f"{base}_{n}{ext}")
            if not os.path.exists(candidate):
                return candidate
            n += 1

    def save_image(self, data: bytes, hint: str = "") -> bool:
        if not data or not _validate_image_bytes(data):
            return False
        digest = self._digest(data)
        if digest in self._seen:
            self.stats.duplicates += 1
            return False
        self._seen.add(digest)
        ext = _ext_from_bytes_or_hint(data, hint, kind="image")
        base = _sanitize_export_basename(hint) or f"image_{self._img_seq:04d}"
        self._img_seq += 1
        path = self._unique_path(self.images_dir, base, ext)
        with open(path, "wb") as f:
            f.write(data)
        self.stats.images += 1
        return True

    def save_audio(self, data: bytes, hint: str = "") -> bool:
        if not data or not _validate_audio_bytes(data):
            return False
        digest = self._digest(data)
        if digest in self._seen:
            self.stats.duplicates += 1
            return False
        self._seen.add(digest)
        ext = _ext_from_bytes_or_hint(data, hint, kind="audio")
        if ext not in _AUDIO_EXTS and ext != ".m4a":
            # 用户要求 MP3；非 mp3 扩展的音频仍导出，但优先 .mp3 命名
            if ext in (".wav", ".ogg", ".webm", ".aac", ".m4a"):
                pass
            else:
                ext = ".mp3"
        base = _sanitize_export_basename(hint) or f"audio_{self._aud_seq:04d}"
        self._aud_seq += 1
        path = self._unique_path(self.audio_dir, base, ext)
        with open(path, "wb") as f:
            f.write(data)
        self.stats.audio += 1
        return True


def _sanitize_export_basename(name: str) -> str:
    if not name:
        return ""
    base = os.path.basename(name.replace("\\", "/").strip("/"))
    base, _ = os.path.splitext(base)
    base = re.sub(r'[<>:"|?*\x00-\x1f]', "_", base).strip(" .")
    return base[:120] if base else ""


def _ext_from_mime(mime: str) -> str:
    mime = mime.lower().split(";", 1)[0].strip()
    return _MIME_EXT_FALLBACK.get(mime, mimetypes.guess_extension(mime) or "")


def _ext_from_bytes_or_hint(data: bytes, hint: str, *, kind: str) -> str:
    hint_ext = os.path.splitext(hint.replace("\\", "/"))[1].lower()
    if hint_ext:
        if kind == "image" and hint_ext in _IMAGE_EXTS:
            return hint_ext
        if kind == "audio" and hint_ext in _AUDIO_EXTS.union({".wav", ".ogg", ".m4a", ".aac"}):
            return hint_ext
    if len(data) >= 4 and data[:4] == b"\x89PNG":
        return ".png"
    if len(data) >= 3 and data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if len(data) >= 6 and data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if len(data) >= 3 and data[:3] == b"ID3":
        return ".mp3"
    if len(data) >= 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0:
        return ".mp3"
    return ".png" if kind == "image" else ".mp3"


def _is_b64_char(ch: str) -> bool:
    return ch.isalnum() or ch in "+/=-_"


def _decode_b64_flexible(b64_text: str) -> Optional[bytes]:
    """标准 / URL-safe Base64 均可解码。"""
    b64_clean = re.sub(r"\s+", "", b64_text)
    if not b64_clean:
        return None
    pad = (-len(b64_clean)) % 4
    if pad:
        b64_clean += "=" * pad
    try:
        return base64.b64decode(b64_clean, validate=False)
    except Exception:
        pass
    try:
        return base64.urlsafe_b64decode(b64_clean)
    except Exception:
        pass
    return None


# 部分游戏（Cocos 等）会在 base64 前几位插入 1 个混淆字符，导致 len(b64) % 4 == 1。
# 经验范围：图片插入在 index 3，音频插入在 index 2；统一在 0..7 范围内尝试。
_DEOBF_SCAN_RANGE = 8


def _decode_b64_with_offset(
    b64_text: str,
    validators: Tuple[Callable[[bytes], bool], ...],
) -> Tuple[Optional[bytes], int]:
    """
    返回 (解码字节, 混淆字符插入位置)。
    offset == -1 表示无混淆（标准 base64 直接解码即通过校验）。
    """
    standard = _decode_b64_flexible(b64_text)
    if standard is not None and any(v(standard) for v in validators):
        return standard, -1

    b64_clean = re.sub(r"\s+", "", b64_text)
    if len(b64_clean) % 4 == 1:
        upper = min(_DEOBF_SCAN_RANGE, len(b64_clean))
        for i in range(upper):
            candidate = b64_clean[:i] + b64_clean[i + 1 :]
            try:
                raw = base64.b64decode(candidate, validate=False)
            except Exception:
                continue
            if any(v(raw) for v in validators):
                return raw, i
    return standard, -1


def _re_obfuscate_b64(b64: str, offset: int, *, fill_char: str = "A") -> str:
    """在 base64 的 offset 位置插入 1 个无效混淆字符，与原资源格式保持一致。"""
    if offset < 0 or offset > len(b64):
        return b64
    return b64[:offset] + fill_char + b64[offset:]


def _decode_b64_with_deobfuscation(
    b64_text: str,
    validators: Tuple[Callable[[bytes], bool], ...],
) -> Optional[bytes]:
    """
    解码 base64。若标准解码后无法通过任一 validator，且长度 mod 4 == 1，
    尝试在前 8 位删除 1 个字符再解码——还原资源被插入 1 个混淆字符的情形。
    成功返回有效字节；否则返回标准解码结果（可能为 None）。
    """
    standard = _decode_b64_flexible(b64_text)
    if standard is not None and any(v(standard) for v in validators):
        return standard

    b64_clean = re.sub(r"\s+", "", b64_text)
    if len(b64_clean) % 4 == 1:
        upper = min(_DEOBF_SCAN_RANGE, len(b64_clean))
        for i in range(upper):
            candidate = b64_clean[:i] + b64_clean[i + 1 :]
            try:
                raw = base64.b64decode(candidate, validate=False)
            except Exception:
                continue
            if any(v(raw) for v in validators):
                return raw

    return standard


def _validate_image_bytes(data: bytes) -> bool:
    if len(data) < 12:
        return False
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return len(data) >= 24 and data[12:16] == b"IHDR"
    if data[:3] == b"\xff\xd8\xff":
        return True
    if len(data) >= 6 and data[:6] in (b"GIF87a", b"GIF89a"):
        return True
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return True
    if data[:2] == b"BM":
        return True
    return False


def _validate_audio_bytes_strict(data: bytes) -> bool:
    """严格校验：只看文件起始 magic bytes，避免 MP3 sync 帧深扫描造成的误判。"""
    if len(data) < 4:
        return False
    if data[:3] == b"ID3":
        return True
    if data[:4] == b"RIFF" and len(data) >= 12 and data[8:12] == b"WAVE":
        return True
    if data[:4] == b"OggS" or data[:4] == b"fLaC":
        return True
    if data[0] == 0xFF and (data[1] & 0xE0) == 0xE0:
        return True
    # ftyp box（M4A / MP4 audio）
    if len(data) >= 12 and data[4:8] == b"ftyp":
        return True
    return False


def _validate_audio_bytes(data: bytes) -> bool:
    if _validate_audio_bytes_strict(data):
        return True
    # 容错：MP3 数据可能前置任意字节，扫描前 16KB 找 sync 帧
    if len(data) < 4:
        return False
    scan = min(len(data) - 1, 16384)
    for i in range(scan):
        if data[i] == 0xFF and (data[i + 1] & 0xE0) == 0xE0:
            return True
    return False


def _validators_for_mime(mime: str) -> Tuple[Callable[[bytes], bool], ...]:
    """根据 MIME 选择**严格**校验器（用于去混淆解码时的命中判定，要求 magic bytes 在起始位置）。"""
    m = mime.lower()
    if m.startswith("image/"):
        return (_validate_image_bytes,)
    if m.startswith("audio/"):
        return (_validate_audio_bytes_strict,)
    return ()


def _decode_b64_for_mime(b64_text: str, mime: str) -> Optional[bytes]:
    validators = _validators_for_mime(mime)
    if validators:
        return _decode_b64_with_deobfuscation(b64_text, validators)
    return _decode_b64_flexible(b64_text)


def _parse_data_url_value(val: str) -> Optional[Tuple[str, bytes]]:
    """解析 data:image/... 或 data:audio/...;base64,... 返回 (mime, bytes)。"""
    low = val.lower()
    if not low.startswith("data:") or ";base64," not in low:
        return None
    marker = ";base64,"
    i = low.find(marker)
    if i < 0:
        return None
    mime = val[5:i].lower()
    b64_part = val[i + len(marker) :]
    raw = _decode_b64_for_mime(b64_part, mime)
    if not raw:
        return None
    return mime, raw


def _iter_data_urls_in_text(text: str) -> List[Tuple[str, str, bytes]]:
    """扫描文本中完整 data:image / data:audio URL（支持 URL-safe Base64 与轻量插字符混淆）。"""
    found: List[Tuple[str, str, bytes]] = []
    lower = text.lower()
    pos = 0
    while pos < len(text):
        i_img = lower.find("data:image/", pos)
        i_aud = lower.find("data:audio/", pos)
        candidates = [i for i in (i_img, i_aud) if i >= 0]
        if not candidates:
            break
        idx = min(candidates)
        semi = lower.find(";base64,", idx)
        if semi < 0:
            pos = idx + 1
            continue
        mime = text[idx + 5 : semi].lower()
        j = semi + len(";base64,")
        start_b64 = j
        while j < len(text) and (_is_b64_char(text[j]) or text[j].isspace()):
            j += 1
        raw = _decode_b64_for_mime(text[start_b64:j], mime)
        if raw:
            found.append((mime, text[idx:j], raw))
        pos = max(j, idx + 1)
    return found


def _export_data_urls_from_text(exporter: _MediaExporter, text: str, name_hint: str = "") -> None:
    for mime, _span, raw in _iter_data_urls_in_text(text):
        hint = name_hint or mime.replace("/", "_")
        if mime.startswith("image/"):
            exporter.save_image(raw, hint)
        elif mime.startswith("audio/"):
            # name_hint 含扩展名时直接使用；否则附加 MIME 推断的扩展，最后 save_audio 还会基于字节再校正。
            hint_ext = os.path.splitext(hint)[1].lower()
            if hint_ext in _AUDIO_EXTS:
                final_hint = hint
            else:
                ext = _ext_from_mime(mime) or ".mp3"
                final_hint = f"{hint}{ext}"
            exporter.save_audio(raw, final_hint)


def _export_resource_map_dict(exporter: _MediaExporter, obj: dict) -> None:
    """
    从资源映射表导出：支持
    - data:image/...;base64,... / data:audio/...
    - Cocos resMap / __res 常见的纯 Base64（键带 .png / .mp3 等扩展名）
    """
    if not isinstance(obj, dict):
        return
    for key, val in obj.items():
        if not isinstance(val, str) or not val.strip():
            continue
        key_s = str(key).replace("\\", "/")
        key_low = key_s.lower()

        parsed = _parse_data_url_value(val)
        if parsed:
            mime, raw = parsed
            if mime.startswith("image/"):
                exporter.save_image(raw, key_s)
            elif mime.startswith("audio/"):
                exporter.save_audio(raw, key_s)
            continue

        ext = os.path.splitext(key_low)[1]
        if ext in _IMAGE_EXTS:
            raw = _decode_b64_with_deobfuscation(val, (_validate_image_bytes,))
            if raw:
                exporter.save_image(raw, key_s)
        elif ext in _AUDIO_EXTS:
            raw = _decode_b64_with_deobfuscation(val, (_validate_audio_bytes_strict,))
            if raw:
                exporter.save_audio(raw, key_s)


def _export_resource_map_from_text(exporter: _MediaExporter, text: str) -> None:
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return
    _export_resource_map_dict(exporter, obj)


def _parse_js_json_object_at(html: str, start: int) -> Optional[Tuple[Any, int]]:
    i = start
    while i < len(html) and html[i].isspace():
        i += 1
    if i >= len(html) or html[i] != "{":
        return None
    try:
        obj, end = json.JSONDecoder().raw_decode(html, i)
        return obj, end
    except json.JSONDecodeError:
        return None


def _extract_js_object_assignments(html: str, field_names: List[str]) -> List[dict]:
    """从 HTML/JS 中提取 window.resMap = {...} 等对象赋值。"""
    results: List[dict] = []
    html_lower = html.lower()
    for field_name in field_names:
        name_lower = field_name.lower()
        pos = 0
        while True:
            idx = html_lower.find(name_lower, pos)
            if idx < 0:
                break
            j = idx + len(field_name)
            while j < len(html) and html[j].isspace():
                j += 1
            if j >= len(html) or html[j] != "=":
                pos = idx + 1
                continue
            j += 1
            while j < len(html) and html[j].isspace():
                j += 1
            parsed = _parse_js_json_object_at(html, j)
            if parsed and isinstance(parsed[0], dict):
                results.append(parsed[0])
                pos = parsed[1]
            else:
                pos = idx + 1
    return results


def _export_res_maps_from_html(exporter: _MediaExporter, html_text: str) -> None:
    for obj in _extract_js_object_assignments(html_text, _RES_MAP_FIELD_NAMES):
        _export_resource_map_dict(exporter, obj)


def _export_from_zip_bytes(
    exporter: _MediaExporter,
    archive_bytes: bytes,
    path_prefix: str = "",
) -> None:
    """解压 ZIP（含嵌套 ZIP），导出其中图片与 MP3/音频。"""
    try:
        zf = zipfile.ZipFile(io.BytesIO(archive_bytes), "r")
    except zipfile.BadZipFile:
        return
    with zf:
        if zf.testzip() is not None:
            return
        for info in zf.infolist():
            if info.is_dir():
                continue
            arc = info.filename.replace("\\", "/")
            lower = arc.lower()
            data = zf.read(info.filename)

            if _looks_like_zip_archive(data):
                nested_prefix = os.path.join(path_prefix, arc + "_unzipped") if path_prefix else arc + "_unzipped"
                _export_from_zip_bytes(exporter, data, nested_prefix)
                continue

            rel_hint = os.path.join(path_prefix, arc) if path_prefix else arc

            if any(lower.endswith(ext) for ext in _IMAGE_EXTS):
                if _validate_image_bytes(data):
                    exporter.save_image(data, rel_hint)
                else:
                    # 条目可能是文本形式的 data URL（Cocos 等会做轻量混淆）
                    try:
                        _export_data_urls_from_text(exporter, data.decode("utf-8"), rel_hint)
                    except UnicodeDecodeError:
                        pass
                continue

            if any(lower.endswith(ext) for ext in _AUDIO_EXTS):
                if _validate_audio_bytes(data):
                    exporter.save_audio(data, rel_hint)
                else:
                    try:
                        _export_data_urls_from_text(exporter, data.decode("utf-8"), rel_hint)
                    except UnicodeDecodeError:
                        pass
                continue

            if arc.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1].lower() == "__res":
                try:
                    text = data.decode("utf-8")
                except UnicodeDecodeError:
                    if _validate_image_bytes(data):
                        exporter.save_image(data, rel_hint)
                    elif _validate_audio_bytes(data):
                        exporter.save_audio(data, rel_hint)
                    continue
                _export_resource_map_from_text(exporter, text)
                continue

            if lower.endswith(".json"):
                try:
                    text = data.decode("utf-8")
                except UnicodeDecodeError:
                    continue
                _export_resource_map_from_text(exporter, text)
                continue

            if lower.endswith(".js"):
                try:
                    text = data.decode("utf-8")
                except UnicodeDecodeError:
                    continue
                for obj in _extract_js_object_assignments(text, _RES_MAP_FIELD_NAMES):
                    _export_resource_map_dict(exporter, obj)
                _export_data_urls_from_text(exporter, text, rel_hint)
                continue

            if not os.path.splitext(lower)[1]:
                if _validate_image_bytes(data):
                    exporter.save_image(data, rel_hint)
                elif _validate_audio_bytes(data):
                    exporter.save_audio(data, rel_hint)
                else:
                    try:
                        text = data.decode("utf-8")
                    except UnicodeDecodeError:
                        continue
                    _export_resource_map_from_text(exporter, text)


def export_html_images_and_mp3(
    html_text: str,
    out_dir: str,
    source_name: str,
    field_names: Optional[List[str]] = None,
) -> MediaExportStats:
    """
    从 HTML 导出图片与 MP3（及常见音频）资源。

    - 扫描整份 HTML 中的 data:image / data:audio Base64。
    - 对 adapter 字段（__adapter_zip__ / __zip 等）：Base64 解码；若为 ZIP 则解压（含嵌套 ZIP）后导出；
      若为 zlib/gzip JSON 则扫描其中 data URL 与 __res 结构。
    """
    os.makedirs(out_dir, exist_ok=True)
    exporter = _MediaExporter(out_dir)

    _export_res_maps_from_html(exporter, html_text)
    _export_data_urls_from_text(exporter, html_text, f"{source_name}_inline")

    names = field_names if field_names else list(_ADAPTER_FIELD_PRESETS)
    seen_fields: set[str] = set()
    for field_name in names:
        if not field_name or field_name in seen_fields:
            continue
        seen_fields.add(field_name)
        bundle = find_adapter_zip_bundle(html_text, field_name)
        if not bundle:
            continue
        tag = field_name.replace("window.", "").replace(".", "_")
        if bundle.archive_bytes is not None:
            _export_from_zip_bytes(exporter, bundle.archive_bytes, tag)
        elif bundle.json_text:
            _export_resource_map_from_text(exporter, bundle.json_text)
            _export_data_urls_from_text(exporter, bundle.json_text, tag)

    return exporter.stats


def _decode_windnd_path(item: Any) -> str:
    if isinstance(item, bytes):
        for enc in ("utf-8", "mbcs", "gbk"):
            try:
                return item.decode(enc)
            except UnicodeDecodeError:
                continue
        return item.decode("utf-8", errors="replace")
    return str(item)


def _guess_image_mime(path: str) -> Optional[str]:
    ext = os.path.splitext(path)[1].lower()
    guessed, _ = mimetypes.guess_type(path)
    if guessed and guessed.startswith("image/"):
        return guessed
    return _EXT_MIME_FALLBACK.get(ext)


def decode_html_adapter_zip(html_text: str, decoded_dir: str, source_name: str, field_name: str) -> int:
    bundle = find_adapter_zip_bundle(html_text, field_name)
    if not bundle:
        return 0
    safe_name = source_name.replace(" ", "_").replace("/", "_")
    clean_field = field_name.replace("window.", "").replace(".", "_")
    os.makedirs(decoded_dir, exist_ok=True)
    if bundle.archive_bytes is not None:
        zip_path = os.path.join(decoded_dir, f"{safe_name}.{clean_field}.zip")
        with open(zip_path, "wb") as f:
            f.write(bundle.archive_bytes)
        extract_dir = os.path.join(decoded_dir, f"{safe_name}.{clean_field}_extracted")
        os.makedirs(extract_dir, exist_ok=True)
        with zipfile.ZipFile(io.BytesIO(bundle.archive_bytes), "r") as zf:
            zf.extractall(extract_dir)
        return 1
    out_name = f"{safe_name}.{clean_field}.json"
    out_path = os.path.join(decoded_dir, out_name)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(bundle.json_text)
    return 1


def image_file_to_data_url(path: str) -> Tuple[str, Optional[str]]:
    """
    读取本地图片文件，生成 data:image/...;base64,... 字符串。
    成功返回 (data_url, None)，失败返回 ("", 错误说明)。
    """
    path = path.strip().strip('"').strip("'")
    if not path or not os.path.isfile(path):
        return "", "不是有效的文件路径。"

    mime = _guess_image_mime(path)
    if not mime:
        return "", "无法根据扩展名识别为图片，请使用常见图片格式。"

    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError as e:
        return "", f"无法读取文件：{e}"

    if not raw:
        return "", "文件为空。"

    b64 = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{b64}", None


@dataclass
class AdapterZipBundle:
    """HTML 中一段连续的 adapter-zip script 块，或直接赋值字段。"""

    block_start: int
    block_end: int
    json_text: str
    compress_mode: str  # "zlib" | "gzip" | "zip"
    format: str  # "split" | "direct"
    quote_char: str = '"'
    archive_bytes: Optional[bytes] = None  # Cocos 等：整包 ZIP；否则为 zlib/gzip 解压得到的 JSON 文本


def _decompress_payload(raw: bytes) -> Tuple[bytes, str]:
    """解压 adapter 二进制，返回 (解压后字节, 压缩方式标记用于回压)。"""
    if len(raw) >= 2 and raw[0] == 0x1F and raw[1] == 0x8B:
        return gzip.decompress(raw), "gzip"
    # 常见为 zlib（如 0x78 0x9c）
    try:
        return zlib.decompress(raw), "zlib"
    except zlib.error:
        pass
    try:
        return zlib.decompress(raw, wbits=-15), "zlib"
    except zlib.error:
        pass
    return gzip.decompress(raw), "gzip"


def _compress_payload(raw: bytes, mode: str) -> bytes:
    if mode == "gzip":
        return gzip.compress(raw, compresslevel=9)
    return zlib.compress(raw, level=zlib.Z_DEFAULT_COMPRESSION)


def _looks_like_zip_archive(raw: bytes) -> bool:
    """标准 ZIP 本地文件头：PK\\x03\\x04"""
    return len(raw) >= 4 and raw[:4] == b"PK\x03\x04"


def _adapter_bundle_from_binary(
    binary: bytes,
    *,
    block_start: int,
    block_end: int,
    format: str,
    quote_char: str = '"',
) -> Optional[AdapterZipBundle]:
    """Base64 解码后的负载：ZIP 包（Cocos window.__zip）或 zlib/gzip 压缩的 JSON 文本。"""
    if _looks_like_zip_archive(binary):
        try:
            with zipfile.ZipFile(io.BytesIO(binary), "r") as zf:
                if zf.testzip() is not None:
                    return None
        except zipfile.BadZipFile:
            return None
        return AdapterZipBundle(
            block_start=block_start,
            block_end=block_end,
            json_text="",
            compress_mode="zip",
            format=format,
            quote_char=quote_char,
            archive_bytes=binary,
        )
    try:
        decompressed, mode = _decompress_payload(binary)
    except Exception:
        return None
    try:
        json_text = decompressed.decode("utf-8")
    except UnicodeDecodeError:
        json_text = decompressed.decode("utf-8", errors="replace")
    return AdapterZipBundle(
        block_start=block_start,
        block_end=block_end,
        json_text=json_text,
        compress_mode=mode,
        format=format,
        quote_char=quote_char,
        archive_bytes=None,
    )


def _zip_entry_is_text_rule_target(arcname: str) -> bool:
    """是否对内嵌 ZIP 中的该路径做 UTF-8 文本规则（代码替换 + data:image 图片替换）。"""
    norm = arcname.replace("\\", "/").strip("/").lower()
    if not norm:
        return False
    base = norm.rsplit("/", 1)[-1]
    if base.endswith(".json") or base.endswith(".js"):
        return True
    # Cocos 等导出：无扩展名的资源聚合文件，内含 data:image;base64 等
    if base == "__res":
        return True
    return False


def _zip_entry_apply_embedded_image_rules(arcname: str) -> bool:
    """
    内嵌 ZIP 中是否对条目执行「图片特征」替换。

    Cocos 的 cocos-js/*.js 等引擎文件内也可能出现 data:image 片段（如虚拟模块），
    若对其做 replace_images_by_feature 极易破坏脚本导致白屏；故仅允许 __res 与资源 .json。
    """
    norm = arcname.replace("\\", "/")
    if "cocos-js/" in norm.lower():
        return False
    base = norm.rstrip("/").rsplit("/", 1)[-1].lower()
    if base == "__res":
        return True
    if base.endswith(".json"):
        return True
    return False


def _validate_cocos_embedded_zip(archive: bytes) -> bool:
    """改写 ZIP 后校验：结构完整，且存在 __res 时须为合法 JSON。"""
    try:
        with zipfile.ZipFile(io.BytesIO(archive), "r") as zf:
            if zf.testzip() is not None:
                return False
            res_name: Optional[str] = None
            for n in zf.namelist():
                if n.endswith("/"):
                    continue
                if n.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1] == "__res":
                    res_name = n
                    break
            if res_name is not None:
                json.loads(zf.read(res_name).decode("utf-8"))
    except (zipfile.BadZipFile, OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return False
    return True


def _zip_entry_basename_is___res(arcname: str) -> bool:
    return arcname.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1].lower() == "__res"


def _replace_images_in___res_json_body(
    text: str, image_rules: List[Tuple[str, str]]
) -> Tuple[str, int]:
    """
    Cocos __res / adapter JSON：对象键为资源路径，值为 data:image;base64,... 。

    每条图片规则只替换「首个」包含该特征字符串的资源项，避免 PNG 等同前缀导致
    一条规则误改多张图；在单个资源值内也最多替换一处 data URL。
    """
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return _apply_image_rules_plain_text(text, image_rules)

    if not isinstance(obj, dict):
        return _apply_image_rules_plain_text(text, image_rules)

    total = 0
    for feat, new_p in image_rules:
        if not feat:
            continue
        for k in list(obj.keys()):
            val = obj[k]
            if not isinstance(val, str) or "data:image" not in val or feat not in val:
                continue
            new_val, n = replace_images_by_feature(val, feat, new_p, max_replacements=1)
            if n:
                obj[k] = new_val
                total += n
                break

    try:
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":")), total
    except (TypeError, ValueError):
        return _apply_image_rules_plain_text(text, image_rules)


def _apply_image_rules_plain_text(
    text: str, image_rules: List[Tuple[str, str]]
) -> Tuple[str, int]:
    """非 __res 结构：按规则顺序在整段文本中替换（每条规则可命中多处）。"""
    out, total = text, 0
    for feat, new_p in image_rules:
        out, n = replace_images_by_feature(out, feat, new_p)
        total += n
    return out, total


def apply_image_rules_to_text(
    text: str, image_rules: List[Tuple[str, str]]
) -> Tuple[str, int]:
    """对文本应用图片规则：优先按 __res JSON 逐资源替换，否则整段正则替换。"""
    if not image_rules:
        return text, 0
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        obj = None
    if isinstance(obj, dict) and any(
        isinstance(v, str) and "data:image" in v for v in obj.values()
    ):
        return _replace_images_in___res_json_body(text, image_rules)
    return _apply_image_rules_plain_text(text, image_rules)


def _apply_rules_to_embedded_zip(
    archive: bytes,
    image_rules: List[Tuple[str, str]],
    code_rules: List[Tuple[str, str]],
) -> Tuple[bytes, int, int]:
    """对内嵌 ZIP 中 .json / .js / __res 等文本条目做替换；图片规则仅作用于 __res 与 assets .json。"""
    in_buf = io.BytesIO(archive)
    out_buf = io.BytesIO()
    img_total = 0
    code_total = 0
    with zipfile.ZipFile(in_buf, "r") as z_in:
        with zipfile.ZipFile(out_buf, "w") as z_out:
            for info in z_in.infolist():
                if info.is_dir():
                    continue
                data = z_in.read(info.filename)
                if _zip_entry_is_text_rule_target(info.filename):
                    try:
                        text = data.decode("utf-8")
                    except UnicodeDecodeError:
                        z_out.writestr(info, data)
                        continue
                    orig = text
                    for old, new in code_rules:
                        text, c = replace_literal_exact(text, old, new)
                        code_total += c
                    # __res：JSON 级图片替换
                    if _zip_entry_basename_is___res(info.filename):
                        text, n_add = _replace_images_in___res_json_body(text, image_rules)
                        img_total += n_add
                    elif _zip_entry_apply_embedded_image_rules(info.filename) and image_rules:
                        for feat, new_p in image_rules:
                            text, n = replace_images_by_feature(text, feat, new_p)
                            img_total += n
                    if text != orig:
                        data = text.encode("utf-8")
                z_out.writestr(info, data)
    return out_buf.getvalue(), img_total, code_total


def find_adapter_zip_bundle(html: str, field_name: str = "window.__adapter_zip__") -> Optional[AdapterZipBundle]:
    """
    若存在 adapter 字段分片（data-id=\"adapter-zip-*\"）或直接赋值（window.__zip = "..."），
    则拼接 Base64 后：可能是标准 ZIP（Cocos 内嵌资源包），或 zlib/gzip 压缩的 JSON 文本。

    说明：__adapter_zip__ 类导出常为 zlib/gzip；window.__zip 常为 ZIP 魔数 PK\\x03\\x04。
    多段 script 时按 JS 语义合并：首段 = 赋值，后续 += 追加；若多段均为 = 则取最后一次（与浏览器一致）。
    
    参数：
        html: 原始 HTML 文本
        field_name: 字段名，如 "window.__adapter_zip__" 或 "window.__zip"
    """
    script_re = _build_adapter_script_re()
    inline_script_re = re.compile(r'<script[^>]*>(.*?)</script>', re.IGNORECASE | re.DOTALL)

    parsed: List[Tuple[int, int, int, str]] = []
    for m in script_re.finditer(html):
        parsed.append((int(m.group(1)), m.start(), m.end(), m.group(2)))
    if parsed:
        parsed.sort(key=lambda t: t[0])
        ids = [t[0] for t in parsed]
        if ids != list(range(len(ids))):
            return None

        all_literals: List[_AdapterLiteral] = []
        for _, _, _, body in parsed:
            all_literals.extend(_extract_adapter_literals(body, field_name))
        if not all_literals:
            return None

        b64_full = _merge_adapter_b64_literals(all_literals)
        binary = _decode_adapter_b64(b64_full)
        if binary is None:
            return None

        bundle = _adapter_bundle_from_binary(
            binary,
            block_start=parsed[0][1],
            block_end=parsed[-1][2],
            format="split",
            quote_char=all_literals[0].quote_char,
        )
        return bundle

    for script_match in inline_script_re.finditer(html):
        body = script_match.group(1)
        literals = _extract_adapter_literals(body, field_name)
        if not literals:
            continue

        b64_full = _merge_adapter_b64_literals(literals)
        binary = _decode_adapter_b64(b64_full)
        if binary is None:
            continue

        start = script_match.start(1) + literals[0].start
        end = script_match.start(1) + literals[-1].end
        bundle = _adapter_bundle_from_binary(
            binary,
            block_start=start,
            block_end=end,
            format="direct",
            quote_char=literals[0].quote_char,
        )
        if bundle:
            return bundle

    return None


def replace_literal_exact(text: str, old: str, new: str) -> Tuple[str, int]:
    """按原文查找 old，全部替换为 new。返回 (新文本, 命中次数)。"""
    if not old:
        return text, 0
    n = text.count(old)
    if n == 0:
        return text, 0
    return text.replace(old, new), n


def detect_adapter_fields(html: str) -> List[str]:
    """
    自动检测 HTML 中可解码的 adapter 字段名列表。
    顺序：先尝试预设字段；再扫描 window.__xxx 模式（去重，保留首次出现的大小写）。
    仅返回能成功解码出 ZIP 或 zlib/gzip 负载的字段名。
    """
    found: List[str] = []
    seen: set[str] = set()

    for field in _ADAPTER_FIELD_PRESETS:
        if field in seen:
            continue
        if find_adapter_zip_bundle(html, field):
            found.append(field)
            seen.add(field)

    for m in _ADAPTER_FIELD_SCAN_RE.finditer(html):
        name = re.sub(r"\s+", "", m.group(1))
        if name in seen:
            continue
        if find_adapter_zip_bundle(html, name):
            found.append(name)
            seen.add(name)

    return found


def apply_adapter_zip_pipeline_auto(
    html: str,
    image_rules: List[Tuple[str, str]],
    code_rules: List[Tuple[str, str]],
) -> Tuple[str, int, int, List[str]]:
    """
    自动检测所有 adapter 字段并依次应用规则。
    返回 (新 html, 图片替换数, 代码替换数, 实际处理过的字段列表)。
    """
    fields = detect_adapter_fields(html)
    img_total = 0
    code_total = 0
    used: List[str] = []
    for field in fields:
        new_html, img_n, code_n = apply_adapter_zip_pipeline(html, image_rules, code_rules, field)
        if img_n or code_n or new_html != html:
            used.append(field)
            html = new_html
            img_total += img_n
            code_total += code_n
    return html, img_total, code_total, used


def apply_adapter_zip_pipeline(
    html: str,
    image_rules: List[Tuple[str, str]],
    code_rules: List[Tuple[str, str]],
    field_name: str = "window.__adapter_zip__",
) -> Tuple[str, int, int]:
    """
    处理 adapter 字段：zlib/gzip 解压后的 JSON，或 Cocos 类内嵌 ZIP；
    先应用代码精确替换，再应用图片规则；再编码写回。
    返回 (新 html, 图片替换次数, 代码替换次数)。
    
    参数：
        field_name: 字段名，如 "window.__adapter_zip__" 或 "window.__zip"
    """
    bundle = find_adapter_zip_bundle(html, field_name)
    if not bundle:
        return html, 0, 0
    if not image_rules and not code_rules:
        return html, 0, 0

    if bundle.archive_bytes is not None:
        new_archive, img_total, code_total = _apply_rules_to_embedded_zip(
            bundle.archive_bytes, image_rules, code_rules
        )
        if img_total == 0 and code_total == 0:
            return html, 0, 0
        if not _validate_cocos_embedded_zip(new_archive):
            return html, 0, 0
        try:
            new_b64 = base64.b64encode(new_archive).decode("ascii")
        except Exception:
            return html, 0, 0
        if bundle.format == "direct":
            new_block = rebuild_adapter_zip_direct_assignment(
                new_b64, field_name, bundle.quote_char
            )
        else:
            new_block = rebuild_adapter_zip_scripts(new_b64, field_name)
        new_html = html[: bundle.block_start] + new_block + html[bundle.block_end :]
        return new_html, img_total, code_total

    json_text = bundle.json_text
    code_total = 0
    for old, new in code_rules:
        json_text, c = replace_literal_exact(json_text, old, new)
        code_total += c

    json_text, img_total = apply_image_rules_to_text(json_text, image_rules)

    if json_text == bundle.json_text:
        return html, img_total, code_total

    try:
        compressed = _compress_payload(
            json_text.encode("utf-8"), bundle.compress_mode
        )
    except Exception:
        return html, 0, 0  # 保持原 HTML，忽略本次 adapter 修改

    new_b64 = base64.b64encode(compressed).decode("ascii")
    if bundle.format == "direct":
        new_block = rebuild_adapter_zip_direct_assignment(new_b64, field_name, bundle.quote_char)
    else:
        new_block = rebuild_adapter_zip_scripts(new_b64, field_name)
    new_html = html[: bundle.block_start] + new_block + html[bundle.block_end :]
    return new_html, img_total, code_total


def rebuild_adapter_zip_scripts(b64_full: str, field_name: str = "window.__adapter_zip__") -> str:
    """将完整 Base64 按长度分片，生成与常见模板一致的 script 串联 HTML。
    
    参数：
        b64_full: 完整 Base64 字符串
        field_name: 字段名，如 "window.__adapter_zip__" 或 "window.__zip"
    """
    chunks: List[str] = []
    s = b64_full
    step = _ADAPTER_B64_CHUNK_SIZE
    for i in range(0, len(s), step):
        chunks.append(s[i : i + step])
    parts: List[str] = []
    for i, ch in enumerate(chunks):
        if i == 0:
            inner = f'{field_name}="{ch}";'
        else:
            inner = f'{field_name}+="{ch}";'
        parts.append(f'<script data-id="adapter-zip-{i}">{inner}</script>')
    return "".join(parts)


def rebuild_adapter_zip_direct_assignment(
    b64_full: str,
    field_name: str = "window.__adapter_zip__",
    quote_char: str = '"',
) -> str:
    """将直接赋值模式的 adapter zip 写回 HTML。"""
    return f"{field_name}={quote_char}{b64_full}{quote_char};"


# ===================== 图片扫描 / 替换：可视化模式 =====================

# location_kind 取值：
#   "html_inline"        — HTML 文本中直接出现的 data:image 完整 URL（含 src="...", JS 字符串内）
#   "html_resmap"        — HTML 中 window.resMap / __resMap__ 等对象赋值的字典 value
#   "adapter_json_res"   — adapter 字段（zlib/gzip）解压为 JSON dict，value 为 data:image
#   "adapter_zip_res"    — adapter 字段为 ZIP 时，内部 __res 文件中的 dict value
#   "adapter_zip_text"   — adapter 字段为 ZIP 时，整个文件就是一条 data:image 文本


@dataclass
class ImageEntry:
    """一张被发现的 HTML 内嵌图片，包含展示信息与写回所需的定位描述。"""

    index: int
    label: str
    mime: str
    width: int
    height: int
    byte_size: int
    data: bytes

    location_kind: str
    adapter_field: Optional[str] = None
    zip_entry_name: Optional[str] = None
    res_key: Optional[str] = None
    original_data_url: str = ""
    obfuscation_offset: int = -1

    new_bytes: Optional[bytes] = None
    new_mime: Optional[str] = None
    new_path: Optional[str] = None


def _png_dimensions(data: bytes) -> Tuple[int, int]:
    if len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
        w = int.from_bytes(data[16:20], "big")
        h = int.from_bytes(data[20:24], "big")
        return w, h
    return 0, 0


def _jpeg_dimensions(data: bytes) -> Tuple[int, int]:
    if len(data) < 4 or data[:3] != b"\xff\xd8\xff":
        return 0, 0
    i = 2
    n = len(data)
    while i < n - 9:
        if data[i] != 0xFF:
            i += 1
            continue
        while i < n and data[i] == 0xFF:
            i += 1
        if i >= n:
            break
        marker = data[i]
        i += 1
        if marker in (0xD8, 0xD9):
            continue
        if marker == 0xDA:
            break
        if i + 2 > n:
            break
        seg_len = int.from_bytes(data[i : i + 2], "big")
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            if i + 7 <= n:
                h = int.from_bytes(data[i + 3 : i + 5], "big")
                w = int.from_bytes(data[i + 5 : i + 7], "big")
                return w, h
        i += seg_len
    return 0, 0


def _gif_dimensions(data: bytes) -> Tuple[int, int]:
    if len(data) >= 10 and data[:6] in (b"GIF87a", b"GIF89a"):
        w = int.from_bytes(data[6:8], "little")
        h = int.from_bytes(data[8:10], "little")
        return w, h
    return 0, 0


def _webp_dimensions(data: bytes) -> Tuple[int, int]:
    if len(data) < 30 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        return 0, 0
    chunk = data[12:16]
    if chunk == b"VP8 " and len(data) >= 30:
        w = int.from_bytes(data[26:28], "little") & 0x3FFF
        h = int.from_bytes(data[28:30], "little") & 0x3FFF
        return w, h
    if chunk == b"VP8L" and len(data) >= 25:
        b0, b1, b2, b3 = data[21], data[22], data[23], data[24]
        w = ((b1 & 0x3F) << 8 | b0) + 1
        h = ((b3 & 0x0F) << 10 | b2 << 2 | (b1 & 0xC0) >> 6) + 1
        return w, h
    if chunk == b"VP8X" and len(data) >= 30:
        w = (data[24] | data[25] << 8 | data[26] << 16) + 1
        h = (data[27] | data[28] << 8 | data[29] << 16) + 1
        return w, h
    return 0, 0


def _image_dimensions(data: bytes) -> Tuple[int, int]:
    """根据 magic bytes 解析图像宽高，不依赖外部库。"""
    for fn in (_png_dimensions, _jpeg_dimensions, _gif_dimensions, _webp_dimensions):
        w, h = fn(data)
        if w and h:
            return w, h
    return 0, 0


def _format_byte_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.2f} MB"


def _ext_for_mime_image(mime: str) -> str:
    return _MIME_EXT_FALLBACK.get(mime.lower(), ".png")


def _make_image_entry(
    *,
    next_index: int,
    raw: bytes,
    mime: str,
    location_kind: str,
    label: str,
    adapter_field: Optional[str] = None,
    zip_entry_name: Optional[str] = None,
    res_key: Optional[str] = None,
    original_data_url: str = "",
    obfuscation_offset: int = -1,
) -> ImageEntry:
    w, h = _image_dimensions(raw)
    return ImageEntry(
        index=next_index,
        label=label,
        mime=mime,
        width=w,
        height=h,
        byte_size=len(raw),
        data=raw,
        location_kind=location_kind,
        adapter_field=adapter_field,
        zip_entry_name=zip_entry_name,
        res_key=res_key,
        original_data_url=original_data_url,
        obfuscation_offset=obfuscation_offset,
    )


def _scan_images_in_text(
    text: str,
    *,
    next_index_ref: List[int],
    seen_digests: set,
    location_kind: str,
    label_prefix: str,
    adapter_field: Optional[str] = None,
    zip_entry_name: Optional[str] = None,
    capture_full_url: bool = True,
) -> List[ImageEntry]:
    """在一段文本中扫描完整 data:image 片段，构建 ImageEntry 列表。"""
    out: List[ImageEntry] = []
    pos = 0
    lower = text.lower()
    while pos < len(text):
        idx = lower.find("data:image/", pos)
        if idx < 0:
            break
        semi = lower.find(";base64,", idx)
        if semi < 0:
            pos = idx + 1
            continue
        mime = text[idx + 5 : semi].lower()
        j = semi + len(";base64,")
        start_b64 = j
        while j < len(text) and (_is_b64_char(text[j]) or text[j].isspace()):
            j += 1
        b64_segment = text[start_b64:j]
        raw, offset = _decode_b64_with_offset(b64_segment, (_validate_image_bytes,))
        if raw and _validate_image_bytes(raw):
            digest = hashlib.sha256(raw).hexdigest()[:16]
            if digest not in seen_digests:
                seen_digests.add(digest)
                full_url = text[idx:j] if capture_full_url else ""
                out.append(
                    _make_image_entry(
                        next_index=next_index_ref[0],
                        raw=raw,
                        mime=mime,
                        location_kind=location_kind,
                        label=f"{label_prefix}（#{next_index_ref[0] + 1}）",
                        adapter_field=adapter_field,
                        zip_entry_name=zip_entry_name,
                        original_data_url=full_url,
                        obfuscation_offset=offset,
                    )
                )
                next_index_ref[0] += 1
        pos = max(j, idx + 1)
    return out


def _scan_images_in_resmap(
    obj: dict,
    *,
    next_index_ref: List[int],
    seen_digests: set,
    location_kind: str,
    adapter_field: Optional[str] = None,
    zip_entry_name: Optional[str] = None,
) -> List[ImageEntry]:
    """从 dict（__res / window.resMap 等）的每个 value 中识别 data:image。"""
    out: List[ImageEntry] = []
    if not isinstance(obj, dict):
        return out
    for key, val in obj.items():
        if not isinstance(val, str) or "data:image" not in val[:32].lower():
            continue
        low = val.lower()
        if not low.startswith("data:image/") or ";base64," not in low:
            continue
        marker = ";base64,"
        i = low.find(marker)
        mime = val[5:i].lower()
        b64_part = val[i + len(marker) :]
        raw, offset = _decode_b64_with_offset(b64_part, (_validate_image_bytes,))
        if not raw or not _validate_image_bytes(raw):
            continue
        digest = hashlib.sha256(raw).hexdigest()[:16]
        if digest in seen_digests:
            continue
        seen_digests.add(digest)
        out.append(
            _make_image_entry(
                next_index=next_index_ref[0],
                raw=raw,
                mime=mime,
                location_kind=location_kind,
                label=str(key),
                adapter_field=adapter_field,
                zip_entry_name=zip_entry_name,
                res_key=str(key),
                obfuscation_offset=offset,
            )
        )
        next_index_ref[0] += 1
    return out


def scan_html_images(html: str) -> List[ImageEntry]:
    """扫描 HTML 中可被替换的图片：HTML 内联 data URL、resMap、adapter（ZIP/JSON）内的图片。"""
    entries: List[ImageEntry] = []
    seen_digests: set = set()
    next_index_ref = [0]

    # 1) HTML 顶层 data URL
    entries.extend(
        _scan_images_in_text(
            html,
            next_index_ref=next_index_ref,
            seen_digests=seen_digests,
            location_kind="html_inline",
            label_prefix="HTML 内联图片",
        )
    )

    # 2) HTML 内 window.resMap 等对象赋值
    for obj in _extract_js_object_assignments(html, _RES_MAP_FIELD_NAMES):
        entries.extend(
            _scan_images_in_resmap(
                obj,
                next_index_ref=next_index_ref,
                seen_digests=seen_digests,
                location_kind="html_resmap",
            )
        )

    # 3) 每个 adapter 字段
    for field in detect_adapter_fields(html):
        bundle = find_adapter_zip_bundle(html, field)
        if bundle is None:
            continue
        if bundle.archive_bytes is not None:
            try:
                with zipfile.ZipFile(io.BytesIO(bundle.archive_bytes), "r") as zf:
                    for info in zf.infolist():
                        if info.is_dir():
                            continue
                        arc = info.filename.replace("\\", "/")
                        base = arc.rstrip("/").rsplit("/", 1)[-1].lower()
                        try:
                            data = zf.read(info.filename)
                        except Exception:
                            continue
                        if base == "__res":
                            try:
                                obj = json.loads(data.decode("utf-8"))
                            except Exception:
                                continue
                            entries.extend(
                                _scan_images_in_resmap(
                                    obj,
                                    next_index_ref=next_index_ref,
                                    seen_digests=seen_digests,
                                    location_kind="adapter_zip_res",
                                    adapter_field=field,
                                    zip_entry_name=info.filename,
                                )
                            )
                            continue
                        lower = arc.lower()
                        # PNG 等扩展名的 ZIP 条目：可能是 text data URL
                        if any(lower.endswith(ext) for ext in _IMAGE_EXTS):
                            if _validate_image_bytes(data):
                                continue  # 真二进制图片，跳过（不打算替换裸 ZIP 内二进制图）
                            try:
                                text = data.decode("utf-8")
                            except UnicodeDecodeError:
                                continue
                            low = text.lower()
                            if not low.startswith("data:image/") or ";base64," not in low:
                                continue
                            marker = ";base64,"
                            i = low.find(marker)
                            mime = text[5:i].lower()
                            b64_part = text[i + len(marker) :]
                            raw, offset = _decode_b64_with_offset(b64_part, (_validate_image_bytes,))
                            if not raw or not _validate_image_bytes(raw):
                                continue
                            digest = hashlib.sha256(raw).hexdigest()[:16]
                            if digest in seen_digests:
                                continue
                            seen_digests.add(digest)
                            entries.append(
                                _make_image_entry(
                                    next_index=next_index_ref[0],
                                    raw=raw,
                                    mime=mime,
                                    location_kind="adapter_zip_text",
                                    label=arc,
                                    adapter_field=field,
                                    zip_entry_name=info.filename,
                                    obfuscation_offset=offset,
                                )
                            )
                            next_index_ref[0] += 1
            except zipfile.BadZipFile:
                continue
        else:
            try:
                obj = json.loads(bundle.json_text)
            except Exception:
                continue
            if isinstance(obj, dict):
                entries.extend(
                    _scan_images_in_resmap(
                        obj,
                        next_index_ref=next_index_ref,
                        seen_digests=seen_digests,
                        location_kind="adapter_json_res",
                        adapter_field=field,
                    )
                )

    return entries


def _build_replacement_data_url(entry: ImageEntry) -> str:
    """根据 entry.new_bytes 构造新 data URL，保留原混淆模式。"""
    assert entry.new_bytes is not None
    mime = entry.new_mime or entry.mime
    new_b64 = base64.b64encode(entry.new_bytes).decode("ascii")
    if entry.obfuscation_offset >= 0:
        new_b64 = _re_obfuscate_b64(new_b64, entry.obfuscation_offset)
    return f"data:{mime};base64,{new_b64}"


def apply_image_replacements(
    html: str,
    entries: List[ImageEntry],
) -> Tuple[str, int]:
    """对所有 new_bytes 已填写的 entry 执行写回，返回 (新 html, 命中次数)。"""
    pending = [e for e in entries if e.new_bytes]
    if not pending:
        return html, 0

    count = 0

    # 1) HTML inline
    for e in pending:
        if e.location_kind != "html_inline" or not e.original_data_url:
            continue
        if e.original_data_url not in html:
            continue
        html = html.replace(e.original_data_url, _build_replacement_data_url(e), 1)
        count += 1

    # 2) HTML resMap
    resmap_entries = [e for e in pending if e.location_kind == "html_resmap"]
    if resmap_entries:
        html, n = _apply_resmap_replacements_in_html(html, resmap_entries)
        count += n

    # 3) 按 adapter 分组
    by_field: dict[str, list[ImageEntry]] = {}
    for e in pending:
        if e.location_kind in ("adapter_zip_res", "adapter_zip_text", "adapter_json_res"):
            by_field.setdefault(e.adapter_field or "", []).append(e)

    for field, group in by_field.items():
        if not field:
            continue
        bundle = find_adapter_zip_bundle(html, field)
        if bundle is None:
            continue
        if bundle.archive_bytes is not None:
            new_archive, n = _apply_image_replacements_to_zip(bundle.archive_bytes, group)
            if n == 0:
                continue
            count += n
            new_b64 = base64.b64encode(new_archive).decode("ascii")
            if bundle.format == "direct":
                new_block = rebuild_adapter_zip_direct_assignment(new_b64, field, bundle.quote_char)
            else:
                new_block = rebuild_adapter_zip_scripts(new_b64, field)
            html = html[: bundle.block_start] + new_block + html[bundle.block_end :]
        else:
            try:
                obj = json.loads(bundle.json_text)
            except Exception:
                continue
            if not isinstance(obj, dict):
                continue
            n = 0
            for e in group:
                if e.location_kind == "adapter_json_res" and e.res_key in obj:
                    obj[e.res_key] = _build_replacement_data_url(e)
                    n += 1
            if n == 0:
                continue
            count += n
            new_json = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
            try:
                compressed = _compress_payload(new_json.encode("utf-8"), bundle.compress_mode)
            except Exception:
                continue
            new_b64 = base64.b64encode(compressed).decode("ascii")
            if bundle.format == "direct":
                new_block = rebuild_adapter_zip_direct_assignment(new_b64, field, bundle.quote_char)
            else:
                new_block = rebuild_adapter_zip_scripts(new_b64, field)
            html = html[: bundle.block_start] + new_block + html[bundle.block_end :]

    return html, count


def _apply_resmap_replacements_in_html(
    html: str, entries: List[ImageEntry]
) -> Tuple[str, int]:
    """在 HTML 文本里查找 window.resMap = {...} 等对象并替换其中匹配键的 value。"""
    total = 0
    for field_name in _RES_MAP_FIELD_NAMES:
        html_lower = html.lower()
        name_lower = field_name.lower()
        search_pos = 0
        while True:
            idx = html_lower.find(name_lower, search_pos)
            if idx < 0:
                break
            j = idx + len(field_name)
            while j < len(html) and html[j].isspace():
                j += 1
            if j >= len(html) or html[j] != "=":
                search_pos = idx + 1
                continue
            j += 1
            while j < len(html) and html[j].isspace():
                j += 1
            parsed = _parse_js_json_object_at(html, j)
            if not parsed:
                search_pos = idx + 1
                continue
            obj, end = parsed
            if not isinstance(obj, dict):
                search_pos = end
                continue
            changed = False
            for e in entries:
                if e.res_key and e.res_key in obj:
                    obj[e.res_key] = _build_replacement_data_url(e)
                    changed = True
                    total += 1
            if changed:
                new_str = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
                obj_start = html.find("{", idx + len(field_name))
                if obj_start >= 0 and obj_start < end:
                    html = html[:obj_start] + new_str + html[end:]
                    html_lower = html.lower()
                    search_pos = obj_start + len(new_str)
                    continue
            search_pos = end
    return html, total


def _apply_image_replacements_to_zip(
    archive: bytes, entries: List[ImageEntry]
) -> Tuple[bytes, int]:
    """在 ZIP 内对 __res JSON 与 text data URL 类型的图片条目执行替换。"""
    res_updates: dict[str, list[ImageEntry]] = {}
    text_updates: dict[str, ImageEntry] = {}
    for e in entries:
        if e.location_kind == "adapter_zip_res" and e.zip_entry_name:
            res_updates.setdefault(e.zip_entry_name, []).append(e)
        elif e.location_kind == "adapter_zip_text" and e.zip_entry_name:
            text_updates[e.zip_entry_name] = e

    in_buf = io.BytesIO(archive)
    out_buf = io.BytesIO()
    total = 0
    with zipfile.ZipFile(in_buf, "r") as z_in:
        with zipfile.ZipFile(out_buf, "w", zipfile.ZIP_DEFLATED) as z_out:
            for info in z_in.infolist():
                if info.is_dir():
                    continue
                data = z_in.read(info.filename)
                if info.filename in res_updates:
                    try:
                        obj = json.loads(data.decode("utf-8"))
                    except Exception:
                        z_out.writestr(info, data)
                        continue
                    n = 0
                    for e in res_updates[info.filename]:
                        if e.res_key and e.res_key in obj and isinstance(obj[e.res_key], str):
                            obj[e.res_key] = _build_replacement_data_url(e)
                            n += 1
                    if n:
                        data = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                        total += n
                elif info.filename in text_updates:
                    e = text_updates[info.filename]
                    data = _build_replacement_data_url(e).encode("utf-8")
                    total += 1
                z_out.writestr(info, data)
    return out_buf.getvalue(), total


def _normalize_new_payload(raw: str) -> str:
    """规范化用户输入：纯 base64 去空白；完整 data URL 则去掉 base64 段内的换行与空格。"""
    s = raw.strip()
    if not s:
        return ""
    if s.lower().startswith("data:"):
        low = s.lower()
        marker = ";base64,"
        i = low.find(marker)
        if i == -1:
            return re.sub(r"\s+", "", s)
        head, tail = s[: i + len(marker)], s[i + len(marker) :]
        return head + re.sub(r"\s+", "", tail)
    return re.sub(r"\s+", "", s)


def replace_images_by_feature(
    html: str,
    feature_substring: str,
    new_base64_or_data_url: str,
    *,
    max_replacements: int = 0,
) -> Tuple[str, int]:
    """
    在 HTML 中查找所有 data:image/...;base64,... 且 Base64 正文包含 feature_substring 的片段，
    将整段 data URL 替换为新图（保留原 MIME，除非新内容为完整 data URL）。

    max_replacements: 0 表示不限制；>0 时最多替换该次数（用于单资源内只改一张图）。

    返回：(新 HTML, 替换次数)
    """
    if not feature_substring:
        return html, 0

    new_payload = _normalize_new_payload(new_base64_or_data_url)
    if not new_payload:
        return html, 0

    count = 0
    limit = max_replacements if max_replacements > 0 else 0

    def sub_fn(m: Any) -> str:
        nonlocal count
        if limit and count >= limit:
            return m.group(0)
        _prefix, old_b64 = m.group(1), m.group(2)
        if feature_substring not in old_b64:
            return m.group(0)
        if new_payload.lower().startswith("data:"):
            count += 1
            return new_payload
        mime_match = re.match(r"data:(image/[\w.+-]+);base64,", m.group(0), re.I)
        mime = mime_match.group(1) if mime_match else "image/png"
        count += 1
        return f"data:{mime};base64,{new_payload}"

    new_html = _DATA_URL_RE.sub(sub_fn, html)
    return new_html, count


# ============ UI 主题与配色 ============

UI_BG = "#f5f6fa"
UI_CARD = "#ffffff"
UI_BORDER = "#e1e4ea"
UI_TEXT = "#1f2937"
UI_MUTED = "#6b7280"
UI_PRIMARY = "#2563eb"
UI_PRIMARY_HOVER = "#1d4ed8"
UI_ACCENT = "#10b981"
UI_DANGER = "#ef4444"
UI_HINT_BG = "#eef2ff"

UI_FONT_FAMILY = "Microsoft YaHei UI"
UI_FONT = (UI_FONT_FAMILY, 10)
UI_FONT_BOLD = (UI_FONT_FAMILY, 10, "bold")
UI_FONT_TITLE = (UI_FONT_FAMILY, 13, "bold")
UI_FONT_SECTION = (UI_FONT_FAMILY, 11, "bold")
UI_FONT_MONO = ("Consolas", 9)


def _configure_styles(root: tk.Tk) -> None:
    """配置全局 ttk 主题与控件样式。"""
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass

    root.configure(bg=UI_BG)
    root.option_add("*Font", UI_FONT)
    root.option_add("*Text.Font", UI_FONT_MONO)
    root.option_add("*Text.background", UI_CARD)
    root.option_add("*Text.foreground", UI_TEXT)
    root.option_add("*Text.relief", "flat")
    root.option_add("*Text.borderWidth", 1)
    root.option_add("*Text.highlightThickness", 1)
    root.option_add("*Text.highlightBackground", UI_BORDER)
    root.option_add("*Text.highlightColor", UI_PRIMARY)
    root.option_add("*Text.insertBackground", UI_PRIMARY)
    root.option_add("*Text.padX", 6)
    root.option_add("*Text.padY", 4)

    style.configure(".", background=UI_BG, foreground=UI_TEXT, font=UI_FONT)
    style.configure("TFrame", background=UI_BG)
    style.configure("Card.TFrame", background=UI_CARD, relief="flat", borderwidth=1)
    style.configure("Hint.TFrame", background=UI_HINT_BG)

    style.configure("TLabel", background=UI_BG, foreground=UI_TEXT, font=UI_FONT)
    style.configure("Card.TLabel", background=UI_CARD, foreground=UI_TEXT)
    style.configure("Hint.TLabel", background=UI_HINT_BG, foreground="#3730a3")
    style.configure("Title.TLabel", background=UI_BG, foreground=UI_PRIMARY, font=UI_FONT_TITLE)
    style.configure("Section.TLabel", background=UI_BG, foreground=UI_TEXT, font=UI_FONT_SECTION)
    style.configure("Muted.TLabel", background=UI_BG, foreground=UI_MUTED, font=(UI_FONT_FAMILY, 9))
    style.configure("Status.TLabel", background=UI_CARD, foreground=UI_MUTED, font=(UI_FONT_FAMILY, 9))

    style.configure(
        "TButton",
        background=UI_CARD,
        foreground=UI_TEXT,
        borderwidth=1,
        relief="flat",
        padding=(12, 6),
        font=UI_FONT,
    )
    style.map(
        "TButton",
        background=[("active", "#eef2ff"), ("pressed", "#dbeafe")],
        bordercolor=[("active", UI_PRIMARY)],
    )

    style.configure(
        "Primary.TButton",
        background=UI_PRIMARY,
        foreground="#ffffff",
        borderwidth=0,
        padding=(16, 8),
        font=UI_FONT_BOLD,
    )
    style.map(
        "Primary.TButton",
        background=[("active", UI_PRIMARY_HOVER), ("pressed", "#1e40af")],
        foreground=[("disabled", "#cbd5e1")],
    )

    style.configure(
        "Accent.TButton",
        background=UI_ACCENT,
        foreground="#ffffff",
        borderwidth=0,
        padding=(12, 6),
        font=UI_FONT_BOLD,
    )
    style.map(
        "Accent.TButton",
        background=[("active", "#059669"), ("pressed", "#047857")],
    )

    style.configure(
        "Danger.TButton",
        background=UI_CARD,
        foreground=UI_DANGER,
        borderwidth=1,
        padding=(8, 4),
        font=UI_FONT,
    )
    style.map(
        "Danger.TButton",
        background=[("active", "#fef2f2"), ("pressed", "#fee2e2")],
        bordercolor=[("active", UI_DANGER)],
    )

    style.configure(
        "TEntry",
        fieldbackground=UI_CARD,
        foreground=UI_TEXT,
        borderwidth=1,
        relief="flat",
        padding=6,
    )
    style.map(
        "TEntry",
        bordercolor=[("focus", UI_PRIMARY)],
    )

    style.configure("TSeparator", background=UI_BORDER)

    style.configure(
        "Card.TLabelframe",
        background=UI_CARD,
        bordercolor=UI_BORDER,
        relief="solid",
        borderwidth=1,
        padding=12,
    )
    style.configure(
        "Card.TLabelframe.Label",
        background=UI_CARD,
        foreground=UI_PRIMARY,
        font=UI_FONT_BOLD,
    )

    style.configure("Vertical.TScrollbar", background=UI_BG, troughcolor=UI_BG, borderwidth=0, arrowcolor=UI_MUTED)


class CodeRuleFrame(ttk.LabelFrame):
    """代码精确替换：原文 old 全部替换为 new（可多条顺序执行）。"""

    def __init__(self, master: tk.Widget, index: int, on_remove: Callable[["CodeRuleFrame"], None]):
        super().__init__(master, text=f"  代码规则 #{index}  ", style="Card.TLabelframe")
        self.on_remove = on_remove

        ttk.Label(
            self,
            text="查找（与目标文本完全一致，区分大小写；替换所有出现处）",
            style="Card.TLabel",
        ).pack(anchor=tk.W)
        self.txt_old = tk.Text(self, height=4, width=70, wrap=tk.NONE)
        self.txt_old.pack(fill=tk.X, pady=(2, 8))

        ttk.Label(self, text="替换为（可为空 = 删除该段原文）", style="Card.TLabel").pack(anchor=tk.W)
        self.txt_new = tk.Text(self, height=4, width=70, wrap=tk.NONE)
        self.txt_new.pack(fill=tk.X, pady=(2, 8))

        ttk.Button(self, text="✕ 删除此规则", command=self._remove, style="Danger.TButton").pack(anchor=tk.E)

    def _remove(self) -> None:
        self.on_remove(self)

    def get_old(self) -> str:
        return self.txt_old.get("1.0", "end-1c")

    def get_new(self) -> str:
        return self.txt_new.get("1.0", "end-1c")


_THUMB_MAX = 96


def _make_thumb_photo(data: bytes, max_size: int = _THUMB_MAX) -> Optional[Any]:
    """从字节生成 Tk PhotoImage 缩略图；需要 PIL。"""
    if not _HAS_PIL or _PILImage is None or _PILImageTk is None:
        return None
    try:
        img = _PILImage.open(io.BytesIO(data))
        img.load()
        if img.mode not in ("RGB", "RGBA", "P", "L"):
            img = img.convert("RGBA")
        img.thumbnail((max_size, max_size), _PILImage.LANCZOS)
        return _PILImageTk.PhotoImage(img)
    except Exception:
        return None


class ImageTile(ttk.Frame):
    """单张图片的展示与替换控件。"""

    def __init__(self, master: tk.Widget, entry: ImageEntry, on_change: Callable[[], None]):
        super().__init__(master, style="Card.TFrame", padding=(10, 8))
        self.entry = entry
        self.on_change = on_change
        self._orig_photo = _make_thumb_photo(entry.data)
        self._new_photo: Optional[Any] = None

        self.configure(borderwidth=1, relief="solid")

        # 左：原图缩略
        self.left = ttk.Frame(self, style="Card.TFrame", width=_THUMB_MAX + 8, height=_THUMB_MAX + 8)
        self.left.pack(side=tk.LEFT, padx=(0, 12))
        self.left.pack_propagate(False)
        if self._orig_photo is not None:
            ttk.Label(self.left, image=self._orig_photo, style="Card.TLabel").pack(expand=True)
        else:
            ttk.Label(self.left, text="(预览不可用)", style="Card.TLabel", foreground=UI_MUTED).pack(expand=True)

        # 中：信息
        info = ttk.Frame(self, style="Card.TFrame")
        info.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        ext = _ext_for_mime_image(entry.mime).lstrip(".").upper()
        size_str = f"{entry.width}×{entry.height}" if entry.width and entry.height else "未知尺寸"
        head = ttk.Label(
            info,
            text=f"#{entry.index + 1}  {ext}  {size_str}  {_format_byte_size(entry.byte_size)}",
            style="Card.TLabel",
            font=UI_FONT_BOLD,
        )
        head.pack(anchor=tk.W)

        src_kind_text = {
            "html_inline": "HTML 内联",
            "html_resmap": "HTML resMap",
            "adapter_zip_res": "ZIP __res",
            "adapter_zip_text": "ZIP 文件条目",
            "adapter_json_res": "JSON adapter",
        }.get(entry.location_kind, entry.location_kind)

        location_bits: list[str] = [src_kind_text]
        if entry.adapter_field:
            location_bits.append(entry.adapter_field)
        location_str = " · ".join(location_bits)
        ttk.Label(info, text=location_str, style="Card.TLabel", foreground=UI_MUTED).pack(anchor=tk.W, pady=(2, 0))

        label_text = entry.label
        if len(label_text) > 90:
            label_text = "…" + label_text[-87:]
        ttk.Label(info, text=label_text, style="Card.TLabel", foreground=UI_MUTED).pack(anchor=tk.W, pady=(2, 6))

        btn_row = ttk.Frame(info, style="Card.TFrame")
        btn_row.pack(anchor=tk.W)
        ttk.Button(
            btn_row,
            text="📂 选择新图片",
            command=self._pick_replacement,
            style="Accent.TButton",
        ).pack(side=tk.LEFT)
        self.btn_clear = ttk.Button(
            btn_row,
            text="↺ 撤销",
            command=self._clear_replacement,
            style="Danger.TButton",
        )

        self.status_label = ttk.Label(info, text="", style="Card.TLabel")
        self.status_label.pack(anchor=tk.W, pady=(6, 0))

        # 右：新图预览
        self.right = ttk.Frame(self, style="Card.TFrame", width=_THUMB_MAX + 8, height=_THUMB_MAX + 8)
        self.right.pack(side=tk.RIGHT, padx=(12, 0))
        self.right.pack_propagate(False)
        self.new_preview_label = ttk.Label(self.right, text="待替换", style="Card.TLabel", foreground=UI_MUTED)
        self.new_preview_label.pack(expand=True)

        if _HAS_WINDND and windnd is not None:
            try:
                windnd.hook_dropfiles(self, func=self._on_drop)  # type: ignore[union-attr]
                if self._orig_photo is not None:
                    windnd.hook_dropfiles(self.left, func=self._on_drop)  # type: ignore[union-attr]
            except Exception:
                pass

    def _on_drop(self, files: Any) -> None:
        if not files:
            return
        paths: List[str] = []
        if isinstance(files, (list, tuple)):
            for it in files:
                paths.append(_decode_windnd_path(it))
        else:
            paths.append(_decode_windnd_path(files))
        for p in paths:
            ext = os.path.splitext(p)[1].lower()
            if ext in _IMAGE_EXTS or _guess_image_mime(p):
                self._apply_path(p)
                return

    def _pick_replacement(self) -> None:
        path = filedialog.askopenfilename(
            parent=self.winfo_toplevel(),
            title="选择替换图片",
            filetypes=[
                ("图片", "*.png *.jpg *.jpeg *.gif *.webp *.bmp *.ico *.svg *.jfif *.avif"),
                ("所有文件", "*.*"),
            ],
        )
        if path:
            self._apply_path(path)

    def _apply_path(self, path: str) -> None:
        try:
            with open(path, "rb") as f:
                raw = f.read()
        except OSError as e:
            messagebox.showerror("读取失败", str(e), parent=self.winfo_toplevel())
            return
        if not raw:
            messagebox.showerror("读取失败", "文件为空。", parent=self.winfo_toplevel())
            return
        if not _validate_image_bytes(raw):
            if not messagebox.askyesno(
                "格式提示",
                "该文件未通过图片格式校验（不是 PNG/JPG/GIF/WebP/BMP）。是否仍要使用？",
                parent=self.winfo_toplevel(),
            ):
                return
        mime = _guess_image_mime(path) or self.entry.mime
        self.entry.new_bytes = raw
        self.entry.new_mime = mime
        self.entry.new_path = path
        self._new_photo = _make_thumb_photo(raw)
        for w in self.right.winfo_children():
            w.destroy()
        if self._new_photo is not None:
            ttk.Label(self.right, image=self._new_photo, style="Card.TLabel").pack(expand=True)
        else:
            ttk.Label(self.right, text="(预览不可用)", style="Card.TLabel").pack(expand=True)
        self.status_label.configure(
            text=f"✅ 已选: {os.path.basename(path)}  ({_format_byte_size(len(raw))})",
            foreground=UI_ACCENT,
        )
        if not self.btn_clear.winfo_ismapped():
            self.btn_clear.pack(side=tk.LEFT, padx=(8, 0))
        self.on_change()

    def _clear_replacement(self) -> None:
        self.entry.new_bytes = None
        self.entry.new_mime = None
        self.entry.new_path = None
        self._new_photo = None
        for w in self.right.winfo_children():
            w.destroy()
        ttk.Label(self.right, text="待替换", style="Card.TLabel", foreground=UI_MUTED).pack(expand=True)
        self.status_label.configure(text="")
        self.btn_clear.pack_forget()
        self.on_change()


def collect_valid_code_rules(
    frames: List[CodeRuleFrame],
) -> Tuple[Optional[List[Tuple[str, str]]], Optional[str]]:
    """返回 (代码规则列表, 错误信息)。允许空列表。"""
    out: List[Tuple[str, str]] = []
    for i, cf in enumerate(frames, start=1):
        old = cf.get_old()
        new = cf.get_new()
        if not old and not new:
            continue
        if not old:
            return None, f"第 {i} 条代码规则：请填写要查找的原文。"
        out.append((old, new))
    return out, None


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("HTML Base64 资源替换工具")
        self.geometry("1040x780")
        self.minsize(820, 600)

        _configure_styles(self)

        self.html_path = tk.StringVar()
        self.status_text = tk.StringVar(value="就绪 · 请选择 HTML 文件")
        self.detected_text = tk.StringVar(value="自动检测：尚未选择文件")
        self.gallery_summary = tk.StringVar(value="尚未加载图片")
        self._image_entries: list[ImageEntry] = []
        self._tiles: list[ImageTile] = []
        self._code_rules: list[CodeRuleFrame] = []
        self._loaded_html: Optional[str] = None
        self._loaded_html_path: Optional[str] = None

        # ===== 顶部标题区 =====
        header = ttk.Frame(self, padding=(16, 14, 16, 4))
        header.pack(fill=tk.X)
        ttk.Label(header, text="HTML Base64 资源替换工具", style="Title.TLabel").pack(anchor=tk.W)
        ttk.Label(
            header,
            text="自动识别 Cocos / Adapter 内嵌图片 · 可视化选图替换 · 保留原始混淆模式",
            style="Muted.TLabel",
        ).pack(anchor=tk.W, pady=(2, 0))

        # ===== 文件选择卡片 =====
        file_card = ttk.Labelframe(self, text="  HTML 文件  ", style="Card.TLabelframe")
        file_card.pack(fill=tk.X, padx=16, pady=(8, 6))

        path_row = ttk.Frame(file_card, style="Card.TFrame")
        path_row.pack(fill=tk.X)
        ttk.Entry(path_row, textvariable=self.html_path).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8), ipady=2
        )
        ttk.Button(path_row, text="📁 浏览…", command=self._browse_html).pack(side=tk.LEFT)

        ttk.Label(
            file_card,
            textvariable=self.detected_text,
            style="Card.TLabel",
            foreground=UI_MUTED,
        ).pack(anchor=tk.W, pady=(8, 6))

        export_row = ttk.Frame(file_card, style="Card.TFrame")
        export_row.pack(fill=tk.X)
        ttk.Button(
            export_row,
            text="🖼️ 加载所有图片",
            command=self._load_images,
            style="Primary.TButton",
        ).pack(side=tk.LEFT)
        ttk.Button(
            export_row,
            text="📦 导出内嵌 ZIP",
            command=self._export_html_adapter_zip,
        ).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(
            export_row,
            text="🖼️ 导出图片 / 音频",
            command=self._export_html_media,
        ).pack(side=tk.LEFT, padx=(8, 0))

        # ===== 操作按钮区 =====
        action_bar = ttk.Frame(self, padding=(16, 4, 16, 4))
        action_bar.pack(fill=tk.X)
        ttk.Label(action_bar, textvariable=self.gallery_summary, style="Muted.TLabel").pack(side=tk.LEFT)
        ttk.Button(
            action_bar,
            text="🚀 一键生成新 HTML",
            command=self._generate,
            style="Primary.TButton",
        ).pack(side=tk.RIGHT)
        ttk.Button(action_bar, text="➕ 添加代码规则", command=self._add_code_rule).pack(
            side=tk.RIGHT, padx=(0, 8)
        )

        # ===== 提示卡片 =====
        hint_card = ttk.Frame(self, style="Hint.TFrame", padding=(12, 8))
        hint_card.pack(fill=tk.X, padx=16, pady=(4, 6))
        ttk.Label(
            hint_card,
            text="💡 操作指引",
            style="Hint.TLabel",
            font=UI_FONT_BOLD,
        ).pack(anchor=tk.W)
        hint = (
            "1. 选择 HTML 文件 → 点击「🖼️ 加载所有图片」自动扫描。\n"
            "2. 在下方图片列表中，点「📂 选择新图片」或拖入文件即可替换该张图。\n"
            "3. 可选「➕ 添加代码规则」对 HTML / 内嵌 JSON 做文本精确替换。\n"
            "4. 最后点「🚀 一键生成新 HTML」即可保存。\n"
            "原图若被加了混淆字符（Cocos 等），新图会按相同模式重新加上，确保游戏能解码。"
        )
        ttk.Label(
            hint_card,
            text=hint,
            style="Hint.TLabel",
            justify=tk.LEFT,
            wraplength=980,
        ).pack(anchor=tk.W, pady=(4, 0))

        # ===== 滚动主体 =====
        scroll_wrap = ttk.Frame(self)
        scroll_wrap.pack(fill=tk.BOTH, expand=True, padx=16, pady=(4, 4))

        canvas = tk.Canvas(scroll_wrap, highlightthickness=0, background=UI_BG, bd=0)
        scroll = ttk.Scrollbar(scroll_wrap, orient=tk.VERTICAL, command=canvas.yview)
        self._rules_container = ttk.Frame(canvas)
        self._rules_container.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        self._canvas_window = canvas.create_window(
            (0, 0), window=self._rules_container, anchor=tk.NW
        )
        canvas.configure(yscrollcommand=scroll.set)

        def _on_canvas_configure(event: tk.Event) -> None:
            canvas.itemconfig(self._canvas_window, width=event.width)

        canvas.bind("<Configure>", _on_canvas_configure)

        def _on_mousewheel(event: tk.Event) -> str | None:
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
            return None

        canvas.bind_all("<MouseWheel>", _on_mousewheel)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)

        self._canvas = canvas
        self._rules_container.columnconfigure(0, weight=1)

        ttk.Label(self._rules_container, text="图片列表", style="Section.TLabel").pack(
            anchor=tk.W, padx=4, pady=(4, 4)
        )
        self._gallery_holder = ttk.Frame(self._rules_container)
        self._gallery_holder.pack(fill=tk.X, padx=2)
        self._gallery_placeholder = ttk.Label(
            self._gallery_holder,
            text="尚未加载图片。请先选择 HTML 文件并点击「🖼️ 加载所有图片」。",
            style="Muted.TLabel",
        )
        self._gallery_placeholder.pack(anchor=tk.W, padx=8, pady=10)

        ttk.Label(
            self._rules_container,
            text="代码精确替换（可选）",
            style="Section.TLabel",
        ).pack(anchor=tk.W, padx=4, pady=(14, 4))
        self._code_rules_holder = ttk.Frame(self._rules_container)
        self._code_rules_holder.pack(fill=tk.X, padx=2, pady=(0, 8))

        status_bar = ttk.Frame(self, style="Card.TFrame", padding=(12, 6))
        status_bar.pack(fill=tk.X, side=tk.BOTTOM)
        ttk.Label(status_bar, textvariable=self.status_text, style="Status.TLabel").pack(
            anchor=tk.W
        )

    def _browse_html(self) -> None:
        path = filedialog.askopenfilename(
            title="选择 HTML 文件",
            filetypes=[("HTML", "*.html *.htm"), ("所有文件", "*.*")],
        )
        if path:
            self.html_path.set(path)
            self._refresh_detection(path)
            # 换文件后已加载的图片缓存失效
            if self._tiles or self._image_entries:
                self._clear_gallery()
                ttk.Label(
                    self._gallery_holder,
                    text="文件已更换，请重新点击「🖼️ 加载所有图片」。",
                    style="Muted.TLabel",
                ).pack(anchor=tk.W, padx=8, pady=10)
                self.gallery_summary.set("尚未加载图片")
            self._loaded_html = None
            self._loaded_html_path = None

    def _read_html_text(self, path: str) -> Optional[str]:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                return f.read()
        except OSError as e:
            messagebox.showerror("错误", f"无法读取 HTML 文件：\n{e}")
            return None

    def _refresh_detection(self, path: str) -> None:
        """读取 HTML 并刷新自动检测显示。"""
        if not path or not os.path.isfile(path):
            self.detected_text.set("自动检测：文件不存在")
            self.status_text.set("就绪 · 请选择 HTML 文件")
            return
        html_text = self._read_html_text(path)
        if html_text is None:
            self.detected_text.set("自动检测：读取失败")
            return
        fields = detect_adapter_fields(html_text)
        size_kb = os.path.getsize(path) / 1024
        if fields:
            self.detected_text.set(
                f"✅ 已识别 adapter 字段：{'、'.join(fields)}（共 {len(fields)} 个，将自动处理）"
            )
        else:
            self.detected_text.set("ℹ️ 未识别到 adapter 字段，将仅处理整份 HTML 中的 data URL")
        self.status_text.set(f"已加载 · {os.path.basename(path)} · {size_kb:.1f} KB")

    def _export_html_adapter_zip(self) -> None:
        path = self.html_path.get().strip()
        if not path or not os.path.isfile(path):
            messagebox.showerror("错误", "请先选择有效的 HTML 文件。")
            return

        html_text = self._read_html_text(path)
        if html_text is None:
            return

        fields = detect_adapter_fields(html_text)
        if not fields:
            messagebox.showinfo("提示", "未在 HTML 中识别到 adapter 字段，无法导出。")
            return

        out_dir = filedialog.askdirectory(title="选择保存解码内容的文件夹")
        if not out_dir:
            return

        base = os.path.splitext(os.path.basename(path))[0]
        outputs: List[str] = []
        for field in fields:
            n = decode_html_adapter_zip(html_text, out_dir, base, field)
            if n:
                clean = field.replace("window.", "").replace(".", "_")
                safe = base.replace(" ", "_").replace("/", "_")
                zip_path = os.path.join(out_dir, f"{safe}.{clean}.zip")
                json_path = os.path.join(out_dir, f"{safe}.{clean}.json")
                if os.path.isfile(zip_path):
                    outputs.append(f"• {field} → {os.path.basename(zip_path)} （已解压）")
                elif os.path.isfile(json_path):
                    outputs.append(f"• {field} → {os.path.basename(json_path)}")

        if not outputs:
            messagebox.showinfo("提示", "未能成功解码任何字段。")
            return

        self.status_text.set(f"导出完成 · {len(outputs)} 个字段 → {out_dir}")
        messagebox.showinfo(
            "完成",
            f"已导出到：\n{out_dir}\n\n" + "\n".join(outputs),
        )

    def _export_html_media(self) -> None:
        path = self.html_path.get().strip()
        if not path or not os.path.isfile(path):
            messagebox.showerror("错误", "请先选择有效的 HTML 文件。")
            return

        out_dir = filedialog.askdirectory(title="选择导出图片 / 音频的文件夹")
        if not out_dir:
            return

        html_text = self._read_html_text(path)
        if html_text is None:
            return

        detected = detect_adapter_fields(html_text)
        all_fields: List[str] = list(_ADAPTER_FIELD_PRESETS)
        for f in detected:
            if f not in all_fields:
                all_fields.append(f)

        source_base = os.path.splitext(os.path.basename(path))[0]
        stats = export_html_images_and_mp3(html_text, out_dir, source_base, all_fields)

        total = stats.images + stats.audio
        if total == 0:
            messagebox.showinfo(
                "提示",
                "未找到可导出的图片或音频资源。\n"
                "请确认 HTML 中含 data:image / data:audio，或 adapter 字段可解码。",
            )
            return

        dup_note = f"\n跳过重复：{stats.duplicates} 个" if stats.duplicates else ""
        self.status_text.set(
            f"导出完成 · 图片 {stats.images} · 音频 {stats.audio} → {out_dir}"
        )
        messagebox.showinfo(
            "完成",
            f"已导出到：\n{out_dir}\n\n"
            f"🖼️ 图片：{stats.images} 个 → images/\n"
            f"🔊 音频：{stats.audio} 个 → audio/{dup_note}",
        )

    def _renumber_rules(self) -> None:
        for i, cf in enumerate(self._code_rules, start=1):
            cf.configure(text=f"  代码规则 #{i}  ")

    def _add_code_rule(self) -> None:
        cf = CodeRuleFrame(self._code_rules_holder, len(self._code_rules) + 1, self._remove_code_rule)
        cf.pack(fill=tk.X, pady=6)
        self._code_rules.append(cf)
        self._canvas.update_idletasks()
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))

    def _remove_code_rule(self, cf: CodeRuleFrame) -> None:
        cf.destroy()
        self._code_rules.remove(cf)
        self._renumber_rules()
        self._canvas.update_idletasks()
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))

    # -------- 图片列表 / 加载 --------

    def _clear_gallery(self) -> None:
        for w in self._gallery_holder.winfo_children():
            w.destroy()
        self._tiles.clear()
        self._image_entries.clear()

    def _on_tile_change(self) -> None:
        chosen = sum(1 for e in self._image_entries if e.new_bytes is not None)
        if self._image_entries:
            self.gallery_summary.set(f"图片：{len(self._image_entries)} 张 · 待替换：{chosen} 张")
        else:
            self.gallery_summary.set("尚未加载图片")

    def _load_images(self) -> None:
        path = self.html_path.get().strip()
        if not path or not os.path.isfile(path):
            messagebox.showerror("错误", "请先选择有效的 HTML 文件。")
            return
        html = self._read_html_text(path)
        if html is None:
            return

        self.status_text.set("正在扫描图片…")
        self.update_idletasks()

        entries = scan_html_images(html)
        self._clear_gallery()
        self._loaded_html = html
        self._loaded_html_path = path
        self._image_entries = entries

        if not entries:
            ttk.Label(
                self._gallery_holder,
                text="未在 HTML 中识别到可替换的图片。",
                style="Muted.TLabel",
            ).pack(anchor=tk.W, padx=8, pady=10)
            self.gallery_summary.set("尚未加载图片")
            self.status_text.set("扫描完成 · 未找到可替换图片")
            return

        if not _HAS_PIL:
            ttk.Label(
                self._gallery_holder,
                text="⚠️ 未安装 Pillow，无法显示缩略图。建议执行：pip install pillow",
                style="Muted.TLabel",
                foreground=UI_DANGER,
            ).pack(anchor=tk.W, padx=8, pady=(4, 8))

        for entry in entries:
            tile = ImageTile(self._gallery_holder, entry, self._on_tile_change)
            tile.pack(fill=tk.X, padx=4, pady=4)
            self._tiles.append(tile)

        self._on_tile_change()
        self.status_text.set(f"已识别 {len(entries)} 张图片 · 可点击单张图片进行替换")
        self._canvas.update_idletasks()
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))
        self._canvas.yview_moveto(0)

    # -------- 生成新 HTML --------

    def _generate(self) -> None:
        path = self.html_path.get().strip()
        if not path:
            messagebox.showerror("错误", "请先选择原始 HTML 文件。")
            return

        # 优先使用 _load_images 缓存的 HTML（避免重复读盘）；若用户加载后又改了文件路径，则重新读。
        if self._loaded_html is not None and self._loaded_html_path == path:
            html = self._loaded_html
        else:
            loaded = self._read_html_text(path)
            if loaded is None:
                return
            html = loaded

        code_rules, err_c = collect_valid_code_rules(self._code_rules)
        if err_c:
            messagebox.showerror("错误", err_c)
            return

        pending_images = [e for e in self._image_entries if e.new_bytes is not None]
        if not pending_images and not code_rules:
            messagebox.showinfo("提示", "尚未选择任何要替换的图片，也没有代码规则。")
            return

        self.status_text.set("处理中 · 应用图片替换与代码规则…")
        self.update_idletasks()

        reports: list[str] = []
        total_img = 0
        total_code = 0

        if pending_images:
            html, total_img = apply_image_replacements(html, self._image_entries)
            reports.append(f"图片替换：{total_img} 张（选中 {len(pending_images)} 张）")

        for i, (old, new) in enumerate(code_rules, start=1):
            html, n = replace_literal_exact(html, old, new)
            total_code += n
            reports.append(f"代码规则 #{i}：{n} 处")

        total_all = total_img + total_code

        default_name = os.path.splitext(os.path.basename(path))[0] + "_replaced.html"
        out = filedialog.asksaveasfilename(
            title="保存新 HTML",
            defaultextension=".html",
            initialfile=default_name,
            filetypes=[("HTML", "*.html"), ("所有文件", "*.*")],
        )
        if not out:
            self.status_text.set("已取消保存")
            return
        try:
            with open(out, "w", encoding="utf-8", newline="\n") as f:
                f.write(html)
        except OSError as e:
            messagebox.showerror("错误", f"无法写入文件:\n{e}")
            return

        msg = "\n".join(reports) if reports else "（无有效操作）"
        self.status_text.set(f"✅ 完成 · 合计替换 {total_all} 处 → {os.path.basename(out)}")
        messagebox.showinfo(
            "完成",
            f"已保存：\n{out}\n\n{msg}\n\n合计：{total_all} 处",
        )


def main() -> None:
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
