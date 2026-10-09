"""One-off script: draws the Simple-Jukebox app icon and exports every
size/format the platform installers need. Not part of the app itself and
not run automatically — re-run manually if the icon design ever changes.

Usage: venv/bin/python packaging/icons/generate_icon.py
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw

OUT_DIR = Path(__file__).parent
APP_RESOURCES = OUT_DIR.parent.parent / "src" / "simple_jukebox" / "gui" / "resources"
SIZE = 1024

# "Arched Jukebox" — the classic rounded-top jukebox silhouette framing a
# vinyl record, so it reads as a music collection rather than a radio.
PLUM = (74, 36, 92, 255)
CREAM = (246, 236, 220, 255)
AMBER = (232, 150, 52, 255)
VINYL = (28, 22, 34, 255)
GROOVE = (64, 54, 74, 255)


def draw_icon() -> Image.Image:
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    margin = SIZE * 0.06

    draw.rounded_rectangle(
        [margin, margin, SIZE - margin, SIZE - margin],
        radius=SIZE * 0.22,
        fill=PLUM,
    )

    # Jukebox cabinet: a tall arch with a cream outline.
    left, right = SIZE * 0.22, SIZE * 0.78
    top, bottom = SIZE * 0.17, SIZE * 0.84
    arch_r = (right - left) / 2
    outline = int(SIZE * 0.035)
    draw.pieslice([left, top, right, top + 2 * arch_r], 180, 360, fill=CREAM)
    draw.rectangle([left, top + arch_r, right, bottom], fill=CREAM)
    inset = outline
    draw.pieslice(
        [left + inset, top + inset, right - inset, top + 2 * arch_r - inset], 180, 360, fill=PLUM
    )
    draw.rectangle([left + inset, top + arch_r, right - inset, bottom - inset], fill=PLUM)

    # Vinyl record in the arch's window.
    cx, cy = SIZE / 2, top + arch_r * 1.02
    r = arch_r * 0.72
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=VINYL)
    for frac in (0.88, 0.74, 0.60):
        gr = r * frac
        draw.ellipse([cx - gr, cy - gr, cx + gr, cy + gr], outline=GROOVE, width=max(2, int(SIZE * 0.006)))
    lr = r * 0.36
    draw.ellipse([cx - lr, cy - lr, cx + lr, cy + lr], fill=AMBER)
    hr = r * 0.07
    draw.ellipse([cx - hr, cy - hr, cx + hr, cy + hr], fill=VINYL)

    # Speaker grille: three amber bars across the lower cabinet.
    bar_left, bar_right = left + arch_r * 0.38, right - arch_r * 0.38
    bar_h = SIZE * 0.028
    for i in range(3):
        y = bottom - SIZE * 0.20 + i * bar_h * 2.1
        draw.rounded_rectangle([bar_left, y, bar_right, y + bar_h], radius=bar_h / 2, fill=AMBER)

    return img


def main() -> None:
    icon = draw_icon()
    master_png = OUT_DIR / "icon.png"
    icon.save(master_png)
    print(f"wrote {master_png}")

    APP_RESOURCES.mkdir(parents=True, exist_ok=True)
    icon.resize((256, 256), Image.LANCZOS).save(APP_RESOURCES / "icon.png")
    print(f"wrote {APP_RESOURCES / 'icon.png'}")

    # Windows .ico (multi-resolution)
    ico_sizes = [16, 24, 32, 48, 64, 128, 256]
    ico_path = OUT_DIR / "icon.ico"
    icon.save(ico_path, sizes=[(s, s) for s in ico_sizes])
    print(f"wrote {ico_path}")

    # Linux: a few common hicolor sizes as plain PNGs.
    for s in (16, 32, 48, 64, 128, 256, 512):
        icon.resize((s, s), Image.LANCZOS).save(OUT_DIR / f"icon_{s}.png")
    print("wrote icon_16..512.png")

    # macOS .icns: iconutil on a Mac; elsewhere Pillow's own ICNS writer is
    # good enough to commit, so CI on macOS always has a file to use.
    if sys.platform == "darwin" and shutil.which("iconutil"):
        iconset = OUT_DIR / "icon.iconset"
        iconset.mkdir(exist_ok=True)
        for s in (16, 32, 128, 256, 512):
            icon.resize((s, s), Image.LANCZOS).save(iconset / f"icon_{s}x{s}.png")
            icon.resize((s * 2, s * 2), Image.LANCZOS).save(iconset / f"icon_{s}x{s}@2x.png")
        subprocess.run(
            ["iconutil", "-c", "icns", str(iconset), "-o", str(OUT_DIR / "icon.icns")],
            check=True,
        )
        for f in iconset.iterdir():
            f.unlink()
        iconset.rmdir()
    else:
        icon.save(OUT_DIR / "icon.icns")
    print(f"wrote {OUT_DIR / 'icon.icns'}")


if __name__ == "__main__":
    main()
