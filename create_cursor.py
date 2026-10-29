"""
pencil.cur 생성 — 에디터 펜 커서.
Tk 기본 "pencil" 커서(X11 글리프)는 연필 끝이 좌상단을 향해 거꾸로 보임.
→ 끝(핫스팟)이 좌하단, 몸통이 우상단으로 올라가는 연필을 4배 크기로 그려 LANCZOS로 줄임(안티앨리어싱).
실행: python create_cursor.py  → pencil.cur, pencil_preview.png
"""
import struct
from PIL import Image, ImageDraw

SIZE = 32
SS = 4               # supersampling
HOTSPOT = (3, 28)    # 연필 끝 (1x 기준)


def draw_pencil_hires() -> Image.Image:
    n = SIZE * SS
    img = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    def P(x, y):
        return (x * SS, y * SS)

    OUT = (45, 45, 45, 255)
    LW = int(1.3 * SS)  # 외곽선 ≈ 1.3px

    # 연필 축: 끝 (3,28) → 지우개 끝 (28,3). 폭 7px
    body = [P(9.5, 21.0), P(23.0, 7.5), P(28.0, 12.5), P(14.5, 26.0)]
    d.polygon(body, fill=(252, 196, 40, 255))
    # 몸통 능선 (살짝 어두운 띠)
    d.polygon([P(11.5, 23.0), P(25.0, 9.5), P(26.5, 11.0), P(13.0, 24.5)], fill=(228, 168, 24, 255))
    # 쇠테 (은색 띠)
    ferrule = [P(22.0, 8.5), P(24.5, 6.0), P(29.5, 11.0), P(27.0, 13.5)]
    d.polygon(ferrule, fill=(190, 192, 198, 255))
    # 지우개
    eraser = [P(24.5, 6.0), P(26.5, 4.0), P(31.5, 9.0), P(29.5, 11.0)]
    d.polygon(eraser, fill=(242, 140, 150, 255))
    # 깎은 나무
    wood = [P(9.5, 21.0), P(14.5, 26.0), P(5.0, 29.5), P(3.0, 28.0)]
    d.polygon(wood, fill=(236, 214, 170, 255))
    # 흑연 심
    tip = [P(6.5, 25.5), P(8.5, 27.5), P(5.0, 29.5), P(3.0, 28.0)]
    d.polygon(tip, fill=(40, 40, 40, 255))
    # 외곽선 — 전체 실루엣
    outline = [P(3.0, 28.0), P(9.5, 21.0), P(23.0, 7.5), P(24.5, 6.0), P(26.5, 4.0),
               P(31.5, 9.0), P(29.5, 11.0), P(28.0, 12.5), P(14.5, 26.0), P(5.0, 29.5)]
    d.line(outline + [outline[0]], fill=OUT, width=LW, joint="curve")
    # 내부 구분선
    d.line([P(9.5, 21.0), P(14.5, 26.0)], fill=OUT, width=max(1, LW - SS // 2))
    d.line([P(22.0, 8.5), P(27.0, 13.5)], fill=OUT, width=max(1, LW - SS // 2))
    d.line([P(24.5, 6.0), P(29.5, 11.0)], fill=OUT, width=max(1, LW - SS // 2))
    return img


def make_cursor() -> Image.Image:
    hi = draw_pencil_hires()
    # 어두운 배경에서도 보이게: 반투명 흰 글로우를 실루엣 바깥에 1px
    from PIL import ImageFilter
    alpha = hi.split()[3]
    glow_a = alpha.filter(ImageFilter.MaxFilter(2 * SS + 1)).point(lambda v: int(v * 0.55))
    glow = Image.new("RGBA", hi.size, (255, 255, 255, 0))
    glow.putalpha(glow_a)
    out = Image.alpha_composite(glow, hi)
    return out.resize((SIZE, SIZE), Image.LANCZOS)


def save_cur(img: Image.Image, path: str, hotspot: tuple[int, int]) -> None:
    """Windows .cur — PNG 압축 ICO는 커서로 못 쓰므로 32bpp DIB(알파) + AND 마스크를 직접 기록."""
    img = img.convert("RGBA")
    w, h = img.size
    px = img.load()
    xor = bytearray()
    for y in range(h - 1, -1, -1):
        for x in range(w):
            r, g, b, a = px[x, y]
            xor += bytes((b, g, r, a))
    row_bytes = ((w + 31) // 32) * 4
    and_mask = bytearray()
    for y in range(h - 1, -1, -1):
        row = bytearray(row_bytes)
        for x in range(w):
            if px[x, y][3] == 0:
                row[x // 8] |= 0x80 >> (x % 8)
        and_mask += row
    bih = struct.pack("<IiiHHIIiiII", 40, w, h * 2, 1, 32, 0, len(xor) + len(and_mask), 0, 0, 0, 0)
    image_data = bih + bytes(xor) + bytes(and_mask)
    header = struct.pack("<HHH", 0, 2, 1)
    entry = struct.pack("<BBBBHHII", w if w < 256 else 0, h if h < 256 else 0, 0, 0,
                        hotspot[0], hotspot[1], len(image_data), 6 + 16)
    with open(path, "wb") as f:
        f.write(header + entry + image_data)


if __name__ == "__main__":
    im = make_cursor()
    save_cur(im, "pencil.cur", HOTSPOT)
    # 미리보기: 밝은/어두운 배경 위 4배
    prev = Image.new("RGBA", (SIZE * 2, SIZE), (0, 0, 0, 0))
    prev.paste((235, 240, 245, 255), (0, 0, SIZE, SIZE))
    prev.paste((40, 40, 48, 255), (SIZE, 0, SIZE * 2, SIZE))
    prev.alpha_composite(im, (0, 0))
    prev.alpha_composite(im, (SIZE, 0))
    prev.resize((SIZE * 8, SIZE * 4), Image.NEAREST).save("pencil_preview.png")
    print("pencil.cur written, hotspot", HOTSPOT)
