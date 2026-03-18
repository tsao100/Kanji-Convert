#!/usr/bin/env python3
"""
make_symbol_cod.py
==================
從 KanjiVG SVG 字體檔產生 SYMBOL.COD

SYMBOL.COD 格式（來自 BASIC 程式碼逆向分析）：
  ┌────────────────────────────────────────────────┐
  │ Lookup Table (512 bytes)                        │
  │   char_code 0..255 各佔 2 bytes (big-endian)    │
  │   值 = IADSYM = 字元資料區塊在檔案中的 0-based 位置 │
  ├────────────────────────────────────────────────┤
  │ Character Data Blocks                           │
  │   [count:1B][pen₁:1B][data₁:1B]...             │
  │   pen byte : 0 = 畫線(IPEN=2), 1 = 移動(IPEN=3) │
  │   data byte: 高4bits=X nibble, 低4bits=Y nibble  │
  │     XSYM = (data >> 4) - 1          → 範圍 -1..14 │
  │     YSYM = (data & 0xF) - 2         → 範圍 -2..13 │
  └────────────────────────────────────────────────┘

SYMBO2 座標系（DD=12）：
  X: 0..11（左→右），Y: 0..12（下→上）
KanjiVG SVG viewBox: 0 0 109 109（Y 由上往下，需翻轉）
"""

import os
import re
import math
import struct
import argparse
from xml.etree import ElementTree as ET

# ─── 常數 ────────────────────────────────────────────────────────────────────
DD = 12                   # SYMBO2 的字元格寬度（基本單位）
SVG_VB = 109.0            # KanjiVG 標準 viewBox 大小
KANJI_DIR = r'.\kanji'    # SVG 資料夾
OUTPUT    = 'SYMBOL.COD'

# BASIC 程式中 NKANF = 4472（原始檔大小），供參考
ORIGINAL_NKANF = 4472

# ─── SVG 路徑解析 ─────────────────────────────────────────────────────────────

def _nums(tokens, i):
    """從 tokens[i] 開始讀取所有連續數字，回傳 (values, new_i)"""
    vals = []
    while i < len(tokens) and not tokens[i].isalpha():
        vals.append(float(tokens[i]))
        i += 1
    return vals, i


def _cubic(p0, p1, p2, p3, steps=8):
    """取樣三次 Bézier 曲線，回傳點列（含起點）"""
    pts = []
    for k in range(steps + 1):
        t = k / steps
        m = 1 - t
        x = m**3*p0[0] + 3*m**2*t*p1[0] + 3*m*t**2*p2[0] + t**3*p3[0]
        y = m**3*p0[1] + 3*m**2*t*p1[1] + 3*m*t**2*p2[1] + t**3*p3[1]
        pts.append((x, y))
    return pts


def _quadratic(p0, p1, p2, steps=6):
    """取樣二次 Bézier 曲線"""
    pts = []
    for k in range(steps + 1):
        t = k / steps
        m = 1 - t
        x = m**2*p0[0] + 2*m*t*p1[0] + t**2*p2[0]
        y = m**2*p0[1] + 2*m*t*p1[1] + t**2*p2[1]
        pts.append((x, y))
    return pts


