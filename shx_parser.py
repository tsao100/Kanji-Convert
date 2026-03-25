import struct
import matplotlib.pyplot as plt

# ---------------------------
# 1️⃣ SHX mapping parser
# ---------------------------
def is_valid_charcode(code):
    # Shift-JIS 常用範圍
    return (0x8100 <= code <= 0x9FFF) or (0xE000 <= code <= 0xFCFF)

def is_valid_glyph(data, offset):
    if offset >= len(data):
        return False
    # 簡單檢查非全0
    block = data[offset:offset+8]
    return any(b != 0 for b in block)

def extract_mapping(data):
    """
    從整個 SHX 檔掃描有效 charcode → glyph offset
    """
    mapping = {}
    # 每個 mapping 可能 4 bytes: [charcode(2) + offset(2)]
    for ptr in range(len(data) - 4):
        charcode = struct.unpack_from(">H", data, ptr)[0]   # big-endian
        offset   = struct.unpack_from("<H", data, ptr+2)[0] # little-endian
        if is_valid_charcode(charcode) and is_valid_glyph(data, offset):
            mapping[charcode] = offset
    print(f"[INFO] extracted {len(mapping)} unique entries")
    return mapping

# ---------------------------
# 2️⃣ Glyph decoder
# ---------------------------
def decode_glyph(data, offset):
    """
    簡單 SHX glyph decoder，輸出多筆 stroke。
    假設：
        - 每 2 bytes = dx, dy (signed)
        - (0,0) 表示筆結束
    """
    strokes = []
    ptr = offset
    while ptr + 1 < len(data):
        stroke = []
        x, y = 0, 0
        while ptr + 1 < len(data):
            dx, dy = struct.unpack_from("bb", data, ptr)
            ptr += 2
            if dx == 0 and dy == 0:
                break
            x += dx
            y += dy
            stroke.append((x, y))
        if stroke:
            strokes.append(stroke)
        # 如果後面是全0，直接結束
        if all(b == 0 for b in data[ptr:ptr+4]):
            break
    return strokes

# ---------------------------
# 3️⃣ Plot glyph
# ---------------------------
def plot_glyph(strokes, title="Glyph"):
    plt.figure(figsize=(6,6))
    for stroke in strokes:
        xs = [pt[0] for pt in stroke]
        ys = [pt[1] for pt in stroke]
        plt.plot(xs, [-y for y in ys], linewidth=2)  # y 軸反轉
    plt.title(title, fontsize=16)
    plt.axis('equal')
    plt.axis('off')
    plt.show()

# ---------------------------
# 4️⃣ 主程式
# ---------------------------
def main():
    shx_file = "extfont2.shx"
    target_char = "見"  # 修改成你想看的漢字

    with open(shx_file, "rb") as f:
        data = f.read()

    # 解析 mapping
    mapping = extract_mapping(data)

    # UTF-8 → Shift-JIS → charcode
    try:
        sjis_bytes = target_char.encode("shift_jis")
        charcode = (sjis_bytes[0] << 8) | sjis_bytes[1]
    except Exception as e:
        print(f"[ERROR] 無法轉換 {target_char} 為 Shift-JIS: {e}")
        return

    offset = mapping.get(charcode)
    if offset is None:
        print(f"[ERROR] 找不到 {target_char} 對應的 glyph offset")
        return

    strokes = decode_glyph(data, offset)
    print(f"[INFO] {target_char} glyph 共 {len(strokes)} 筆 stroke")
    for i, s in enumerate(strokes):
        print(f"  Stroke {i+1}: {s[:10]}{'...' if len(s)>10 else ''}")

    # 可視化
    plot_glyph(strokes, title=target_char)

if __name__ == "__main__":
    main()