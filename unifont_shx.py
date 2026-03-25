import matplotlib.pyplot as plt

# -----------------------------
# signed nibble
# -----------------------------
def signed_nibble(n):
    return n - 16 if n >= 8 else n

def decode_vector(byte):
    dx = signed_nibble((byte >> 4) & 0xF)
    dy = signed_nibble(byte & 0xF)
    return dx, dy

# -----------------------------
# Simplex SHX Decoder
# -----------------------------
class SimplexSHX:
    def __init__(self, path):
        with open(path, "rb") as f:
            self.data = f.read()
        self.glyphs = []
        self.parse_glyph_stream()

    # -------------------------
    # 解析 glyph stream（順序 glyph）
    # -------------------------
    def parse_glyph_stream(self):
        data = self.data
        pos = 0x40  # header 結束後開始
        glyph = []

        while pos < len(data):
            b = data[pos]

            if b == 0x00:
                # glyph 結束
                if glyph:
                    self.glyphs.append(glyph)
                    glyph = []
                pos += 1
                continue

            glyph.append(b)
            pos += 1

        # 最後一個 glyph
        if glyph:
            self.glyphs.append(glyph)

        print(f"Total glyphs parsed: {len(self.glyphs)}")

    # -------------------------
    # decode single glyph
    # -------------------------
    def decode_glyph(self, glyph_bytes):
        x, y = 0, 0
        path = []
        stroke = [(x, y)]

        for b in glyph_bytes:
            if b < 0x80:
                dx, dy = decode_vector(b)
                x += dx
                y += dy
                stroke.append((x, y))
            # control bytes 可擴充（0x08 / 0x0E 等）
            elif b == 0x0E:
                # skip next 2 bytes
                continue
            elif b == 0x08:
                # vector escape, optional
                continue
            else:
                # fallback unknown control
                continue

        if len(stroke) > 1:
            path.append(stroke)
        return path

    # -------------------------
    # plot glyph by index
    # -------------------------
    def plot_glyph(self, index):
        if index >= len(self.glyphs):
            print("Invalid glyph index")
            return

        path = self.decode_glyph(self.glyphs[index])

        for stroke in path:
            xs = [p[0] for p in stroke]
            ys = [p[1] for p in stroke]
            plt.plot(xs, ys, '-k')

        plt.gca().invert_yaxis()
        plt.axis("equal")
        plt.title(f"Glyph index: {index}")
        plt.show()


# -----------------------------
# Main
# -----------------------------
if __name__ == "__main__":
    font = SimplexSHX("simplex.shx")

    # 測試 ASCII glyph 'A', 'B', 'C'
    # ASCII ' ' = index 0, '!' = 1, 'A' = 33 (0x41-0x20)
    ascii_base = 0x20
    for ch in ['A', 'B', 'C']:
        index = ord(ch) - ascii_base
        font.plot_glyph(index)