def _arc_approx(x0, y0, rx, ry, x_rot_deg, large_arc, sweep, x1, y1, steps=8):
    """SVG arc → 折線近似（完整橢圓弧計算）"""
    if rx == 0 or ry == 0:
        return [(x0, y0), (x1, y1)]

    phi = math.radians(x_rot_deg)
    cos_p, sin_p = math.cos(phi), math.sin(phi)

    # Step 1: 轉換到中間座標系
    dx2 = (x0 - x1) / 2
    dy2 = (y0 - y1) / 2
    x1p =  cos_p * dx2 + sin_p * dy2
    y1p = -sin_p * dx2 + cos_p * dy2

    # Adjust radii
    lam = (x1p/rx)**2 + (y1p/ry)**2
    if lam > 1:
        sq = math.sqrt(lam)
        rx *= sq; ry *= sq

    # Step 2: 求弧心
    num = max(0, rx**2*ry**2 - rx**2*y1p**2 - ry**2*x1p**2)
    den = rx**2*y1p**2 + ry**2*x1p**2
    sq  = math.sqrt(num / den) if den else 0
    if large_arc == sweep:
        sq = -sq
    cxp =  sq * rx * y1p / ry
    cyp = -sq * ry * x1p / rx

    # Step 3: 轉回原始座標系
    cx = cos_p*cxp - sin_p*cyp + (x0+x1)/2
    cy = sin_p*cxp + cos_p*cyp + (y0+y1)/2

    # Step 4: 計算角度
    def angle(ux, uy, vx, vy):
        a = math.atan2(ux*vy - uy*vx, ux*vx + uy*vy)
        return a

    th1 = angle(1, 0, (x1p-cxp)/rx, (y1p-cyp)/ry)
    dth = angle((x1p-cxp)/rx, (y1p-cyp)/ry, (-x1p-cxp)/rx, (-y1p-cyp)/ry)

    if not sweep and dth > 0:  dth -= 2*math.pi
    if sweep and dth < 0:      dth += 2*math.pi

    pts = []
    for k in range(steps + 1):
        t  = k / steps
        th = th1 + t * dth
        x  = cos_p * rx*math.cos(th) - sin_p * ry*math.sin(th) + cx
        y  = sin_p * rx*math.cos(th) + cos_p * ry*math.sin(th) + cy
        pts.append((x, y))
    return pts


def parse_svg_path(d: str):
    """
    解析 SVG path 的 d 屬性。
    回傳 list of (x, y, pen_up:bool)
      pen_up=True  → 移動（不畫線）
      pen_up=False → 畫線到此點
    """
    tokens = re.findall(
        r'[MmLlHhVvCcSsQqTtAaZz]|[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?',
        d
    )
    result = []
    cx = cy = 0.0          # 目前位置
    sx = sy = 0.0          # 子路徑起點
    prev_ctrl = None       # 前一個控制點（for S/T）
    i = 0

    while i < len(tokens):
        cmd = tokens[i]; i += 1
        rel = cmd.islower()
        C   = cmd.upper()

        if C == 'M':
            first = True
            while i < len(tokens) and not tokens[i].isalpha():
                x, y = float(tokens[i]), float(tokens[i+1]); i += 2
                if rel: x += cx; y += cy
                cx, cy = x, y
                if first:
                    sx, sy = x, y
                    result.append((x, y, True))   # pen up
                    first = False
                else:
                    result.append((x, y, False))  # implicit L
            prev_ctrl = None

        elif C == 'L':
            while i < len(tokens) and not tokens[i].isalpha():
                x, y = float(tokens[i]), float(tokens[i+1]); i += 2
                if rel: x += cx; y += cy
                cx, cy = x, y
                result.append((x, y, False))
            prev_ctrl = None

        elif C == 'H':
            while i < len(tokens) and not tokens[i].isalpha():
                x = float(tokens[i]); i += 1
                if rel: x += cx
                cx = x
                result.append((cx, cy, False))
            prev_ctrl = None

        elif C == 'V':
            while i < len(tokens) and not tokens[i].isalpha():
                y = float(tokens[i]); i += 1
                if rel: y += cy
                cy = y
                result.append((cx, cy, False))
            prev_ctrl = None

        elif C == 'C':
            while i < len(tokens) and not tokens[i].isalpha():
                x1,y1,x2,y2,x,y = (float(tokens[i+k]) for k in range(6)); i += 6
                if rel:
                    x1+=cx;y1+=cy; x2+=cx;y2+=cy; x+=cx;y+=cy
                for px,py in _cubic((cx,cy),(x1,y1),(x2,y2),(x,y))[1:]:
                    result.append((px,py,False))
                prev_ctrl = (x2,y2)
                cx,cy = x,y

        elif C == 'S':
            while i < len(tokens) and not tokens[i].isalpha():
                x2,y2,x,y = (float(tokens[i+k]) for k in range(4)); i += 4
                if rel: x2+=cx;y2+=cy; x+=cx;y+=cy
                if prev_ctrl:
                    x1 = 2*cx - prev_ctrl[0]
                    y1 = 2*cy - prev_ctrl[1]
                else:
                    x1,y1 = cx,cy
                for px,py in _cubic((cx,cy),(x1,y1),(x2,y2),(x,y))[1:]:
                    result.append((px,py,False))
                prev_ctrl = (x2,y2)
                cx,cy = x,y

        elif C == 'Q':
            while i < len(tokens) and not tokens[i].isalpha():
                x1,y1,x,y = (float(tokens[i+k]) for k in range(4)); i += 4
                if rel: x1+=cx;y1+=cy; x+=cx;y+=cy
                for px,py in _quadratic((cx,cy),(x1,y1),(x,y))[1:]:
                    result.append((px,py,False))
                prev_ctrl = (x1,y1)
                cx,cy = x,y

        elif C == 'T':
            while i < len(tokens) and not tokens[i].isalpha():
                x,y = float(tokens[i]),float(tokens[i+1]); i += 2
                if rel: x+=cx; y+=cy
                if prev_ctrl:
                    x1 = 2*cx - prev_ctrl[0]
                    y1 = 2*cy - prev_ctrl[1]
                else:
                    x1,y1 = cx,cy
                for px,py in _quadratic((cx,cy),(x1,y1),(x,y))[1:]:
                    result.append((px,py,False))
                prev_ctrl = (x1,y1)
                cx,cy = x,y

        elif C == 'A':
            while i < len(tokens) and not tokens[i].isalpha():
                rx,ry,xr,la,sw,x,y = (float(tokens[i+k]) for k in range(7)); i += 7
                if rel: x+=cx; y+=cy
                for px,py in _arc_approx(cx,cy,rx,ry,xr,int(la),int(sw),x,y)[1:]:
                    result.append((px,py,False))
                prev_ctrl = None
                cx,cy = x,y

        elif C == 'Z':
            if abs(cx-sx) > 0.5 or abs(cy-sy) > 0.5:
                result.append((sx, sy, False))
            cx,cy = sx,sy
            prev_ctrl = None

    return result


