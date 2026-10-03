"""Line icons, bundled as SVG path data and tinted at run time.

One consistent set (24×24, 2 px round strokes, in the style of Lucide), so no
image files have to ship and every icon can take any colour from the theme.
"""

from __future__ import annotations

import tempfile
from functools import lru_cache
from pathlib import Path

from PySide6.QtCore import QByteArray, QSize, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

_ICONS: dict[str, str] = {
    "menu": '<path d="M4 6h16M4 12h16M4 18h16"/>',
    "dashboard": '<rect x="3" y="3" width="7" height="9" rx="1"/><rect x="14" y="3" width="7" height="5" rx="1"/>'
    '<rect x="14" y="12" width="7" height="9" rx="1"/><rect x="3" y="16" width="7" height="5" rx="1"/>',
    "search": '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
    "bookmark": '<path d="M6 3h12a1 1 0 0 1 1 1v17l-7-4-7 4V4a1 1 0 0 1 1-1z"/>',
    "wand": '<path d="m3 21 12-12"/><path d="m15 9 2-2"/><path d="M19 3v4M17 5h4"/><path d="M9 3v2M8 4h2"/>'
    '<path d="M20 13v3M18.5 14.5h3"/>',
    "files": '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/>'
    '<path d="M9 13h6M9 17h6"/>',
    "shield": '<path d="M12 3 5 6v5c0 4.5 3 8.3 7 10 4-1.700 7-5.500 7-10V6z"/><path d="m9 12 2 2 4-4"/>',
    "settings": '<circle cx="12" cy="12" r="3"/><path d="M19.400 15a1.650 1.650 0 0 0 .33 1.820l.06.060a2 2 0 1 1-2.830 '
    "2.830l-.060-.060a1.650 1.650 0 0 0-1.820-.330 1.650 1.650 0 0 0-1 1.510V21a2 2 0 1 1-4 0v-.090a1.650 1.650 0 0 "
    "0-1-1.510 1.650 1.650 0 0 0-1.820.330l-.060.060a2 2 0 1 1-2.830-2.830l.060-.060a1.650 1.650 0 0 0 .330-1.820 1.650 "
    "1.650 0 0 0-1.510-1H3a2 2 0 1 1 0-4h.090a1.650 1.650 0 0 0 1.510-1 1.650 1.650 0 0 0-.330-1.820l-.060-.060a2 2 0 "
    "1 1 2.830-2.830l.060.060a1.650 1.650 0 0 0 1.820.330h0a1.650 1.650 0 0 0 1-1.510V3a2 2 0 1 1 4 0v.090a1.650 1.650 "
    "0 0 0 1 1.510h0a1.650 1.650 0 0 0 1.820-.330l.060-.060a2 2 0 1 1 2.830 2.830l-.060.060a1.650 1.650 0 0 0-.330 "
    '1.820v0a1.650 1.650 0 0 0 1.510 1H21a2 2 0 1 1 0 4h-.090a1.650 1.650 0 0 0-1.510 1z"/>',
    "logout": '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><path d="m16 17 5-5-5-5"/><path d="M21 12H9"/>',
    "refresh": '<path d="M21 12a9 9 0 0 1-15.500 6.200L3 16"/><path d="M3 21v-5h5"/>'
    '<path d="M3 12a9 9 0 0 1 15.500-6.200L21 8"/><path d="M21 3v5h-5"/>',
    "copy": '<rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 '
    '1 2 2v1"/>',
    "check": '<path d="m5 12 5 5 9-10"/>',
    "check-circle": '<circle cx="12" cy="12" r="9"/><path d="m8 12 3 3 5-6"/>',
    "trash": '<path d="M3 6h18"/><path d="M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2"/>'
    '<path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6M14 11v6"/>',
    "external": '<path d="M15 3h6v6"/><path d="M10 14 21 3"/><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 '
    '1 2-2h6"/>',
    "download": '<path d="M12 3v12"/><path d="m7 10 5 5 5-5"/><path d="M5 21h14"/>',
    "upload": '<path d="M12 15V3"/><path d="m7 8 5-5 5 5"/><path d="M5 21h14"/>',
    "file-text": '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/>'
    '<path d="M9 13h6M9 17h4"/>',
    "file-user": '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/>'
    '<circle cx="12" cy="13" r="2"/><path d="M8.500 18.500a3.500 3.500 0 0 1 7 0"/>',
    "play": '<path d="M7 4v16l13-8z" fill="currentColor"/>',
    "stop": '<rect x="6" y="6" width="12" height="12" rx="1.500" fill="currentColor"/>',
    "x": '<path d="M6 6l12 12M18 6 6 18"/>',
    "alert": '<path d="M12 3 2 20h20z"/><path d="M12 10v4"/><path d="M12 17v.010"/>',
    "info": '<circle cx="12" cy="12" r="9"/><path d="M12 11v5"/><path d="M12 8v.010"/>',
    "chevron-left": '<path d="m15 6-6 6 6 6"/>',
    "chevron-right": '<path d="m9 6 6 6-6 6"/>',
    "chevron-down": '<path d="m6 9 6 6 6-6"/>',
    "chevron-up": '<path d="m6 15 6-6 6 6"/>',
    "arrow-left": '<path d="M19 12H5"/><path d="m11 6-6 6 6 6"/>',
    "arrow-right": '<path d="M5 12h14"/><path d="m13 6 6 6-6 6"/>',
    "reload": '<path d="M21 12a9 9 0 1 1-2.600-6.400L21 8"/><path d="M21 3v5h-5"/>',
    "eye": '<path d="M2 12s3.500-7 10-7 10 7 10 7-3.500 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
    "eye-off": '<path d="M3 3l18 18"/><path d="M10.600 5.100A10.500 10.500 0 0 1 12 5c6.500 0 10 7 10 7a17 17 0 0 1-3.200 '
    '4.100"/><path d="M6.300 6.300A17 17 0 0 0 2 12s3.500 7 10 7a10 10 0 0 0 4.200-.900"/><path d="M9.900 9.900a3 3 0 0 0 4.200 '
    '4.200"/>',
    "user": '<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>',
    "users": '<circle cx="9" cy="8" r="3.500"/><path d="M2.500 20a6.500 6.500 0 0 1 13 0"/>'
    '<path d="M16 4.500a3.500 3.500 0 0 1 0 7"/><path d="M18 14.500a6.500 6.500 0 0 1 3.500 5.500"/>',
    "clock": '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    "send": '<path d="M21 3 10 14"/><path d="M21 3 14 21l-4-7-7-4z"/>',
    "star": '<path d="m12 3 2.800 5.700 6.200.900-4.500 4.400 1.100 6.200L12 17.300l-5.600 2.900 1.100-6.200L3 9.600l6.200-.900z"/>',
    "folder": '<path d="M3 6a2 2 0 0 1 2-2h4l2 3h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
    "cloud": '<path d="M7 18a4.500 4.500 0 0 1-.500-8.970A6 6 0 0 1 18 10a4 4 0 0 1 0 8z"/>',
    "cloud-check": '<path d="M7 18a4.500 4.500 0 0 1-.500-8.970A6 6 0 0 1 18 10a4 4 0 0 1 0 8z"/><path d="m9.500 13.500 2 2 3.500-4"/>',
    "cloud-off": '<path d="M3 3l18 18"/><path d="M7 18a4.500 4.500 0 0 1-1.800-8.600"/>'
    '<path d="M9.500 5.600A6 6 0 0 1 18 10a4 4 0 0 1 2.600 7"/><path d="M7 18h9"/>',
    "globe": '<circle cx="12" cy="12" r="9"/><path d="M3 12h18"/><path d="M12 3a14 14 0 0 1 0 18 14 14 0 0 1 0-18z"/>',
    "zap": '<path d="M13 2 4 14h7l-1 8 9-12h-7z"/>',
    "browser": '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 9h18"/><path d="M7 6.500v.010M10 6.500v.010"/>',
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "minus": '<path d="M5 12h14"/>',
    "skip": '<path d="M5 5v14l9-7z"/><path d="M19 5v14"/>',
    "circle": '<circle cx="12" cy="12" r="8"/>',
    "dot": '<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="4" fill="currentColor"/>',
    "list": '<path d="M8 6h13M8 12h13M8 18h13"/><path d="M3.500 6v.010M3.500 12v.010M3.500 18v.010"/>',
    "calendar": '<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M3 10h18"/><path d="M8 3v4M16 3v4"/>',
    "lock": '<rect x="4" y="11" width="16" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/>',
    "zoom-in": '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.500-3.500"/><path d="M11 8v6M8 11h6"/>',
    "zoom-out": '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.500-3.500"/><path d="M8 11h6"/>',
    "braces": '<path d="M8 3H7a2 2 0 0 0-2 2v4a2 2 0 0 1-2 2 2 2 0 0 1 2 2v4a2 2 0 0 0 2 2h1"/>'
    '<path d="M16 3h1a2 2 0 0 1 2 2v4a2 2 0 0 0 2 2 2 2 0 0 0-2 2v4a2 2 0 0 1-2 2h-1"/>',
    "link": '<path d="M10 14a4 4 0 0 0 5.700 0l3-3a4 4 0 0 0-5.700-5.700l-1 1"/>'
    '<path d="M14 10a4 4 0 0 0-5.700 0l-3 3a4 4 0 0 0 5.700 5.700l1-1"/>',
}

