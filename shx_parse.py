import struct
import sys


class SHXFont:
    def __init__(self, data):
        self.data = data
        self.index = {}
        self.parse()

    # =========================
    # 基本讀取
    # =========================
    def u8(self, o):
        return self.data[o]

    def u16(self, o):
        return struct.unpack_from("<H", self.data, o)[0]

    # =========================
    # 主解析流程
    # =========================
    def parse(self):
        data = self.data
        size = len(data)

        print("[INFO] file size =", size)

        # ===== HEADER =====
        header_end = data.find(b'\x1A')
        if header_end < 0:
            header_end = 0x20

        print("[INFO] header_end =", header_end)

        pos = header_end + 1

        # ===== ESCAPE TABLE =====
        nesc = data[pos]
        pos += 1

        print("[INFO] NESC =", nesc)

        pos += nesc * 2

        print("[INFO] index_start =", pos)

        # ===== 核心：掃描 range table =====
        self.parse_range_table(pos)

    # =========================
    # 評分機制（關鍵）
    # =========================
    def score_ranges(self, ranges):
        data = self.data
        size = len(data)

        score = 0
        valid = 0
        offsets = set()

        for (start, end, offset_table_pos) in ranges:
            count = end - start + 1

            for i in range(0, min(count, 100), 3):
                off_pos = offset_table_pos + i * 2

                if off_pos + 2 > size:
                    continue

                glyph_offset = self.u16(off_pos)

                if glyph_offset == 0 or glyph_offset >= size:
                    continue

                offsets.add(glyph_offset)

                if glyph_offset + 3 > size:
                    continue

                length = self.u8(glyph_offset + 2)

                # 判斷 glyph 是否合理
                if 5 < length < 500:
                    score += 5
                    valid += 1
                else:
                    score -= 5

        # offset 分散性加分
        unique_count = len(offsets)
        score += unique_count * 2
        if unique_count < 20:
            score -= 10000  # 避免假 range

        return score, valid

    # =========================
    # 掃描 range table
    # =========================
    def parse_range_table(self, start_pos):
        data = self.data
        size = len(data)

        print("[INFO] scanning for range tables...")

        best_ranges = []
        best_score = -999999

        scan_end = min(start_pos + 20000, size - 6)

        for pos in range(start_pos, scan_end, 2):

            ranges = []
            p = pos

            for _ in range(50):  # 最多 50 個 range
                if p + 6 > size:
                    break

                start = self.u16(p)
                end = self.u16(p + 2)
                offset = self.u16(p + 4)

                # 基本合理性
                if start > end or (end - start) > 8000 or offset >= size:
                    break

                ranges.append((start, end, offset))
                p += 6

            if not ranges:
                continue

            score, valid = self.score_ranges(ranges)
            if valid < 5:
                continue

            if score > best_score:
                best_score = score
                best_ranges = ranges

        print("[INFO] best range count =", len(best_ranges))

        # 建立 glyph index
        glyphs = {}

        for (start, end, offset_table_pos) in best_ranges:
            count = end - start + 1

            for i in range(count):
                off_pos = offset_table_pos + i * 2
                if off_pos + 2 > size:
                    continue

                glyph_offset = self.u16(off_pos)
                if glyph_offset == 0 or glyph_offset >= size:
                    continue

                code = start + i
                if code not in glyphs:  # 避免覆蓋
                    glyphs[code] = glyph_offset

        self.index = glyphs

        print("[INFO] glyph count =", len(self.index))
        print("[DEBUG] unique offsets =", len(set(glyphs.values())))

    # =========================
    # 輸出 glyph
    # =========================
    def dump_glyph(self, code):
        if code not in self.index:
            print("glyph not found:", hex(code))
            return

        pos = self.index[code]

        if pos + 3 > len(self.data):
            print("invalid glyph offset")
            return

        length = self.u8(pos + 2)

        data = self.data[pos + 3: pos + 3 + length]

        print(f"glyph {hex(code)} len={length}")
        print(data[:64])