# ─── 座標轉換 ─────────────────────────────────────────────────────────────────

def svg_to_symgrid(points, vb_w=SVG_VB, vb_h=SVG_VB):
    """
    SVG (0..vb_w, 0..vb_h) → SYMBOL 格子座標
      gx: 0.0 .. DD-1  (左→右)
      gy: 0.0 .. DD    (下→上, SVG Y 需翻轉)
    """
    out = []
    for x, y, pu in points:
        gx = x / vb_w * (DD - 1)
        gy = (vb_h - y) / vb_h * DD   # Y 翻轉
        out.append((gx, gy, pu))
    return out


def quantize_and_dedupe(points):
    """量化到整數格並移除連續重複點。"""
    out = []
    prev_xy = None
    for x, y, pu in points:
        qx = int(max(-1, min(14, round(x))))
        qy = int(max(-2, min(13, round(y))))
        if pu:
            out.append((qx, qy, True))
            prev_xy = (qx, qy)
        else:
            if (qx, qy) != prev_xy:
                out.append((qx, qy, False))
                prev_xy = (qx, qy)
    return out


# ─── 編碼為 SYMBOL.COD bytes ──────────────────────────────────────────────────

def encode_strokes(points):
    """
    將量化後的點列轉為 (pen_byte, data_byte) 配對。
    pen_byte : 0 = 畫線 (IPEN→2), 1 = 移動 (IPEN→3)
    data_byte: bits[7:4]=X nibble (xsym+1), bits[3:0]=Y nibble (ysym+2)
    """
    encoded = []
    for x, y, pu in points:
        x_nib = max(0, min(15, x + 1))   # XSYM = nibble - 1
        y_nib = max(0, min(15, y + 2))   # YSYM = nibble - 2
        data_byte = (x_nib << 4) | y_nib
        pen_byte  = 1 if pu else 0
        encoded.append((pen_byte, data_byte))
    return encoded


# ─── 處理單一 SVG 檔 ──────────────────────────────────────────────────────────

def process_svg(svg_path: str):
    """讀取 KanjiVG SVG，回傳 [(pen_byte, data_byte), ...]"""
    try:
        tree = ET.parse(svg_path)
    except ET.ParseError as e:
        print(f"    [WARN] 解析失敗: {svg_path}: {e}")
        return []

    root = tree.getroot()

    # 處理命名空間
    m = re.match(r'\{([^}]*)\}', root.tag)
    ns = m.group(1) if m else ''
    tag = lambda t: f'{{{ns}}}{t}' if ns else t

    # 取得 viewBox
    vb_str = root.get('viewBox', f'0 0 {SVG_VB} {SVG_VB}')
    vb = list(map(float, vb_str.split()))
    vb_x, vb_y = vb[0], vb[1]
    vb_w = vb[2] if len(vb) >= 3 else SVG_VB
    vb_h = vb[3] if len(vb) >= 4 else SVG_VB

    all_pts = []
    for elem in root.iter(tag('path')):
        d = elem.get('d', '').strip()
        if not d:
            continue
        pts = parse_svg_path(d)
        # 套用 viewBox offset
        if vb_x or vb_y:
            pts = [(x - vb_x, y - vb_y, pu) for x, y, pu in pts]
        all_pts.extend(pts)

    if not all_pts:
        return []

    grid = svg_to_symgrid(all_pts, vb_w, vb_h)
    quant = quantize_and_dedupe(grid)
    return encode_strokes(quant)