# File-type icons carry their own colours: red for PDF, blue for Word.
_FILE_ICONS: dict[str, tuple[str, str]] = {"pdf": ("#DC2626", "PDF"), "docx": ("#2563EB", "DOC")}


def _svg(name: str, color: str) -> bytes:
    if name in _FILE_ICONS:
        tint, label = _FILE_ICONS[name]
        body = (
            f'<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" fill="#FFFFFF" stroke="{tint}" '
            f'stroke-width="1.600"/><path d="M14 2v6h6" fill="none" stroke="{tint}" stroke-width="1.600"/>'
            f'<rect x="1.500" y="11" width="15" height="8" rx="1.500" fill="{tint}"/>'
            f'<text x="9" y="17.300" font-family="Arial, sans-serif" font-size="6" font-weight="700" fill="#FFFFFF" '
            f'text-anchor="middle">{label}</text>'
        )
        return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">{body}</svg>'.encode()

    body = _ICONS.get(name) or _ICONS["circle"]
    return (
        (
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="{color}" '
            f'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" color="{color}">{body}</svg>'
        )
        .replace("currentColor", color)
        .encode()
    )


@lru_cache(maxsize=512)
def pixmap(name: str, color: str = "#0F172A", size: int = 18, scale: float = 2.0) -> QPixmap:
    """The icon as a pixmap, rendered at `scale`× for sharp high-DPI output."""
    renderer = QSvgRenderer(QByteArray(_svg(name, color)))
    side = max(1, int(size * scale))
    image = QPixmap(side, side)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    renderer.render(painter)
    painter.end()
    image.setDevicePixelRatio(scale)
    return image


def icon(name: str, color: str = "#0F172A", size: int = 18) -> QIcon:
    return QIcon(pixmap(name, color, size))


def icon_size(size: int = 18) -> QSize:
    return QSize(size, size)


@lru_cache(maxsize=32)
def icon_file(name: str, color: str, size: int = 12) -> str:
    """The icon written to a temp PNG, for use from QSS (`image: url(...)`)."""
    folder = Path(tempfile.gettempdir()) / "resume-ai-icons"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}-{color.strip('#')}-{size}.png"
    if not path.exists():
        source = pixmap(name, color, size, 2.0)
        source.save(str(path), "PNG")
    return str(path).replace("\\", "/")


def names() -> list[str]:
    return sorted([*_ICONS, *_FILE_ICONS])
