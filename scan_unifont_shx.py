import re

SHP_FILE = "simplex.shp"
SHX_FILE = "simplex.shx"
CONST = 5  # SHP n + CONST
SHX_START = 0x48  # SHX 實際資料起點

def load_shp(shp_file):
    glyphs = []
    with open(shp_file, "r", encoding="latin-1") as f:
        for line in f:
            m = re.match(r"\*(\w+),(\d+),", line)
            if m:
                code_hex = m.group(1)
                n = int(m.group(2))
                glyphs.append((code_hex, n))
    return glyphs

SHX_START = 0x48  # SHX 實際資料起點
CONST = 10          # 原先加的額外長度

def refine_shx_offsets(shp_file, shx_file):
    glyphs = load_shp(shp_file)
    offsets = []
    offset = SHX_START
    start = SHX_START
    
    with open(shx_file, "rb") as f:
        shx_bytes = f.read()

    for code, n in glyphs:
        # 先減掉 CONST，回退 5 bytes
        #offset -= CONST  
        #n -= CONST  
       # 從 offset 開始搜尋 00 02
        while offset < len(shx_bytes)-1 and shx_bytes[offset:offset+2] != b'\x00\x02':
            offset += 1
            
        # 找到 00 02，這就是精準切點
        length = offset - start # 或其他計算方式，可依情況調整
        offsets.append((code, start, length, n))
        start = offset
        offset += n  # 為下一筆資料做準備
        offset -= CONST  # 為下一筆資料做準備
    return offsets

    
def print_table(table):
    print(f"{'Code':>6} {'Offset':>8} {'Length':>6} {'00 02?':>7}")
    for code, offset, length, ok in table:
        print(f"{code:>6} {offset:8X} {length:6} {str(ok):>7}")

if __name__ == "__main__":
    table = refine_shx_offsets("simplex.shp", "simplex.shx")
    for code, start, length, n in table:
        print(f"code=0x{code:>6} start=0x{start:08X} length={length} n={n}")
