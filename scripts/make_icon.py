"""Draw the WorldBaton logo: a block with two arrows passing around it, on a dark rounded tile.

Writes assets/worldbaton.png (window icon), assets/worldbaton.ico (exe icon) and assets/worldbaton-1024.png.
The results are committed, so building the app doesn't need Pillow. Run again after changing the design.
Needs: pip install pillow
"""
import math
from pathlib import Path

from PIL import Image, ImageDraw

K = 10                      # pixels per unit: the logo is designed on a 100x100 grid, drawn at 1000px
GREEN, TILE = "#1fbf8f", "#1b2a33"
R, STROKE = 35, 7           # arc radius and line width, in grid units
ARCS = [(212, 318), (32, 138)]  # degrees clockwise from 3 o'clock; each arrow points at its second angle
OUT = Path(__file__).resolve().parent.parent / "assets"


def mark() -> Image.Image:
    """The green block-and-arrows mark on a transparent background."""
    im = Image.new("RGBA", (100 * K, 100 * K), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    w, mid = STROKE * K, R - STROKE / 2  # Pillow draws arc strokes inward from the radius

    def dot(x, y):
        r = STROKE / 2
        d.ellipse([(x - r) * K, (y - r) * K, (x + r) * K, (y + r) * K], fill=GREEN)

    corners = [(50, 33), (65, 41.5), (65, 58.5), (50, 67), (35, 58.5), (35, 41.5)]
    d.line([(x * K, y * K) for x, y in corners + corners[:1]], fill=GREEN, width=w, joint="curve")
    d.line([(35 * K, 41.5 * K), (50 * K, 50 * K), (65 * K, 41.5 * K)], fill=GREEN, width=w, joint="curve")
    d.line([(50 * K, 50 * K), (50 * K, 67 * K)], fill=GREEN, width=w)
    for x, y in corners + [(50, 50)]:
        dot(x, y)  # round corners

    for start, end in ARCS:
        d.arc([(50 - R) * K, (50 - R) * K, (50 + R) * K, (50 + R) * K], start, end, fill=GREEN, width=w)
        s = math.radians(start)
        dot(50 + mid * math.cos(s), 50 + mid * math.sin(s))  # round tail
        a = math.radians(end)
        tx, ty = -math.sin(a), math.cos(a)  # direction of travel along the arc
        nx, ny = math.cos(a), math.sin(a)   # outward from the centre
        ex, ey = 50 + mid * nx, 50 + mid * ny
        bx, by, half = ex - tx * 3, ey - ty * 3, 10.5
        head = [(ex + tx * 11, ey + ty * 11), (bx + nx * half, by + ny * half), (bx - nx * half, by - ny * half)]
        d.polygon([(x * K, y * K) for x, y in head], fill=GREEN)
    return im


def logo() -> Image.Image:
    """The mark at 80% size on the dark rounded tile."""
    size = 100 * K
    tile = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(tile).rounded_rectangle([0, 0, size - 1, size - 1], radius=22 * K, fill=TILE)
    inner = mark().resize((int(size * 0.8),) * 2, Image.LANCZOS)
    tile.alpha_composite(inner, ((size - inner.width) // 2,) * 2)
    return tile


def main():
    img = logo()
    OUT.mkdir(exist_ok=True)
    img.resize((1024, 1024), Image.LANCZOS).save(OUT / "worldbaton-1024.png")
    img.resize((256, 256), Image.LANCZOS).save(OUT / "worldbaton.png")
    img.save(OUT / "worldbaton.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("wrote", *sorted(p.name for p in OUT.iterdir()))


if __name__ == "__main__":
    main()
