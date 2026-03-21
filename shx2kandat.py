#!/usr/bin/env python3
"""
make_kandat.py — AutoCAD SHX BigFont → KANDAT.DAT + KANDAT2.DAT
================================================================
以 extfont.shx + extfont2.shx 產生 KANDAT 格式漢字向量資料。

SHX opcode 規格（已由 AutoCAD R12 實際編譯結果驗證）：
  0x00           END
  0x01           DrawON   (落筆)
  0x02           DrawOFF  (抬筆)
  0x03 n         scale÷n  (n=u8整數)
  0x04 n         scale×n  (n=u8整數)
  0x05           PUSH
  0x06           POP
  0x07 lo [hi]   CALL subshape  (一般字型:1byte, BigFont:2bytes LE)
  0x08 dx dy     XY 位移 (各1個有號位元組)
  0x09 …(0,0)    XY 序列 (有號位元組對，以00 00結束)
  0x0A r oct     ARC (r=半徑u8, oct=八分弧位元組)
  0x0B …         分數弧 (略過4bytes)
  0x0C           條件: skip next if NOT vertical (水平時執行)
  0x0D           條件: skip next if vertical     (水平時跳過)
  0x0E           SKIP-NEXT-IF-HORIZONTAL — 水平模式(KANDAT永遠是水平)
                 → 消耗並丟棄下一條完整指令+其參數
  0x0F           SKIP-NEXT-IF-VERTICAL  — 垂直模式跳過
                 → 水平模式為 NOP
  0x10–0xFF      向量位元組 0LN: L=長度(1..F hex), N=方向(0..F hex)

方向對照(16方向):
  0=E 1=ENE 2=NE 3=NNE 4=N 5=NNW 6=NW 7=WNW
  8=W 9=WSW A=SW B=SSW C=S D=SSE E=SE F=ESE
"""
from __future__ import annotations
import os, struct, argparse, math

# ══════════════════════════════════════════════════════════════
# 常數
# ══════════════════════════════════════════════════════════════
FONT_DIR  = r'.'
BASE_SHX  = 'extfont.shx'
BIG_SHX   = 'extfont2.shx'
OUTPUT1   = 'KANDAT.DAT'
OUTPUT2   = 'KANDAT2.DAT'

REC_SIZE  = 32
INTS_PER  = 16
DATA_INTS = 15
GRID      = 32.0
MAX_RECS  = 8
MAX_SLOT1 = 3693
MAX_SLOT2 = 3572

# 16方向向量（單位向量，斜向分量取整數近似）
_DIR16 = [
    ( 1,    0   ),  # 0  E
    ( 1,    0.5 ),  # 1  ENE
    ( 1,    1   ),  # 2  NE
    ( 0.5,  1   ),  # 3  NNE
    ( 0,    1   ),  # 4  N
    (-0.5,  1   ),  # 5  NNW
    (-1,    1   ),  # 6  NW
    (-1,    0.5 ),  # 7  WNW
    (-1,    0   ),  # 8  W
    (-1,   -0.5 ),  # 9  WSW
    (-1,   -1   ),  # A  SW
    (-0.5, -1   ),  # B  SSW
    ( 0,   -1   ),  # C  S
    ( 0.5, -1   ),  # D  SSE
    ( 1,   -1   ),  # E  SE
    ( 1,   -0.5 ),  # F  ESE
]

# ══════════════════════════════════════════════════════════════
# ZKC 非漢字 JIS 範圍
# ══════════════════════════════════════════════════════════════
ZKC = [
    0x2120, 0x217F,
    0x2220, 0x222F,
    0x232F, 0x233A,
    0x2340, 0x235B,
    0x2360, 0x237B,
    0x2420, 0x2474,
    0x2520, 0x2577,
    0x2620, 0x2639,
    0x2640, 0x2659,
    0x2720, 0x2742,
    0x2750, 0x2772,
]