# ─── 主函式：組建 SYMBOL.COD ──────────────────────────────────────────────────

def build_symbol_cod(kanji_dir=KANJI_DIR, output=OUTPUT, verbose=True):
    """
    組建完整的 SYMBOL.COD 二進位檔。

    格式：
      bytes   0..511  : Lookup Table (256 × 2 bytes, big-endian IADSYM)
      bytes 512..     : Character Data Blocks
        each block: [count:1B] [pen₁:1B][data₁:1B] ...
    """
    lookup    = bytearray(512)   # 查找表
    char_data = bytearray()      # 字元資料

    found = missing = 0

    for char_code in range(256):
        # 目前字元資料起始位置（0-based）
        iadsym = 512 + len(char_data)

        # 寫入查找表
        lookup[char_code * 2]     = (iadsym >> 8) & 0xFF
        lookup[char_code * 2 + 1] =  iadsym       & 0xFF

        strokes = []

        # 只處理可列印 ASCII（0x20–0x7E），對應 Unicode 相同碼位
        if 0x20 <= char_code <= 0x7E:
            unicode_cp = char_code          # ASCII ≡ Unicode U+0020..U+007E
            svg_name   = f'{unicode_cp:05x}.svg'
            svg_path   = os.path.join(kanji_dir, svg_name)

            if os.path.isfile(svg_path):
                strokes = process_svg(svg_path)
                found  += 1
                if verbose:
                    ch = chr(unicode_cp)
                    print(f"  U+{unicode_cp:04X} {repr(ch):4s}  {svg_name}  → {len(strokes)} strokes")
            else:
                missing += 1
                if verbose:
                    ch = chr(unicode_cp)
                    print(f"  U+{unicode_cp:04X} {repr(ch):4s}  {svg_name}  [NOT FOUND]")

        # 寫入字元資料塊: [count][pen₁][data₁]...
        n = min(len(strokes), 127)   # 最多 127 個筆畫（1 byte 可容納）
        char_data.append(n)
        for pen_b, dat_b in strokes[:n]:
            char_data.append(pen_b)
            char_data.append(dat_b)

    # 合併輸出
    result = bytes(lookup) + bytes(char_data)
    with open(output, 'wb') as f:
        f.write(result)

    size = len(result)
    print()
    print("=" * 55)
    print(f"  輸出檔案  : {output}")
    print(f"  總大小    : {size} bytes  (原始 NKANF={ORIGINAL_NKANF})")
    print(f"  SVG 找到  : {found} / 95 個可列印 ASCII 字元")
    print(f"  SVG 缺少  : {missing}")
    print(f"  BASIC 修改: 將 NKANF = {ORIGINAL_NKANF} 改為 NKANF = {size}")
    print("=" * 55)

    return size


# ─── Entry point ─────────────────────────────────────────────────────────────

if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='從 KanjiVG SVG 字體產生 SYMBOL.COD'
    )
    parser.add_argument(
        '--kanji-dir', default=KANJI_DIR,
        help=f'KanjiVG SVG 資料夾（預設: {KANJI_DIR}）'
    )
    parser.add_argument(
        '--output', default=OUTPUT,
        help=f'輸出檔名（預設: {OUTPUT}）'
    )
    parser.add_argument(
        '--quiet', action='store_true',
        help='不顯示每個字元的處理資訊'
    )
    args = parser.parse_args()

    print(f"KanjiVG SVG 目錄 : {args.kanji_dir}")
    print(f"輸出檔案         : {args.output}")
    print("-" * 55)

    build_symbol_cod(
        kanji_dir=args.kanji_dir,
        output=args.output,
        verbose=not args.quiet
    )