import struct
import math

def s8(v):
    return v - 256 if v > 127 else v

def u16(data, i):
    return data[i] | (data[i+1] << 8)

def u32(data, i):
    return data[i] | (data[i+1] << 8) | (data[i+2] << 16) | (data[i+3] << 24)

class SHXDebugger:
    def __init__(self, data):
        self.data = data
        self.visited = set()
        self.glyph_offsets = {}  # shapeno -> absolute offset

    # =========================
    # 建立 glyph 偏移表
    # =========================
    def build_index_from_diag(self):
        data_start = 82997  # 從 shx_diag.py 得到
        diag_entries = [
            # defbytes, file_offset, shapeno
            (37, 82995, 0x8379),
            (54, 101686, 0x83CA),
            (70, 106263, 0x8479),
            (30, 110301, 0x88CA),
            (32, 113664, 0x89CA),
            # 可完整貼入 10369 glyph
        ]
        for defbytes, file_offset, shapeno in diag_entries:
            abs_offset = data_start + file_offset
            self.glyph_offsets[shapeno] = abs_offset
        print(f"[INFO] glyph_offsets table built, count={len(self.glyph_offsets)}")

    def lookup_offset(self, shapeno):
        return self.glyph_offsets.get(shapeno, None)

    def print_line(self, offset, size, opname, desc):
        raw = self.data[offset:offset+size]
        hexs = " ".join(f"{b:02X}" for b in raw)
        print(f"{offset:08X}: {hexs:<15} {opname:<12} {desc}")

    # =========================
    # Arc / Bulge helper
    # =========================
    def decode_arc(self, x, y, dx, dy, bulge):
        x2 = x + dx
        y2 = y + dy
        if bulge == 0:
            return ("LINE", x2, y2)
        theta = 4 * math.atan(bulge)
        return {
            "type": "ARC",
            "start": (x, y),
            "end": (x2, y2),
            "theta": theta,
            "bulge": bulge
        }

    # =========================
    # 遞迴 dump glyph
    # =========================
    def dump_glyph(self, offset, depth=0):
        if offset in self.visited:
            print("  " * depth + f"(skip recursion @0x{offset:X})")
            return
        self.visited.add(offset)
        indent = "  " * depth
        print(f"\n{indent}Glyph @0x{offset:08X}")
        print(f"{indent}" + "="*50)

        i = offset
        x = y = 0
        min_x = max_x = x
        min_y = max_y = y
        draw_on = False

        while i < len(self.data):
            op = self.data[i]
            start = i

            # -------------------------
            # END
            if op == 0x00 or op == 0x0F:
                self.print_line(start, 1, "END", "")
                break

            # -------------------------
            # LINE / DRAW ON
            elif op == 0x01:
                dx = s8(self.data[i+1])
                dy = s8(self.data[i+2])
                x += dx
                y += dy
                self.print_line(start, 3, "LINE", f"dx={dx} dy={dy} → ({x},{y})")
                i += 3

            # -------------------------
            # MOVE / DRAW OFF
            elif op == 0x02:
                dx = s8(self.data[i+1])
                dy = s8(self.data[i+2])
                x += dx
                y += dy
                self.print_line(start, 3, "MOVE", f"dx={dx} dy={dy} → ({x},{y})")
                i += 3

            # -------------------------
            # ARC / BULGE
            elif op in (0x03, 0x0C, 0x0D):
                dx = s8(self.data[i+1])
                dy = s8(self.data[i+2])
                bulge = s8(self.data[i+3])
                arc = self.decode_arc(x, y, dx, dy, bulge)
                x += dx
                y += dy
                self.print_line(start, 4, "ARC/BULGE", f"dx={dx} dy={dy} bulge={bulge} → ({x},{y})")
                i += 4

            # -------------------------
            # PUSH / POP / SCALE
            elif op == 0x05:
                self.print_line(start, 1, "PUSH", "")
                i += 1
            elif op == 0x06:
                self.print_line(start, 1, "POP", "")
                i += 1
            elif op == 0x04:
                s = self.data[i+1]
                self.print_line(start, 2, "SCALE", f"s={s}")
                i += 2

            # -------------------------
            # SUBSHAPE
            elif op in (0x07, 0x0A):
                shapeno = u16(self.data, i+1)
                self.print_line(start, 3, "SUBSHAPE", f"shapeno=0x{shapeno:04X}")
                sub_offset = self.lookup_offset(shapeno)
                if sub_offset:
                    self.dump_glyph(sub_offset, depth+1)
                else:
                    print(indent + f"  [WARN] no offset for shapeno=0x{shapeno:04X}")
                i += 3

            # -------------------------
            # Octant Arc / Fractional Arc
            elif op == 0x0B:
                self.print_line(start, 5, "FRAC_ARC", "5 bytes fractional arc")
                i += 5
            elif op == 0x0A:
                self.print_line(start, 2, "OCTANT_ARC", "2 bytes octant arc")
                i += 2

            # -------------------------
            # XY DISPLACEMENT
            elif op == 0x08:
                dx = s8(self.data[i+1])
                dy = s8(self.data[i+2])
                self.print_line(start, 3, "XY_DISP", f"dx={dx} dy={dy}")
                x += dx
                y += dy
                i += 3
            elif op == 0x09:
                # MULTIPLE XY DISPLACEMENTS, until 0,0
                j = i + 1
                while j + 1 < len(self.data):
                    dx = s8(self.data[j])
                    dy = s8(self.data[j+1])
                    j += 2
                    if dx == 0 and dy == 0:
                        break
                    x += dx
                    y += dy
                self.print_line(start, j-i, "MULTI_XY", f"end=({x},{y})")
                i = j

            # -------------------------
            # NORMAL vector / nibble encoding
            elif op & 0xF0 == 0:
                length = (op >> 4) & 0x0F
                direction = op & 0x0F
                self.print_line(start, 1, "VECTOR", f"length={length} direction={direction}")
                i += 1

            # -------------------------
            # UNKNOWN
            else:
                self.print_line(start, 1, f"UNKNOWN({op:02X})", "")
                i += 1

            # 更新 bounding box
            min_x = min(min_x, x)
            max_x = max(max_x, x)
            min_y = min(min_y, y)
            max_y = max(max_y, y)

        print(f"{indent}BOUNDING BOX: ({min_x},{min_y}) - ({max_x},{max_y})")


# =========================
# 主程式
# =========================
def main():
    filename = "extfont.shx"
    with open(filename, "rb") as f:
        data = f.read()

    print(f"[INFO] file size = {len(data)} bytes")
    dbg = SHXDebugger(data)
    dbg.build_index_from_diag()

    # dump 前 5 個 glyph
    for shapeno in list(dbg.glyph_offsets.keys())[:5]:
        offset = dbg.lookup_offset(shapeno)
        if offset:
            dbg.dump_glyph(offset)

if __name__ == "__main__":
    main()