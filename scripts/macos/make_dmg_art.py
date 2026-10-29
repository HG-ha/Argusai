"""Generate the drag-to-Applications background for the ArgusAI DMG."""

from __future__ import annotations

import os
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = Path(__file__).resolve().parent / "dmg-ui"
ICON_CANDIDATES = (
    ROOT / "assets" / "brand" / "argusai-icon-Transparency.png",
    ROOT / "assets" / "brand" / "argusai-icon.png",
    ROOT / "assets" / "brand" / "logo.png",
)

# 2x assets for a 660x460 Finder window (drag row + Read Me).
WIDTH, HEIGHT = 1320, 920
BG = (24, 25, 27, 255)
ACCENT = (55, 148, 255, 255)
FG = (247, 248, 248, 255)
MUTED = (170, 177, 187, 255)


def _font(size: int, *, bold: bool = False) -> ImageFont.ImageFont:
    windir = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    names = []
    if bold:
        names.extend(
            [
                windir / "segoeuib.ttf",
                windir / "msyhbd.ttc",
                Path("/System/Library/Fonts/SFNS.ttf"),
                Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
            ]
        )
    names.extend(
        [
            windir / "segoeui.ttf",
            windir / "msyh.ttc",
            Path("/System/Library/Fonts/SFNS.ttf"),
            Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
            Path("/Library/Fonts/Arial.ttf"),
        ]
    )
    for path in names:
        if not path.is_file():
            continue
        try:
            return ImageFont.truetype(str(path), size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def _load_icon() -> Image.Image | None:
    for path in ICON_CANDIDATES:
        if path.is_file():
            return Image.open(path).convert("RGBA")
    return None


def _fit_icon(icon: Image.Image, box: int) -> Image.Image:
    icon = icon.copy()
    icon.thumbnail((box, box), Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", (box, box), (0, 0, 0, 0))
    canvas.paste(icon, ((box - icon.width) // 2, (box - icon.height) // 2), icon)
    return canvas


def _arrow(draw: ImageDraw.ImageDraw) -> None:
    # Centered chevron between the two Finder icons.
    x, y = 660, 370
    draw.polygon(
        [(x - 70, y - 28), (x + 36, y), (x - 70, y + 28)],
        fill=ACCENT,
    )
    draw.rectangle((x - 118, y - 14, x - 58, y + 14), fill=ACCENT)


def make_background() -> Image.Image:
    im = Image.new("RGBA", (WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(im)
    draw.rectangle((0, 0, WIDTH, 8), fill=ACCENT)
    icon = _load_icon()
    if icon is not None:
        badge = _fit_icon(icon, 56)
        glow = badge.filter(ImageFilter.GaussianBlur(6))
        im.alpha_composite(glow, (36, 36))
        im.alpha_composite(badge, (40, 40))
        draw.text((112, 48), "ArgusAI", font=_font(28, bold=True), fill=FG)
    else:
        draw.text((40, 44), "ArgusAI", font=_font(28, bold=True), fill=FG)
    _arrow(draw)
    caption = _font(22)
    text = "Drag ArgusAI to Applications"
    bbox = draw.textbbox((0, 0), text, font=caption)
    tw = bbox[2] - bbox[0]
    draw.text(((WIDTH - tw) // 2, 520), text, font=caption, fill=MUTED)
    hint = _font(18)
    extra = "Read Me  ·  使用说明"
    bbox = draw.textbbox((0, 0), extra, font=hint)
    tw = bbox[2] - bbox[0]
    draw.text(((WIDTH - tw) // 2, 820), extra, font=hint, fill=MUTED)
    return im.convert("RGB")


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    dest = OUT_DIR / "background.png"
    make_background().save(dest, format="PNG")
    print(dest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
