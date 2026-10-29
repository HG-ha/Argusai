"""Generate MUI2 wizard bitmaps for the ArgusAI NSIS installer."""

from __future__ import annotations

import os
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = Path(__file__).resolve().parent / "nsis-ui"
ICON_CANDIDATES = (
    ROOT / "assets" / "brand" / "argusai-icon-Transparency.png",
    ROOT / "assets" / "brand" / "argusai-icon.png",
    ROOT / "assets" / "brand" / "logo.png",
)

BG = (24, 25, 27, 255)
ACCENT = (55, 148, 255, 255)
FG = (247, 248, 248, 255)
MUTED = (170, 177, 187, 255)
HEADER_BG = (248, 249, 250, 255)
HEADER_FG = (32, 34, 38, 255)


def _font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    windir = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    names = (
        ("segoeuib.ttf", "msyhbd.ttc", "segoeui.ttf", "msyh.ttc")
        if bold
        else ("segoeui.ttf", "msyh.ttc", "arial.ttf")
    )
    for name in names:
        path = windir / name
        if not path.is_file():
            continue
        try:
            return ImageFont.truetype(str(path), size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def _load_icon() -> Image.Image | None:
    for path in ICON_CANDIDATES:
        if not path.is_file():
            continue
        im = Image.open(path).convert("RGBA")
        return im
    return None


def _fit_icon(icon: Image.Image, box: int) -> Image.Image:
    icon = icon.copy()
    icon.thumbnail((box, box), Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", (box, box), (0, 0, 0, 0))
    x = (box - icon.width) // 2
    y = (box - icon.height) // 2
    canvas.paste(icon, (x, y), icon)
    return canvas


def make_welcome() -> Image.Image:
    im = Image.new("RGBA", (164, 314), BG)
    draw = ImageDraw.Draw(im)
    icon = _load_icon()
    if icon is not None:
        badge = _fit_icon(icon, 72)
        glow = badge.filter(ImageFilter.GaussianBlur(8))
        im.alpha_composite(glow, (46, 72))
        im.alpha_composite(badge, (46, 78))
    title = _font(22, bold=True)
    draw.text((22, 178), "ArgusAI", font=title, fill=FG)
    sub = _font(11)
    draw.text((22, 210), "Security", font=sub, fill=MUTED)
    draw.text((22, 226), "Workbench", font=sub, fill=MUTED)
    return im.convert("RGB")


def make_header() -> Image.Image:
    im = Image.new("RGBA", (150, 57), HEADER_BG)
    draw = ImageDraw.Draw(im)
    icon = _load_icon()
    if icon is not None:
        badge = _fit_icon(icon, 28)
        im.alpha_composite(badge, (10, 14))
        text_x = 46
    else:
        text_x = 14
    draw.text((text_x, 18), "ArgusAI", font=_font(16, bold=True), fill=HEADER_FG)
    return im.convert("RGB")


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    welcome = OUT_DIR / "welcome.bmp"
    header = OUT_DIR / "header.bmp"
    make_welcome().save(welcome, format="BMP")
    make_header().save(header, format="BMP")
    print(welcome)
    print(header)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
