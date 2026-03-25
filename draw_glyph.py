import struct
import matplotlib.pyplot as plt

# 16方向（AutoCAD標準）
DIR_TABLE = [
    (1, 0),   (1, 1),   (0, 1),   (-1, 1),
    (-1, 0),  (-1, -1), (0, -1),  (1, -1),
    (2, 0),   (2, 2),   (0, 2),   (-2, 2),
    (-2, 0),  (-2, -2), (0, -2),  (2, -2),
]

def decode_glyph(data, offset):
    x, y = 0, 0
    path = [(x, y)]

    ptr = offset

    while True:
        b = data[ptr]
        ptr += 1

        if b == 0:
            break

        length = (b >> 4) & 0xF
        direction = b & 0xF

        dx, dy = DIR_TABLE[direction]
        x += dx * length
        y += dy * length

        path.append((x, y))

    return path


def draw_glyph(path):
    xs = [p[0] for p in path]
    ys = [p[1] for p in path]

    plt.figure()
    plt.plot(xs, ys, marker='o')
    plt.gca().invert_yaxis()
    plt.axis('equal')
    plt.show()


def main():
    with open("extfont.shx", "rb") as f:
        data = f.read()

    # 手動挑一個有內容的 glyph
    offsets = [256, 1280, 14080, 9984]

    for off in offsets:
        print(f"\nDrawing glyph @ {off}")
        path = decode_glyph(data, off)
        draw_glyph(path)


if __name__ == "__main__":
    main()