# ══════════════════════════════════════════════════════════════
# JIS / Shift-JIS 對照
# ══════════════════════════════════════════════════════════════
def mstojis(mscode: int) -> int:
    il = mscode & 0xFF
    ih = (mscode >> 8) & 0xFF
    ihh = 2*(ih-129)+33 if ih <= 159 else 2*(ih-224)+95
    if il >= 159: ihh += 1
    if   64  <= il <= 126: ill = il - 31
    elif 128 <= il <= 158: ill = il - 32
    elif 159 <= il <= 252: ill = il - 126
    else: return 0
    return (ihh << 8) | ill

def jistoxy(jiscode: int) -> int:
    hcd = (jiscode >> 8) & 0xFF
    lcd =  jiscode       & 0xFF
    ktype = 0
    if 0x2120 < jiscode < 0x277E: ktype = 1
    if 0x30 <= hcd <= 0x4F:       ktype = 2
    if 0x50 <= hcd <= 0x75:       ktype = 3
    if 0x7620 < jiscode < 0x76D0: ktype = 4
    if ktype == 1:
        kcbase = 0
        for i in range(1, len(ZKC), 2):
            if ZKC[i-1] < jiscode < ZKC[i]:
                return jiscode - ZKC[i-1] + kcbase
            kcbase += ZKC[i] - ZKC[i-1] - 1
        return 0
    elif ktype == 2:
        if not (0x21 <= lcd <= 0x7E): return 0
        return (hcd-0x30)*94 + (lcd-0x20) + 453
    elif ktype == 3:
        if not (0x21 <= lcd <= 0x7E): return 0
        return (hcd-0x50)*94 + (lcd-0x20) + 4000
    elif ktype == 4:
        return jiscode - 0x7620 + 3518
    return 0

def build_xycode_maps() -> tuple[dict, dict]:
    map1: dict[int, int] = {}
    map2: dict[int, int] = {}
    for hi in range(0x81, 0xF0):
        for lo in list(range(0x40, 0x7F)) + list(range(0x80, 0xFD)):
            try:
                ch = bytes([hi, lo]).decode('cp932')
            except (UnicodeDecodeError, ValueError):
                continue
            if len(ch) != 1: continue
            ucp     = ord(ch)
            jiscode = mstojis((hi << 8) | lo)
            if not jiscode: continue
            xycode  = jistoxy(jiscode)
            if xycode <= 0: continue
            if xycode > 4000:
                map2.setdefault(xycode-4000, ucp)
            else:
                map1.setdefault(xycode, ucp)
    return map1, map2

# ══════════════════════════════════════════════════════════════
# SHX BigFont 解析器
# ══════════════════════════════════════════════════════════════
class ShxFont:
    """AutoCAD SHX BigFont 解析器。

    BigFont 二進位標頭格式（已由 shx_diag.py 診斷工具逆向確認）：
      bytes 0..pos_1a-1  ASCII 簽名 "AutoCAD-86 bigfont 1.0\\r\\n"
      byte  pos_1a       0x1A (DOS EOF 標記)
      2 bytes            未知 (08 00)
      u16 LE             nshapes (形狀總數)
      u8                 nranges (Shift-JIS 逸出範圍數)
      nranges×4 bytes    逸出範圍 [00 lead_start 00 lead_end]
      3 bytes            終止符 00 00 00
      index_start:       索引表 nshapes × 8 bytes
                         [defbytes u16, file_offset u32, shapeno u16]
      data_start:        形狀資料區 (file_offset 為相對偏移)
    """
    def __init__(self, path: str):
        self.path     = path
        self.shapes   : dict[int, tuple[int,int]] = {}  # {shapeno: (abs_off, defbytes)}
        self.raw      : bytes = b''
        self.above    = 21
        self.below    = 7
        self.advance  = 28
        self._parse()

    def get_shape_data(self, shapeno: int) -> bytes:
        entry = self.shapes.get(shapeno)
        if entry is None: return b''
        abs_off, defbytes = entry
        return self.raw[abs_off : abs_off + defbytes]

    def _parse(self):
        with open(self.path, 'rb') as f:
            self.raw = f.read()
        raw  = self.raw
        size = len(raw)

        pos_1a = raw.find(0x1A)
        if pos_1a < 0:
            print(f'  [警告] {self.path}: 找不到 0x1A')
            return
        pos = pos_1a + 1

        pos += 2   # 跳過未知 2 bytes (08 00)

        if pos + 2 > size: return
        nshapes = struct.unpack_from('<H', raw, pos)[0]; pos += 2

        if pos >= size: return
        nranges = raw[pos]; pos += 1
        pos += nranges * 4   # 逸出範圍
        pos += 3             # 終止符 00 00 00

        index_start = pos
        data_start  = index_start + nshapes * 8

        p = index_start
        for _ in range(nshapes):
            if p + 8 > size: break
            defbytes    = struct.unpack_from('<H', raw, p)[0]
            file_offset = struct.unpack_from('<I', raw, p+2)[0]
            shapeno     = struct.unpack_from('<H', raw, p+6)[0]
            p += 8
            if defbytes == 0: continue
            abs_off = data_start + file_offset
            if abs_off + defbytes > size: continue
            self.shapes[shapeno] = (abs_off, defbytes)

        entry0 = self.shapes.get(0)
        if entry0:
            abs0, _ = entry0
            if abs0 + 2 <= size:
                self.above = raw[abs0]
                self.below = raw[abs0+1]
        self.advance = (self.above + self.below) or 28

