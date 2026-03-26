#!/usr/bin/env python3
"""
shx2kandat.py — AutoCAD SHX BigFont → KANDAT.DAT + KANDAT2.DAT
================================================================
修正版 v6：子形狀 (CALL 0x07) 實作真正的不等比例縮放
  ─ render_shape 新增獨立的 scx / scy 參數
  ─ 所有向量位移的 dx 乘 scx、dy 乘 scy（含 0x08/0x0C/0x0D/0x10-0xFF 等）
  ─ CALL 子形狀傳入 child_scx = sc_x*(w/adv)、child_scy = sc_y*(h/total_y)
  ─ CALL 位移 dx/dy 亦分別乘 scx/scy（非單一 sc）
  ─ 弧線系列 (0x0A/0x0B) 以 (scx+scy)/2 保持圓弧形狀合理近似

  ╔══════════════════════════════════════════════════════════════╗
  ║  BUG 1：CALL 操作碼 (0x07) 的形狀編號讀取方式錯誤 [v2修正]   ║
  ║  BUG 2：操作碼 0x00 被當作 END [v2修正]                      ║
  ║  BUG 3：CALL 後父形狀的 x,y 位置被累加 [v3修正]              ║
  ║  BUG 4：操作碼 0x09 被解析為 XY_SEQ [v3修正]                 ║
  ║  BUG 5：操作碼 0x0E 被當作 NOP 或座標重置 [v5修正]           ║
  ║  BUG 6：子形狀 x/y 縮放強制等比，忽略 w/h [v6修正]           ║
  ╚══════════════════════════════════════════════════════════════╝
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

# 16 方向向量（單位長度，dx/dy 分別乘 scx/scy）
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
    """AutoCAD SHX BigFont 解析器。"""
    def __init__(self, path: str):
        self.path    = path
        self.shapes  : dict[int, tuple[int,int]] = {}
        self.raw     : bytes = b''
        self.above   = 21
        self.below   = 7
        self.advance = 28
        self.lead_ranges: list[tuple[int,int]] = []
        self._parse()

    def is_lead_byte(self, b: int) -> bool:
        return any(lo <= b <= hi for lo, hi in self.lead_ranges)

    def get_opdata(self, shapeno: int) -> bytes:
        entry = self.shapes.get(shapeno)
        if entry is None: return b''
        abs_off, defbytes = entry
        raw_block = self.raw[abs_off : abs_off + defbytes]
        null_pos = raw_block.find(0)
        if null_pos >= 0:
            return raw_block[null_pos+1:]
        return raw_block

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
        pos += 2

        if pos + 2 > size: return
        nshapes = struct.unpack_from('<H', raw, pos)[0]; pos += 2

        if pos >= size: return
        nranges = raw[pos]; pos += 1

        self.lead_ranges = []
        for i in range(nranges):
            if pos + 4 > size: break
            lead_start = raw[pos + 1]
            lead_end   = raw[pos + 3]
            self.lead_ranges.append((lead_start, lead_end))
            pos += 4

        if pos < size and raw[pos] == 0x00:
            pos += 1

        for _ in range(nshapes):
            if pos + 8 > size: break
            sno  = struct.unpack_from('<H', raw, pos)[0]
            db   = struct.unpack_from('<H', raw, pos+2)[0]
            foff = struct.unpack_from('<I', raw, pos+4)[0]
            pos += 8
            if db == 0: continue
            if foff + db > size: continue
            self.shapes[sno] = (foff, db)

        entry0 = self.shapes.get(0)
        if entry0:
            abs0, db0 = entry0
            data0 = raw[abs0 : abs0+db0]
            null_pos = data0.find(0)
            if null_pos >= 0 and null_pos+2 < len(data0):
                self.above = data0[null_pos+1]
                self.below = data0[null_pos+2]
        self.advance = (self.above + self.below) or 28

# ══════════════════════════════════════════════════════════════
# 弧線 / 圓形近似工具
# ══════════════════════════════════════════════════════════════
def _arc_pts(cx: float, cy: float, r: float,
             a0: float, span: float, seg: int = 4):
    n = max(abs(round(span / (math.pi/4))) * seg, 1)
    return [
        (cx + r*math.cos(a0 + k/n*span),
         cy + r*math.sin(a0 + k/n*span))
        for k in range(1, n+1)
    ]

def _bulge_arc(x0, y0, x1, y1, bulge):
    if abs(bulge) < 1e-9:
        return [(x1, y1)]
    half_chord = math.hypot(x1-x0, y1-y0) / 2
    if half_chord < 1e-9:
        return [(x1, y1)]
    r = half_chord * (1 + bulge*bulge) / (2 * abs(bulge))
    mx, my = (x0+x1)/2, (y0+y1)/2
    dx, dy = x1-x0, y1-y0
    sign = 1 if bulge > 0 else -1
    d = math.sqrt(max(r*r - half_chord*half_chord, 0))
    hc = math.hypot(dx, dy)
    if hc < 1e-9:
        return [(x1, y1)]
    cx = mx - sign * dy/hc * d
    cy = my + sign * dx/hc * d
    a0 = math.atan2(y0-cy, x0-cx)
    a1 = math.atan2(y1-cy, x1-cx)
    span = a1 - a0
    if bulge > 0 and span < 0: span += 2*math.pi
    if bulge < 0 and span > 0: span -= 2*math.pi
    return _arc_pts(cx, cy, r, a0, span)

# ══════════════════════════════════════════════════════════════
# 0x0E helper：跳過緊接的下一條完整指令（含所有參數 bytes）
# ══════════════════════════════════════════════════════════════
def _skip_one_instruction(opdata: bytes, i: int) -> int:
    """BUG 5 修正：0x0E = 使緊接的下一條指令完全無效。"""
    n = len(opdata)
    if i >= n:
        return i
    nop = opdata[i]
    if nop == 0x08:
        return i + 3
    elif nop == 0x07:
        return i + 1
    elif nop == 0x09:
        return i + 2
    elif nop == 0x0A:
        return i + 3
    elif nop == 0x0B:
        return i + 6
    elif nop == 0x0C:
        return i + 4
    elif nop == 0x0D:
        k = i + 1
        while k + 2 <= n:
            dx2 = opdata[k]; dy2 = opdata[k+1]; k += 2
            if dx2 == 0 and dy2 == 0:
                if k < n: k += 1
                break
            if k < n: k += 1
        return k
    elif nop == 0x0E:
        return i + 1
    else:
        return i + 1

# ══════════════════════════════════════════════════════════════
# SHX 形狀渲染器（v6 — 不等比縮放整合版）
# ══════════════════════════════════════════════════════════════
def render_shape(
    shapeno  : int,
    all_fonts: list[ShxFont],
    scx      : float = 1.0,   # X 軸縮放（取代舊版單一 sc）
    scy      : float | None = None,  # Y 軸縮放；None 表示與 scx 相同（等比）
    depth    : int   = 0,
) -> list[tuple[float, float, bool]]:
    """執行 SHX BigFont 操作碼，傳回 (x,y,pen_up) 點列。

    v6 主要改動：
    ・scx / scy 分離：所有向量位移的 dx 乘 scx、dy 乘 scy
    ・CALL (0x07) 子形狀傳入 child_scx/child_scy 獨立計算
    ・CALL 位移 dx/dy 亦分別乘 scx/scy
    ・弧線系列 (0x0A/0x0B) 以 sc_avg=(scx+scy)/2 維持圓弧合理性
    ・0x03/0x04（sc 縮放操作碼）同步等比調整 scx 與 scy
    ・stack push/pop 同時儲存/還原 scx/scy
    """
    if scy is None:
        scy = scx          # 預設等比

    if depth > 8: return []

    opdata = b''
    font_used = None
    for font in all_fonts:
        d = font.get_opdata(shapeno)
        if d:
            opdata = d
            font_used = font
            break
    if not opdata or font_used is None:
        return []

    # 從 fonts[0]（big_font）取度量，供 CALL 子形狀非等比縮放使用
    big_font = all_fonts[0]
    adv     = big_font.advance
    total_y = big_font.above + big_font.below

    pts    : list[tuple[float,float,bool]] = []
    stack  : list = []
    x = y  = 0.0
    draw_on = False
    pen_up  = True

    # 弧線用平均縮放（保持圓弧形狀合理）
    def _sc_avg() -> float:
        return (scx + scy) / 2.0

    def _emit_move(nx, ny):
        nonlocal x, y, pen_up
        x, y = nx, ny; pen_up = True

    def _emit_draw(nx, ny):
        nonlocal x, y, pen_up
        if pen_up:
            pts.append((x, y, True)); pen_up = False
        pts.append((nx, ny, False))
        x, y = nx, ny

    def _step(nx, ny):
        if draw_on: _emit_draw(nx, ny)
        else:       _emit_move(nx, ny)

    i = 0
    n = len(opdata)

    while i < n:
        op = opdata[i]; i += 1

        if op == 0x00:
            continue   # NOP（BUG 2）

        elif op == 0x01:
            draw_on = True

        elif op == 0x02:
            draw_on = False; pen_up = True

        elif op == 0x03:
            # 除以常數：同步縮小 scx / scy
            if i >= n: break
            v = opdata[i]; i += 1
            if v:
                scx /= v
                scy /= v

        elif op == 0x04:
            # 乘以常數：同步放大 scx / scy
            if i >= n: break
            v = opdata[i]; i += 1
            scx *= v
            scy *= v

        elif op == 0x05:
            stack.append((x, y, scx, scy, draw_on, pen_up))

        elif op == 0x06:
            if stack:
                x, y, scx, scy, draw_on, pen_up = stack.pop()

        elif op == 0x07:
            # ── CALL 子形狀（BUG 1 + BUG 3 + v6 不等比縮放）──
            sno_called = None
            while i < n:
                b = opdata[i]; i += 1
                if b == 0x00:
                    continue
                elif font_used.is_lead_byte(b):
                    if i < n:
                        b2 = opdata[i]; i += 1
                        sno_called = (b << 8) | b2
                    break
                else:
                    sno_called = b
                    break

            # 讀 dx, dy（位移）, w, h（縮放尺寸）
            if i + 4 <= n:
                dx = struct.unpack_from('b', opdata, i)[0]
                dy = struct.unpack_from('b', opdata, i+1)[0]
                w  = opdata[i+2]
                h  = opdata[i+3]
                i += 4
            else:
                dx = dy = 0
                w = h = 0

            if sno_called is not None and sno_called != 0 and sno_called != shapeno:
                # ── v6 核心：依 w/h 分別計算 child_scx / child_scy ──
                child_scx = scx * (w / adv)     if w else scx
                child_scy = scy * (h / total_y) if h else scy

                sub_pts = render_shape(
                    sno_called, all_fonts,
                    scx=child_scx, scy=child_scy,
                    depth=depth + 1,
                )

                # 位移在父層座標空間：dx 乘 scx，dy 乘 scy
                ox = x + dx * scx
                oy = y + dy * scy
                for sx, sy, spu in sub_pts:
                    pts.append((ox + sx, oy + sy, spu))

                # BUG 3：父形狀 x,y 保持不變
                pen_up = True

        elif op == 0x08:
            # 相對位移：dx 乘 scx，dy 乘 scy
            if i + 2 > n: break
            dx = struct.unpack_from('b', opdata, i)[0]; i += 1
            dy = struct.unpack_from('b', opdata, i)[0]; i += 1
            _step(x + dx*scx, y + dy*scy)

        elif op == 0x09:
            # 保留分支防錯位（BUG 4）
            i = min(i + 1, n)

        elif op == 0x0A:
            # 圓形：以 sc_avg 縮放半徑
            if i + 2 > n: break
            r    = opdata[i] * _sc_avg(); i += 1
            dirb = opdata[i];             i += 1
            a0_oct = (dirb >> 4) & 0x07
            a0 = a0_oct * math.pi / 4
            cx_ = x - r * math.cos(a0)
            cy_ = y - r * math.sin(a0)
            for px, py in _arc_pts(cx_, cy_, r, a0, 2*math.pi):
                _step(px, py)

        elif op == 0x0B:
            # 弧線：以 sc_avg 縮放半徑
            if i + 5 > n: break
            _b1 = struct.unpack_from('b', opdata, i)[0]; i += 1
            _b2 = struct.unpack_from('b', opdata, i)[0]; i += 1
            _b3 = struct.unpack_from('b', opdata, i)[0]; i += 1
            r   = opdata[i] * _sc_avg();                 i += 1
            b5  = opdata[i];                             i += 1
            a0_oct = (b5 >> 4) & 0x07
            n_oct  =  b5       & 0x0F
            if n_oct == 0: n_oct = 8
            ccw = not bool(b5 & 0x80)
            if r < 0.01: continue
            a0  = a0_oct * math.pi / 4
            cx_ = x - r * math.cos(a0)
            cy_ = y - r * math.sin(a0)
            span = n_oct * math.pi / 4 * (1 if ccw else -1)
            for px, py in _arc_pts(cx_, cy_, r, a0, span):
                _step(px, py)

        elif op == 0x0C:
            # Bulge arc（單段）：dx 乘 scx，dy 乘 scy
            if i + 3 > n: break
            dx    = struct.unpack_from('b', opdata, i)[0]; i += 1
            dy    = struct.unpack_from('b', opdata, i)[0]; i += 1
            bulge = struct.unpack_from('b', opdata, i)[0]; i += 1
            x1 = x + dx*scx; y1 = y + dy*scy
            b  = bulge / 127.0
            for px, py in _bulge_arc(x, y, x1, y1, b):
                _step(px, py)

        elif op == 0x0D:
            # Bulge arc（多段）：dx 乘 scx，dy 乘 scy
            while i + 2 <= n:
                dx = struct.unpack_from('b', opdata, i)[0]; i += 1
                dy = struct.unpack_from('b', opdata, i)[0]; i += 1
                if dx == 0 and dy == 0:
                    if i < n: i += 1
                    break
                if i >= n: break
                bulge = struct.unpack_from('b', opdata, i)[0]; i += 1
                x1 = x + dx*scx; y1 = y + dy*scy
                b  = bulge / 127.0
                for px, py in _bulge_arc(x, y, x1, y1, b):
                    _step(px, py)

        elif op == 0x0E:
            # 使緊接的下一條完整指令無效（BUG 5 修正）
            i = _skip_one_instruction(opdata, i)

        elif op == 0x0F:
            pass  # BigFont 保留（NOP）

        elif op >= 0x10:
            # 向量位元組：L×方向，dx 乘 scx，dy 乘 scy
            L = (op >> 4) & 0x0F
            N =  op       & 0x0F
            dx_u, dy_u = _DIR16[N]
            _step(x + dx_u*L*scx, y + dy_u*L*scy)

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
    """渲染點列 → KANDAT IP 值列。"""
    if not pts or adv <= 0: return []
    total_y = (above + below) or adv

    drawn_only = [(x, y) for x, y, pu in pts if not pu]
    if not drawn_only: return []

    xs = [p[0] for p in drawn_only]
    ys = [p[1] for p in drawn_only]
    x_max = max(xs); y_min = min(ys); y_max = max(ys)

    in_range = (x_max <= adv * 1.1 and
                y_min >= -total_y * 0.1 and
                y_max <= total_y * 1.1)

    if in_range:
        def norm_pt(x: float, y: float) -> tuple[int, int]:
            xi = max(0, min(32, round(x / adv * GRID)))
            yi = max(0, min(32, round((y + below) / total_y * GRID)))
            return xi, yi
    else:
        x_min = min(xs)
        y_min2 = min(ys); y_max2 = max(ys)
        xr = (max(xs) - x_min) or float(adv)
        yr = (y_max2 - y_min2) or float(total_y)
        sf   = min(adv / xr, total_y / yr)
        xpad = (adv    - xr * sf) / 2
        ypad = (total_y - yr * sf) / 2 + below
        def norm_pt(x: float, y: float) -> tuple[int, int]:
            xi = max(0, min(32, round(((x - x_min) * sf + xpad) / adv    * GRID)))
            yi = max(0, min(32, round(((y - y_min2) * sf + ypad) / total_y * GRID)))
            return xi, yi

    grid: list[tuple[int,int,bool]] = []
    prev_key = None
    for x, y, pu in pts:
        xi, yi = norm_pt(x, y)
        if pu:
            grid.append((xi, yi, True)); prev_key = None
        else:
            key = (xi, yi)
            if key != prev_key:
                grid.append((xi, yi, False)); prev_key = key

    clean: list[tuple[int,int,bool]] = []
    ng = len(grid)
    for k in range(ng):
        xi, yi, pu = grid[k]
        if pu:
            if k + 1 < ng and not grid[k+1][2]:
                clean.append((xi, yi, True))
        else:
            clean.append((xi, yi, False))

    if not clean: return []

    MAX_IPS = MAX_RECS * DATA_INTS - 1
    if len(clean) > MAX_IPS:
        step  = len(clean) / MAX_IPS
        clean = [clean[round(j*step)] for j in range(MAX_IPS)]

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
    print(f'  Lead byte 範圍：{[(hex(a),hex(b)) for a,b in big_font.lead_ranges]}')
    print(f'{"═"*60}')

    ip_by_slot: dict[int,list[int]] = {}
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
        # v6：頂層呼叫以等比縮放（scx=scy=1.0）啟動
        raw_pts = render_shape(shapeno, all_fonts, scx=1.0, scy=1.0)
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

    flat      : dict[int,list[int]] = {}
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
            for s,r in zip(slots, records):
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
        description='AutoCAD SHX BigFont → KANDAT.DAT + KANDAT2.DAT (v6 不等比縮放版)')
    parser.add_argument('--font-dir', default=FONT_DIR)
    parser.add_argument('--base-shx', default=BASE_SHX)
    parser.add_argument('--big-shx',  default=BIG_SHX)
    parser.add_argument('--out1', default=OUTPUT1)
    parser.add_argument('--out2', default=OUTPUT2)
    parser.add_argument('--verbose', '-v', action='store_true')
    parser.add_argument('--test-shx', default=None,
                        help='用測試 SHX 驗證解析（顯示幾個字的渲染結果）')
    args = parser.parse_args()

    if args.test_shx:
        print(f'測試解析：{args.test_shx}')
        font = ShxFont(args.test_shx)
        print(f'  above={font.above} below={font.below} advance={font.advance}')
        print(f'  lead_ranges={[(hex(a),hex(b)) for a,b in font.lead_ranges]}')
        print(f'  形狀數：{len(font.shapes)}')
        for sno in sorted(font.shapes.keys())[:10]:
            opdata = font.get_opdata(sno)
            pts    = render_shape(sno, [font], scx=1.0, scy=1.0)
            print(f'  sno=0x{sno:04X}  opdata={len(opdata)}B  渲染點={len(pts)}')
        return

    base_path = os.path.join(args.font_dir, args.base_shx)
    big_path  = os.path.join(args.font_dir, args.big_shx)

    print('AutoCAD SHX BigFont → KANDAT 產生器 v6（不等比縮放版）')
    for path in (base_path, big_path):
        if not os.path.isfile(path):
            print(f'\n[錯誤] 找不到：{path}'); return

    print('\n載入字型...')
    base_font = ShxFont(base_path)
    big_font  = ShxFont(big_path)
    print(f'  {args.base_shx}: {len(base_font.shapes):,} 形狀  '
          f'lead={[(hex(a),hex(b)) for a,b in base_font.lead_ranges]}')
    print(f'  {args.big_shx}:  {len(big_font.shapes):,} 形狀  '
          f'above={big_font.above} below={big_font.below}  '
          f'lead={[(hex(a),hex(b)) for a,b in big_font.lead_ranges]}')

    all_fonts = [big_font, base_font]

    print('\n掃描 Shift-JIS 編碼空間...')
    map1, map2 = build_xycode_maps()
    print(f'  KANDAT.DAT  對照：{len(map1)} 筆')
    print(f'  KANDAT2.DAT 對照：{len(map2)} 筆')

    print('\n[驗證] 測試字符渲染...')
    test_chars = [
        ('見', 0x8ca9), ('市', 0x8e73), ('缶', 0x8aca),
        ('軸', 0x8eb2), ('計', 0x8c76), ('川', 0x90ec),
        ('橋', 0x8bb4), ('線', 0x90fc), ('間', 0x8ad4),
        ('図', 0x907d), ('算', 0x8e5a), ('区', 0x8be6),
        ('り', 0x82E8), ('ょ', 0x8368), ('う', 0x82A4),
    ]
    for ch, sno in test_chars:
        pts = render_shape(sno, all_fonts, scx=1.0, scy=1.0)
        drawn = len([p for p in pts if not p[2]])
        print(f'  {ch} (0x{sno:04X}): {len(pts)} 點, {drawn} 繪製點')
    print()

    dat1 = build_kandat_bytes(map1, all_fonts, big_font, MAX_SLOT1, args.verbose, 'KANDAT.DAT')
    with open(args.out1,'wb') as f: f.write(dat1)
    print(f'\n→ {args.out1} 完成')

    dat2 = build_kandat_bytes(map2, all_fonts, big_font, MAX_SLOT2, args.verbose, 'KANDAT2.DAT')
    with open(args.out2,'wb') as f: f.write(dat2)
    print(f'→ {args.out2} 完成')

if __name__ == '__main__':
    main()