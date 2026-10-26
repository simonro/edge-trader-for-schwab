"""Draw assets/companion.ico: dark tile, three candles, a rising line.

    python scripts/make_icon.py
"""
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
S = 256


def draw() -> Image.Image:
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((8, 8, S - 8, S - 8), radius=48, fill=(16, 21, 28, 255),
                        outline=(46, 58, 72, 255), width=6)
    green, red = (63, 182, 139, 255), (229, 83, 75, 255)
    # (x, wick top, body top, body bottom, wick bottom, color)
    for x, wt, bt, bb, wb, c in ((70, 120, 140, 190, 205, red),
                                 (128, 85, 100, 160, 180, green),
                                 (186, 50, 65, 125, 145, green)):
        d.line((x, wt, x, wb), fill=c, width=8)
        d.rounded_rectangle((x - 20, bt, x + 20, bb), radius=5, fill=c)
    d.line((40, 215, 110, 175, 160, 190, 222, 120), fill=(76, 141, 255, 255), width=10, joint="curve")
    return im


def main() -> None:
    out = ROOT / "assets" / "companion.ico"
    out.parent.mkdir(exist_ok=True)
    draw().save(out, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print(out)


if __name__ == "__main__":
    main()