# ══════════════════════════════════════════════════════════════
# 弧線近似
# ══════════════════════════════════════════════════════════════
def _arc_pts(cx, cy, r, a0_oct, n_oct, ccw=True, seg=4):
    """八分弧轉折線點列（不含起點）。"""
    a0 = a0_oct * math.pi / 4
    span = n_oct * math.pi / 4
    if not ccw: span = -span
    n = max(n_oct * seg, 1)
    return [
        (cx + r*math.cos(a0 + k/n*span),
         cy + r*math.sin(a0 + k/n*span))
        for k in range(1, n+1)
    ]

# ══════════════════════════════════════════════════════════════
# _skip_cmd: 跳過一條完整指令（含參數）
# ══════════════════════════════════════════════════════════════
def _skip_cmd(data: bytes, i: int) -> int:
    """從位置 i 跳過一條完整指令，傳回跳過後的新位置。
    用於處理 0x0E (SKIP-NEXT-IF-HORIZONTAL)。
    """
    if i >= len(data): return i
    op = data[i]; i += 1
    if   op == 0x00: i -= 1   # END — 不跳，讓外層迴圈處理
    elif op == 0x01: pass      # DrawON:  0 param
    elif op == 0x02: pass      # DrawOFF: 0 param
    elif op == 0x03: i += 1    # scale÷n: 1 param
    elif op == 0x04: i += 1    # scale×n: 1 param
    elif op == 0x05: pass      # PUSH
    elif op == 0x06: pass      # POP
    elif op == 0x07: i += 2    # CALL: BigFont 2-byte shapeno
    elif op == 0x08: i += 2    # XY: dx, dy
    elif op == 0x09:            # XY sequence: until (0,0)
        while i+1 < len(data):
            dx=data[i]; dy=data[i+1]; i+=2
            if dx==0 and dy==0: break
    elif op == 0x0A: i += 2    # ARC: r, oct
    elif op == 0x0B: i += 4    # 分數弧: 4 bytes
    elif op in (0x0C, 0x0D, 0x0E, 0x0F): pass  # 0 param conditionals
    elif op >= 0x10: pass       # vector byte: 0 param
    return i

