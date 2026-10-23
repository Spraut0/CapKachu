# -*- coding: utf-8 -*-
"""CapKachu icon — black background + yellow Pikachu-tail (Z-shape, two opposite-slanted bars)."""
import os
from PIL import Image, ImageDraw

BLACK_BG = (18, 18, 18, 255)
PIKACHU_YELLOW = (255, 203, 5, 255)
ICO_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.ico")


def create_icon() -> None:
    size = 256
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # === Black rounded background ===
    draw.rounded_rectangle([16, 16, 240, 240], radius=44, fill=BLACK_BG)

    # === Yellow Pikachu tail — single 8-point polygon (no gap between segments) ===
    tail = [
        (110, 28),    # upper-left top
        (175, 28),    # upper-right top
        (135, 110),   # connector — upper-right bottom meets lower-left top
        (200, 110),   # lower-right top
        (150, 230),   # lower-right bottom
        (85, 230),    # lower-left bottom
        (125, 130),   # connector — lower-left top meets upper-right bottom
        (60, 130),    # upper-left bottom
    ]
    draw.polygon(tail, fill=PIKACHU_YELLOW)

    # === Save as multi-size ICO ===
    icon_sizes = [(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    img.save(ICO_PATH, format="ICO", sizes=icon_sizes)
    print(f"CapKachu icon written: {ICO_PATH}")


if __name__ == "__main__":
    create_icon()