# =========================
# 測試程式
# =========================
def test_font(path):
    with open(path, "rb") as f:
        data = f.read()

    font = SHXFont(data)

    keys = sorted(font.index.keys())

    print("\n--- first 20 glyphs ---")
    for i, k in enumerate(keys[:20]):
        print(i, f"code=0x{k:04X}", "offset=", font.index[k])

    if not keys:
        print("[ERROR] no glyph found")
        return

    sample_index = min(100, len(keys) - 1)
    sample_code = keys[sample_index]

    print("\n--- sample glyph ---")
    font.dump_glyph(sample_code)


# =========================
# main
# =========================
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python shx_parse.py font.shx")
        sys.exit(0)

    test_font(sys.argv[1])

'''PS D:\Git\Kanji-Convert> python shx_parse.py extfont.shx 
[INFO] file size = 436560
[INFO] header_end = 24
[INFO] NESC = 8
[INFO] index_start = 42
[INFO] scanning for range tables...
[INFO] best range count = 50
[INFO] glyph count = 5975
[DEBUG] unique offsets = 1250

--- first 20 glyphs ---
0 code=0x0000 offset= 30017
1 code=0x40E0 offset= 62208
2 code=0x40E1 offset= 12941
3 code=0x40E2 offset= 32256
4 code=0x40E3 offset= 606
5 code=0x40E4 offset= 20736
6 code=0x40E5 offset= 10382
7 code=0x40E6 offset= 17920
8 code=0x40E7 offset= 611
9 code=0x40E8 offset= 41472
10 code=0x40E9 offset= 6286
12 code=0x40EB offset= 624
13 code=0x40EC offset= 62208
14 code=0x40ED offset= 13966
15 code=0x40EE offset= 29184
16 code=0x40EF offset= 637
17 code=0x40F0 offset= 20736
18 code=0x40F1 offset= 10383
19 code=0x40F2 offset= 15104

--- sample glyph ---
glyph 0x4143 len=230
b'0\x007\x9c\x05\x00y\xe7(\x00e\xb0\x05\x00\xca\xe72\x00\xf5\xbe\x05\x00y\xe8 \x00\xbf\xd2\x05\x00\xca\xe8@\x00\xf8\xe0\x05\x00y\xe90\x00\xe4\xf4\x05\x00\xca\xe9 \x00\xa6\x03\x06\x00y\xea*\x00b\x17\x06\x00\x00\x00'
PS D:\Git\Kanji-Convert> python shx_parse.py extfont2.shx
[INFO] file size = 490870
[INFO] header_end = 24
[INFO] NESC = 8
[INFO] index_start = 42
[INFO] scanning for range tables...
[INFO] best range count = 17
[INFO] glyph count = 5032
[DEBUG] unique offsets = 2562

--- first 20 glyphs ---
0 code=0x0000 offset= 30017
1 code=0x42FE offset= 58331
2 code=0x42FF offset= 40
3 code=0x4300 offset= 33650
4 code=0x4301 offset= 5
5 code=0x4302 offset= 58587
6 code=0x4303 offset= 40
7 code=0x4304 offset= 42776
8 code=0x4305 offset= 5
9 code=0x4306 offset= 58843
10 code=0x4307 offset= 40
11 code=0x4308 offset= 52137
12 code=0x4309 offset= 5
13 code=0x430A offset= 59099
14 code=0x430B offset= 40
15 code=0x430C offset= 61129
16 code=0x430D offset= 5
17 code=0x430E offset= 59355
18 code=0x430F offset= 44
19 code=0x4310 offset= 4419

--- sample glyph ---
glyph 0x4361 len=65
b'D-86 bigfont 1.0\r\n\x1a\x08\x00\xcd+\x02\x00\x81\x00\xa0\x00\xe0\x00\xff\x00\x00\x008\x00\x8f^\x01\x00W\x998\x00\xf6\x18\x04\x00W\x9a@\x00q:\x04\x00W\x9b \x00/[\x04'
PS D:\Git\Kanji-Convert> 
'''