# ══════════════════════════════════════════════════════════════
# SHX 形狀渲染器
# ══════════════════════════════════════════════════════════════
def render_shape(
    shapeno  : int,
    all_fonts: list[ShxFont],
    sc       : float = 1.0,
    depth    : int   = 0,
) -> list[tuple[float, float, bool]]:
    """執行 SHX 形狀操作碼，傳回 (x,y,pen_up) 點列。
    pen_up=True  : 筆畫起始點（移動，不畫線）
    pen_up=False : 畫線至此點
    """
    if depth > 8: return []

    data = b''
    for font in all_fonts:
        d = font.get_shape_data(shapeno)
        if d: data = d; break
    if not data: return []

    pts   : list[tuple[float,float,bool]] = []
    stack : list = []
    x = y = 0.0
    draw_on = False   # 初始: pen up (DrawOFF)
    pen_up  = True

    def _emit_move(nx, ny):
        nonlocal x, y, pen_up
        x, y = nx, ny; pen_up = True

    def _emit_draw(nx, ny):
        nonlocal x, y, pen_up
        if pen_up:
            pts.append((x, y, True))
            pen_up = False
        pts.append((nx, ny, False))
        x, y = nx, ny

    def _step(nx, ny):
        if draw_on: _emit_draw(nx, ny)
        else:       _emit_move(nx, ny)

    i = 0
    while i < len(data):
        op = data[i]; i += 1

        if op == 0x00:   # END
            break

        elif op == 0x01:  # DrawON
            draw_on = True

        elif op == 0x02:  # DrawOFF
            draw_on = False; pen_up = True

        elif op == 0x03:  # scale÷n
            if i >= len(data): break
            n = data[i]; i += 1
            if n: sc /= n

        elif op == 0x04:  # scale×n
            if i >= len(data): break
            n = data[i]; i += 1
            sc *= n

        elif op == 0x05:  # PUSH
            stack.append((x, y, sc, draw_on, pen_up))

        elif op == 0x06:  # POP
            if stack:
                x, y, sc, draw_on, pen_up = stack.pop()

        elif op == 0x07:  # CALL subshape (BigFont: 2-byte LE shapeno)
            if i+2 > len(data): break
            sub_lo = data[i]; sub_hi = data[i+1]; i += 2
            subno  = sub_lo | (sub_hi << 8)
            if subno == 0: continue   # shape 0 = metrics, NOP call
            sub_pts = render_shape(subno, all_fonts, sc, depth+1)
            for sx, sy, spu in sub_pts:
                pts.append((x+sx, y+sy, spu))
            if sub_pts:
                lx, ly, _ = sub_pts[-1]
                x += lx; y += ly
            pen_up = True

        elif op == 0x08:  # XY displacement (signed bytes)
            if i+2 > len(data): break
            dx = struct.unpack_from('b', data, i)[0]; i += 1
            dy = struct.unpack_from('b', data, i)[0]; i += 1
            _step(x + dx*sc, y + dy*sc)

        elif op == 0x09:  # XY sequence (terminated by 0,0)
            while i+2 <= len(data):
                dx = struct.unpack_from('b', data, i)[0]; i += 1
                dy = struct.unpack_from('b', data, i)[0]; i += 1
                if dx == 0 and dy == 0: break
                _step(x + dx*sc, y + dy*sc)

        elif op == 0x0A:  # ARC (r, octant byte)
            if i+2 > len(data): break
            r       = data[i]   * sc;  i += 1
            oct_b   = data[i];          i += 1
            a0_oct  = (oct_b >> 4) & 0x07
            n_octs  =  oct_b       & 0x0F
            ccw     = not bool(oct_b & 0x80)
            if n_octs == 0: n_octs = 8
            # 計算圓心
            a0 = a0_oct * math.pi / 4
            cx_ = x - r * math.cos(a0)
            cy_ = y - r * math.sin(a0)
            for px, py in _arc_pts(cx_, cy_, r, a0_oct, n_octs, ccw):
                _step(px, py)

        elif op == 0x0B:  # 分數弧 — skip 4 bytes
            i = min(i+4, len(data))

        elif op == 0x0C:  # skip next if NOT vertical → 水平時執行 = NOP
            pass

        elif op == 0x0D:  # skip next if vertical → 水平時跳過
            i = _skip_cmd(data, i)

        elif op == 0x0E:  # SKIP-NEXT-IF-HORIZONTAL → 永遠跳過（KANDAT = 水平）
            i = _skip_cmd(data, i)

        elif op == 0x0F:  # SKIP-NEXT-IF-VERTICAL → 水平時 NOP
            pass

        elif op >= 0x10:  # 向量位元組 0LN
            L = (op >> 4) & 0x0F
            N =  op       & 0x0F
            dx_u, dy_u = _DIR16[N]
            _step(x + dx_u*L*sc, y + dy_u*L*sc)

    return pts

# ══════════════════════════════════════════════════════════════
# 座標正規化 & IP 值編碼
# ══════════════════════════════════════════════════════════════
def normalize_and_encode(
    pts   : list[tuple[float,float,bool]],
    above : int,
    below : int,
    adv   : int,
) -> list[int]:
    if not pts or adv <= 0: return []
    total_y = above + below or adv

    grid : list[tuple[int,int,bool]] = []
    prev_key = None
    for x, y, pu in pts:
        xi = max(0, min(32, round(x / adv * GRID)))
        yi = max(0, min(32, round((y+below) / total_y * GRID)))
        if pu:
            grid.append((xi, yi, True)); prev_key = None
        else:
            key = (xi, yi)
            if key != prev_key:
                grid.append((xi, yi, False)); prev_key = key

    clean : list[tuple[int,int,bool]] = []
    n = len(grid)
    for k in range(n):
        xi, yi, pu = grid[k]
        if pu:
            if k+1 < n and not grid[k+1][2]:
                clean.append((xi, yi, True))
        else:
            clean.append((xi, yi, False))

    if not clean: return []

    MAX_IPS = MAX_RECS * DATA_INTS - 1   # 119
    if len(clean) > MAX_IPS:
        step  = len(clean) / MAX_IPS
        clean = [clean[round(i*step)] for i in range(MAX_IPS)]

    return [(2 if pu else 1)*10000 + xi*100 + yi for xi,yi,pu in clean]

# ══════════════════════════════════════════════════════════════
# 記錄封裝
# ══════════════════════════════════════════════════════════════
def pack_to_records(ip_values: list[int]) -> list[list[int]]:
    max_pts = MAX_RECS * DATA_INTS - 1
    if len(ip_values) > max_pts:
        ip_values = ip_values[:max_pts]
    stream  = [len(ip_values)] + ip_values
    records = []
    for i in range(0, len(stream), DATA_INTS):
        chunk = stream[i : i+DATA_INTS]
        chunk += [0] * (DATA_INTS - len(chunk))
        records.append(chunk + [0])
    return records or [[0]*INTS_PER]

# ══════════════════════════════════════════════════════════════
# KANDAT 二進位生成
# ══════════════════════════════════════════════════════════════
def build_kandat_bytes(
    xycode_map  : dict[int,int],
    all_fonts   : list[ShxFont],
    big_font    : ShxFont,
    max_primary : int,
    verbose     : bool,
    label       : str,
) -> bytes:
    print(f'\n{"═"*60}')
    print(f'  [{label}]  主要槽號上限：{max_primary}')
    print(f'  BigFont 度量：above={big_font.above}, '
          f'below={big_font.below}, advance={big_font.advance}')
    print(f'{"═"*60}')

    ip_by_slot : dict[int,list[int]] = {}
    found = missing = empty = 0

    for slot in range(1, max_primary+1):
        ucp = xycode_map.get(slot)
        if ucp is None:
            ip_by_slot[slot] = []; empty += 1; continue
        try:
            sjis = chr(ucp).encode('cp932')
        except (UnicodeEncodeError, ValueError):
            ip_by_slot[slot] = []; missing += 1; continue

        shapeno = ((sjis[0]<<8) | sjis[1]) if len(sjis)==2 else sjis[0]
        raw_pts = render_shape(shapeno, all_fonts)
        if raw_pts:
            ips = normalize_and_encode(
                raw_pts, big_font.above, big_font.below, big_font.advance)
            found += 1
            if verbose:
                try:    ch = chr(ucp)
                except  ValueError: ch='?'
                print(f'  slot {slot:4d}  U+{ucp:04X} {ch}  '
                      f'SHX={shapeno:#06x}  {len(ips):3d}pt')
        else:
            ips = []; missing += 1

        ip_by_slot[slot] = ips

    # 鏈結記錄配置
    flat       : dict[int,list[int]] = {}
    next_extra = max_primary + 1

    for slot in range(1, max_primary+1):
        records = pack_to_records(ip_by_slot.get(slot, []))
        if len(records) == 1:
            flat[slot] = records[0]
        else:
            cont  = list(range(next_extra, next_extra+len(records)-1))
            next_extra += len(records)-1
            slots = [slot] + cont
            for idx in range(len(records)-1):
                records[idx][15] = slots[idx+1]
            records[-1][15] = 0
            for s, r in zip(slots, records):
                flat[s] = r

    max_slot  = max(flat.keys()) if flat else max_primary
    file_size = (max_slot+8) * REC_SIZE
    buf       = bytearray(file_size)
    for slot, record in flat.items():
        off = (slot+7) * REC_SIZE
        for j, val in enumerate(record):
            struct.pack_into('<h', buf, off+j*2, val)

    print(f'\n  有字符    ：{found:5d}')
    print(f'  無字符    ：{missing:5d}  (無對照：{empty})')
    print(f'  延續記錄  ：{next_extra-max_primary-1:5d}')
    print(f'  最大槽號  ：{max_slot:5d}')
    print(f'  檔案大小  ：{len(buf):,} bytes ({len(buf)//1024} KB)')
    return bytes(buf)

# ══════════════════════════════════════════════════════════════
# 程式進入點
# ══════════════════════════════════════════════════════════════
def main():
    parser = argparse.ArgumentParser(
        description='AutoCAD SHX BigFont → KANDAT.DAT + KANDAT2.DAT')
    parser.add_argument('--font-dir', default=FONT_DIR)
    parser.add_argument('--base-shx', default=BASE_SHX)
    parser.add_argument('--big-shx',  default=BIG_SHX)
    parser.add_argument('--out1', default=OUTPUT1)
    parser.add_argument('--out2', default=OUTPUT2)
    parser.add_argument('--verbose', '-v', action='store_true')
    args = parser.parse_args()

    base_path = os.path.join(args.font_dir, args.base_shx)
    big_path  = os.path.join(args.font_dir, args.big_shx)

    print('AutoCAD SHX BigFont → KANDAT 產生器')
    for path in (base_path, big_path):
        if not os.path.isfile(path):
            print(f'\n[錯誤] 找不到：{path}'); return

    print('\n載入字型...')
    base_font = ShxFont(base_path)
    big_font  = ShxFont(big_path)
    print(f'  {args.base_shx}: {len(base_font.shapes):,} 形狀')
    print(f'  {args.big_shx}:  {len(big_font.shapes):,} 形狀  '
          f'above={big_font.above} below={big_font.below}')

    all_fonts = [big_font, base_font]

    print('\n掃描 Shift-JIS 編碼空間...')
    map1, map2 = build_xycode_maps()
    print(f'  KANDAT.DAT  對照：{len(map1)} 筆')
    print(f'  KANDAT2.DAT 對照：{len(map2)} 筆')

    dat1 = build_kandat_bytes(map1, all_fonts, big_font, MAX_SLOT1, args.verbose, 'KANDAT.DAT')
    with open(args.out1,'wb') as f: f.write(dat1)
    print(f'\n→ {args.out1} 完成')

    dat2 = build_kandat_bytes(map2, all_fonts, big_font, MAX_SLOT2, args.verbose, 'KANDAT2.DAT')
    with open(args.out2,'wb') as f: f.write(dat2)
    print(f'→ {args.out2} 完成')

if __name__ == '__main__':
    main()