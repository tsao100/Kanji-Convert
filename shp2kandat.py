#!/usr/bin/env python3
"""
make_kandat_shp.py — AutoCAD SHP (dumpshx text) BigFont → KANDAT.DAT + KANDAT2.DAT
====================================================================================
從 dumpshx.exe 輸出的 .shp 文字檔讀取，產生 KANDAT.DAT / KANDAT2.DAT。

  ╔══════════════════════════════════════════════════════════════════╗
  ║  dumpshx SHP 文字格式（實際分析確認）                              ║
  ║                                                                  ║
  ║  ★ 修正 1：_parse_shp_value 的 0-前綴識別規則                      ║
  ║    SHP 格式中，任何以 "0" 開頭且長度 > 1 的 token = 十六進位        ║
  ║    例：090 = 0x90=144，08141 = 0x8141，0FE29 = 0xFE29             ║
  ║    例外：單一 "0" = 0（END marker）                                ║
  ║    ← 舊版只認 "含 A-F 字母才是 hex"，導致 090→90（錯）             ║
  ║                                                                  ║
  ║  ★ 修正 2：形狀編號解析的 regex 保留前導 0                          ║
  ║    舊版 r'^\*0?([...]+)' 會吃掉前導 0                              ║
  ║    → "08141" 被截成 "8141"，再被解析為 decimal 8141 ≠ 0x8141      ║
  ║    新版 r'^([0-9A-Fa-f]+)' 保留完整字串                          ║
  ║    → "08141" 以 0-前綴規則 → hex 0x8141 = 33089 ✓                ║
  ║                                                                  ║
  ║  ★ 修正 3：CALL 操作碼 7 的參數格式（dumpshx 特有）                  ║
  ║    格式：7,(0,shapeno16,off_x,off_y,adv_x,adv_y) = 固定 6 個值    ║
  ║    tokenize 後緊接在 7 後面的 6 個整數                              ║
  ║    ← 舊版只讀 2 個（b1+b2），剩下 4 個值當 opcode 執行              ║
  ║    off_x=0 被當作 opcode 0（END）→ 形狀立即終止！                   ║
  ║                                                                  ║
  ║  ★ 已知限制：dumpshx 不輸出 0xFF80-0xFFFE 的內部筆劃子形狀          ║
  ║    約 30% 的漢字參照這些缺失的子形狀 → 這些字會空白                  ║
  ║    解決方案：改用原始 .shx 二進位檔（make_kandat_shx.py）            ║
  ╚══════════════════════════════════════════════════════════════════╝

  *BIGFONT 標頭行格式（dumpshx 特有）：
    *BIGFONT nshapes,nranges,lo1,hi1[,lo2,hi2,...]
    注意：這是特殊行，不是 *0 shape

  shape 0 = 字型度量：
    *0,defbytes,name
    above, below, modes, advance, 0

  形狀條目（ALL hex with leading 0）：
    *0XXXX,defbytes,name
    opcode,...,0

  CALL 操作碼格式：
    7,(0, shapeno16, off_x, off_y, adv_x, adv_y)
    括號展開後為 6 個連續整數
"""
from __future__ import annotations
import os, struct, re, argparse, math

# ══════════════════════════════════════════════════════════════
# 常數
# ══════════════════════════════════════════════════════════════
FONT_DIR  = r'.'
BASE_SHP  = 'extfont.shp'
BIG_SHP   = 'extfont2.shp'
OUTPUT1   = 'KANDAT.DAT'
OUTPUT2   = 'KANDAT2.DAT'

REC_SIZE  = 32
INTS_PER  = 16
DATA_INTS = 15
GRID      = 32.0
MAX_RECS  = 8
MAX_SLOT1 = 3693
MAX_SLOT2 = 3572

# 16 方向向量
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
# SHP token 解析工具
# ══════════════════════════════════════════════════════════════

def _parse_shp_value(tok: str) -> int:
    """SHP token → int

    ★ 核心規則（符合 dumpshx 格式）：
      1. '0x' 前綴                → hex
      2. 以 '0' 開頭且長度 > 1    → hex（SHP 慣例：leading 0 = hex）
         例：090=0x90, 08141=0x8141, 0FE29=0xFE29, 02E=0x2E
      3. 含 A-F 字母（非 0x 前綴）→ hex（以防萬一）
      4. 其餘（含負號）            → 十進位
         例：2, -7, 14, 127
    """
    tok = tok.strip()
    if not tok:
        raise ValueError('空 token')
    neg = tok.startswith('-')
    abs_tok = tok[1:] if neg else tok

    if abs_tok.lower().startswith('0x'):
        v = int(abs_tok, 16)
    elif len(abs_tok) > 1 and abs_tok[0] == '0':
        # 以 0 開頭且長度 > 1 → 永遠視為十六進位
        v = int(abs_tok, 16)
    elif any(c in 'abcdefABCDEF' for c in abs_tok):
        v = int(abs_tok, 16)
    else:
        v = int(abs_tok, 10)

    return -v if neg else v


def _tokenize_shp_data(data_str: str) -> list[int]:
    """SHP 形狀資料字串 → 整數列表（括號展開）

    '2,14,8,(-7,-15),7,(0,0FE29,0,0,14,14),0'
    → [2, 14, 8, -7, -15, 7, 0, 0xFE29, 0, 0, 14, 14, 0]
    
    注意：
    - 括號 (a,b,...) 內的值展開為個別整數，順序保持
    - 逗號分隔；分號後為注釋
    - 負號處理：(-7,-15) → [-7, -15]
    """
    tokens: list[int] = []
    s = data_str.strip()
    i = 0
    buf = ''

    def flush():
        nonlocal buf
        b = buf.strip()
        buf = ''
        if b:
            tokens.append(_parse_shp_value(b))

    while i < len(s):
        c = s[i]
        if c == '(':
            flush(); i += 1
            depth = 1; inner = ''
            while i < len(s) and depth > 0:
                if   s[i] == '(': depth += 1
                elif s[i] == ')': depth -= 1
                if depth > 0: inner += s[i]
                i += 1
            for part in inner.split(','):
                part = part.strip()
                if part:
                    tokens.append(_parse_shp_value(part))
        elif c == ',':
            flush(); i += 1
        elif c == ';':
            break
        else:
            buf += c; i += 1
    flush()
    return tokens


# ══════════════════════════════════════════════════════════════
# SHP 文字檔解析器
# ══════════════════════════════════════════════════════════════

class ShpFont:
    """AutoCAD dumpshx SHP 文字格式 BigFont 解析器。

    ★ regex 修正：使用 r'^\*([0-9A-Fa-f]+)' 保留完整形狀號字串
      (舊版 regex 會吃掉前導 0，導致 "08141" 變 "8141" → 解析錯誤)
    """

    def __init__(self, path: str):
        self.path        = path
        self.shapes      : dict[int, list[int]] = {}
        self.above       = 15
        self.below       = 0
        self.advance     = 14
        self.lead_ranges : list[tuple[int,int]] = []
        self._parse()

    def is_lead_byte(self, b: int) -> bool:
        return any(lo <= b <= hi for lo, hi in self.lead_ranges)

    def get_opdata(self, shapeno: int) -> list[int]:
        return self.shapes.get(shapeno, [])

    def _parse(self):
        try:
            with open(self.path, encoding='latin-1') as f:
                content = f.read()
        except Exception as e:
            print(f'  [錯誤] 無法讀取 {self.path}: {e}')
            return

        lines = content.splitlines()

        # ── 1. *BIGFONT 標頭行 → 前導位元組範圍 ─────────────
        m = re.search(
            r'^\*BIGFONT\s+(\d+)\s*,\s*(\d+)\s*,(.+)$',
            content, re.MULTILINE | re.IGNORECASE)
        if m:
            nranges  = int(m.group(2))
            rest_str = m.group(3).strip()
            raw: list[int] = []
            for tok in rest_str.split(','):
                tok = tok.strip()
                if tok:
                    try: raw.append(_parse_shp_value(tok))
                    except ValueError: pass
            self.lead_ranges = []
            for j in range(min(nranges, len(raw)//2)):
                self.lead_ranges.append((raw[j*2], raw[j*2+1]))

        # ── 2. 合併多行形狀條目 ───────────────────────────────
        blocks: list[tuple[str,str]] = []
        cur_hdr  = ''
        cur_data : list[str] = []

        for line in lines:
            sc = line.find(';')
            if sc >= 0: line = line[:sc]
            line = line.rstrip()
            if not line:
                if cur_hdr:
                    blocks.append((cur_hdr, ','.join(cur_data)))
                    cur_hdr = ''; cur_data = []
                continue
            if line.startswith('*'):
                if cur_hdr:
                    blocks.append((cur_hdr, ','.join(cur_data)))
                cur_hdr = line; cur_data = []
            else:
                if cur_hdr:
                    cur_data.append(line)

        if cur_hdr:
            blocks.append((cur_hdr, ','.join(cur_data)))

        # ── 3. 解析各形狀 ────────────────────────────────────
        for hdr, data_str in blocks:
            # 跳過 *BIGFONT 標頭行
            if re.match(r'^\*BIGFONT\b', hdr, re.IGNORECASE):
                continue

            # ★ 修正：不使用 0? 前綴吞掉，保留完整形狀號字串
            m2 = re.match(r'^\*([0-9A-Fa-f]+)\s*,\s*\d+\s*,', hdr)
            if not m2: continue

            sno_str = m2.group(1)
            try:
                shapeno = _parse_shp_value(sno_str)
            except ValueError:
                continue

            if not data_str.strip(): continue

            try:
                opcodes = _tokenize_shp_data(data_str)
            except Exception as e:
                print(f'  [警告] shape 0x{shapeno:04X} 解析失敗: {e}')
                continue

            if shapeno == 0:
                # 字型度量：above, below, modes, advance, 0
                if len(opcodes) >= 2:
                    self.above = opcodes[0]
                    self.below = opcodes[1]
                if len(opcodes) >= 4:
                    self.advance = opcodes[3]
                if self.advance == 0:
                    self.advance = self.above + self.below or 14
            else:
                self.shapes[shapeno] = opcodes

        if self.advance == 0:
            self.advance = self.above + self.below or 14


# ══════════════════════════════════════════════════════════════
# 弧線近似工具
# ══════════════════════════════════════════════════════════════
def _arc_pts(cx, cy, r, a0, span, seg=4):
    n = max(abs(round(span / (math.pi/4))) * seg, 1)
    return [(cx + r*math.cos(a0 + k/n*span),
             cy + r*math.sin(a0 + k/n*span)) for k in range(1, n+1)]

def _bulge_arc(x0, y0, x1, y1, bulge):
    if abs(bulge) < 1e-9: return [(x1, y1)]
    hc = math.hypot(x1-x0, y1-y0) / 2
    if hc < 1e-9: return [(x1, y1)]
    r  = hc * (1 + bulge*bulge) / (2 * abs(bulge))
    mx, my = (x0+x1)/2, (y0+y1)/2
    dx, dy = x1-x0, y1-y0
    sg = 1 if bulge > 0 else -1
    d  = math.sqrt(max(r*r - hc*hc, 0))
    ch = math.hypot(dx, dy)
    if ch < 1e-9: return [(x1, y1)]
    cx = mx - sg*dy/ch*d;  cy = my + sg*dx/ch*d
    a0 = math.atan2(y0-cy, x0-cx)
    a1 = math.atan2(y1-cy, x1-cx)
    sp = a1 - a0
    if bulge > 0 and sp < 0: sp += 2*math.pi
    if bulge < 0 and sp > 0: sp -= 2*math.pi
    return _arc_pts(cx, cy, r, a0, sp)


# ══════════════════════════════════════════════════════════════
# SHP 形狀渲染器
# ══════════════════════════════════════════════════════════════
def render_shape(
    shapeno  : int,
    all_fonts: list[ShpFont],
    sc       : float = 1.0,
    depth    : int   = 0,
) -> list[tuple[float, float, bool]]:
    """SHP 操作碼 → (x,y,pen_up) 點列

    ★ CALL (opcode 7) 修正：
      dumpshx 格式 7,(0,shapeno16,off_x,off_y,adv_x,adv_y)
      opcode=7 消耗，緊接 6 個整數全部消耗：
        b1=0（固定）, sno（16位元形狀號）,
        off_x, off_y（呼叫前位移）,
        adv_x, adv_y（子形狀尺寸，忽略）
    """
    if depth > 8: return []

    opdata: list[int] = []
    font_used = None
    for font in all_fonts:
        d = font.get_opdata(shapeno)
        if d:
            opdata = d; font_used = font; break
    if not opdata or font_used is None:
        return []

    pts  : list[tuple[float,float,bool]] = []
    stack: list = []
    x = y = 0.0
    draw_on = False
    pen_up  = True

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

        if op == 0:                            # END
            break

        elif op == 1:                          # PEN DOWN
            draw_on = True

        elif op == 2:                          # PEN UP
            draw_on = False; pen_up = True

        elif op == 3:                          # scale ÷ n
            if i >= n: break
            v = opdata[i]; i += 1
            if v: sc /= v

        elif op == 4:                          # scale × n
            if i >= n: break
            sc *= opdata[i]; i += 1

        elif op == 5:                          # PUSH
            stack.append((x, y, sc, draw_on, pen_up))

        elif op == 6:                          # POP
            if stack:
                x, y, sc, draw_on, pen_up = stack.pop()

        elif op == 7:
            # ★ CALL（dumpshx 固定 6 參數）
            if i + 6 > n: break
            _b1    = opdata[i]; i += 1   # 固定 0
            sno    = opdata[i]; i += 1   # 形狀號（16位元）
            off_x  = opdata[i]; i += 1   # 呼叫前 x 偏移
            off_y  = opdata[i]; i += 1   # 呼叫前 y 偏移
            _adv_x = opdata[i]; i += 1   # 子形狀寬（忽略）
            _adv_y = opdata[i]; i += 1   # 子形狀高（忽略）

            if sno == 0 or sno == shapeno: continue

            cx = x + off_x * sc
            cy = y + off_y * sc
            sub = render_shape(sno, all_fonts, sc, depth+1)
            for sx, sy, spu in sub:
                pts.append((cx+sx, cy+sy, spu))
            pen_up = True

        elif op == 8:                          # XY 位移
            if i + 2 > n: break
            dx = opdata[i]; i += 1
            dy = opdata[i]; i += 1
            _step(x + dx*sc, y + dy*sc)

        elif op == 9:                          # XY 序列，(0,0) 結束
            while i + 2 <= n:
                dx = opdata[i]; i += 1
                dy = opdata[i]; i += 1
                if dx == 0 and dy == 0: break
                _step(x + dx*sc, y + dy*sc)

        elif op == 0x0A:                       # 八分弧
            if i + 2 > n: break
            r    = opdata[i]*sc; i += 1
            dirb = opdata[i] & 0xFF; i += 1
            a0   = ((dirb>>4)&7) * math.pi/4
            cx_  = x - r*math.cos(a0); cy_ = y - r*math.sin(a0)
            for px, py in _arc_pts(cx_, cy_, r, a0, 2*math.pi): _step(px, py)

        elif op == 0x0B:                       # 分數弧（5 參數）
            if i + 5 > n: break
            _b1=opdata[i];i+=1; _b2=opdata[i];i+=1; _b3=opdata[i];i+=1
            r  = opdata[i]*sc; i += 1
            b5 = opdata[i]&0xFF; i += 1
            a0_oct=(b5>>4)&7; n_oct=b5&0xF
            if n_oct==0: n_oct=8
            ccw = not bool(b5&0x80)
            if r < 0.01: continue
            a0  = a0_oct*math.pi/4
            cx_ = x-r*math.cos(a0); cy_=y-r*math.sin(a0)
            span= n_oct*math.pi/4*(1 if ccw else -1)
            for px,py in _arc_pts(cx_,cy_,r,a0,span): _step(px,py)

        elif op == 0x0C:                       # 單段凸弧
            if i + 3 > n: break
            dx=opdata[i];i+=1; dy=opdata[i];i+=1; bl=opdata[i];i+=1
            x1=x+dx*sc; y1=y+dy*sc
            for px,py in _bulge_arc(x,y,x1,y1,bl/127.0): _step(px,py)

        elif op == 0x0D:                       # 多段凸弧
            while i + 2 <= n:
                dx=opdata[i];i+=1; dy=opdata[i];i+=1
                if dx==0 and dy==0:
                    if i<n: i+=1
                    break
                if i>=n: break
                bl=opdata[i];i+=1
                x1=x+dx*sc; y1=y+dy*sc
                for px,py in _bulge_arc(x,y,x1,y1,bl/127.0): _step(px,py)

        elif op == 0x0E:                       # skip-next（BigFont 字元寬度元資料）
            if i >= n: break
            nop   = opdata[i]; i += 1
            nop_u = nop & 0xFF if nop < 0 else nop
            if   nop_u == 7:  i += 6
            elif nop_u == 8:  i += 2
            elif nop_u == 9:
                while i+2<=n:
                    a,b=opdata[i],opdata[i+1]; i+=2
                    if a==0 and b==0: break
            elif nop_u == 0x0A: i += 2
            elif nop_u == 0x0B: i += 5
            elif nop_u == 0x0C: i += 3
            elif nop_u == 0x0D:
                while i+2<=n:
                    a,b=opdata[i],opdata[i+1]; i+=2
                    if a==0 and b==0:
                        if i<n: i+=1
                        break
                    if i<n: i+=1

        elif op == 0x0F:                       # 保留 NOP
            pass

        else:                                  # 向量位元組 0xLN
            op_u = op & 0xFF if op < 0 else op
            if op_u >= 0x10:
                L=(op_u>>4)&0xF; N=op_u&0xF
                if N < len(_DIR16):
                    dxu,dyu = _DIR16[N]
                    _step(x + dxu*L*sc, y + dyu*L*sc)

    return pts


# ══════════════════════════════════════════════════════════════
# 座標正規化 & IP 值編碼
# ══════════════════════════════════════════════════════════════
def normalize_and_encode(pts, above, below, adv):
    if not pts or adv <= 0: return []
    total_y = (above + below) or adv

    grid = []
    prev_key = None
    for x, y, pu in pts:
        xi = max(0, min(32, round(x / adv * GRID)))
        yi = max(0, min(32, round((y + below) / total_y * GRID)))
        if pu:
            grid.append((xi, yi, True)); prev_key = None
        else:
            key = (xi, yi)
            if key != prev_key:
                grid.append((xi, yi, False)); prev_key = key

    clean = []
    ng = len(grid)
    for k in range(ng):
        xi, yi, pu = grid[k]
        if pu:
            if k+1 < ng and not grid[k+1][2]:
                clean.append((xi, yi, True))
        else:
            clean.append((xi, yi, False))

    if not clean: return []
    MAX_IPS = MAX_RECS * DATA_INTS - 1
    if len(clean) > MAX_IPS:
        step  = len(clean) / MAX_IPS
        clean = [clean[round(i*step)] for i in range(MAX_IPS)]

    return [(2 if pu else 1)*10000 + xi*100 + yi for xi,yi,pu in clean]


# ══════════════════════════════════════════════════════════════
# 記錄封裝
# ══════════════════════════════════════════════════════════════
def pack_to_records(ip_values):
    max_pts = MAX_RECS * DATA_INTS - 1
    if len(ip_values) > max_pts: ip_values = ip_values[:max_pts]
    stream  = [len(ip_values)] + ip_values
    records = []
    for i in range(0, len(stream), DATA_INTS):
        chunk = stream[i:i+DATA_INTS]
        chunk += [0] * (DATA_INTS - len(chunk))
        records.append(chunk + [0])
    return records or [[0]*INTS_PER]


# ══════════════════════════════════════════════════════════════
# KANDAT 二進位生成
# ══════════════════════════════════════════════════════════════
def build_kandat_bytes(xycode_map, all_fonts, big_font, max_primary, verbose, label):
    print(f'\n{"═"*60}')
    print(f'  [{label}]  主要槽號上限：{max_primary}')
    print(f'  BigFont 度量：above={big_font.above}, below={big_font.below}, advance={big_font.advance}')
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

        sno = ((sjis[0]<<8)|sjis[1]) if len(sjis)==2 else sjis[0]
        pts = render_shape(sno, all_fonts)
        if pts:
            ips = normalize_and_encode(pts, big_font.above, big_font.below, big_font.advance)
            found += 1
            if verbose:
                try: ch = chr(ucp)
                except: ch = '?'
                print(f'  slot {slot:4d}  U+{ucp:04X} {ch}  SHX={sno:#06x}  {len(ips):3d}pt')
        else:
            ips = []; missing += 1

        ip_by_slot[slot] = ips

    flat: dict[int,list[int]] = {}
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
        description='AutoCAD dumpshx SHP BigFont → KANDAT.DAT + KANDAT2.DAT')
    parser.add_argument('--font-dir', default=FONT_DIR)
    parser.add_argument('--base-shp', default=BASE_SHP)
    parser.add_argument('--big-shp',  default=BIG_SHP)
    parser.add_argument('--out1', default=OUTPUT1)
    parser.add_argument('--out2', default=OUTPUT2)
    parser.add_argument('--verbose', '-v', action='store_true')
    parser.add_argument('--test-shp', default=None,
                        help='測試解析指定 SHP，顯示前幾個形狀')
    parser.add_argument('--test-chars', default=None,
                        help='測試指定字串渲染（例：見川軸計）')
    args = parser.parse_args()

    if args.test_shp:
        print(f'測試解析：{args.test_shp}')
        font = ShpFont(args.test_shp)
        print(f'  above={font.above} below={font.below} advance={font.advance}')
        print(f'  lead_ranges={[(hex(a),hex(b)) for a,b in font.lead_ranges]}')
        print(f'  形狀數：{len(font.shapes)}')
        all_f = [font]
        for sno in sorted(font.shapes.keys())[:10]:
            pts   = render_shape(sno, all_f)
            drawn = sum(1 for p in pts if not p[2])
            print(f'  0x{sno:04X}  opdata={len(font.get_opdata(sno)):3d}  pts={len(pts):3d}  drawn={drawn:3d}')
        if args.test_chars:
            print()
            for ch in args.test_chars:
                try:
                    sjis = ch.encode('cp932')
                    sno  = ((sjis[0]<<8)|sjis[1]) if len(sjis)==2 else sjis[0]
                    pts  = render_shape(sno, all_f)
                    drawn= sum(1 for p in pts if not p[2])
                    print(f'  {ch}  0x{sno:04X}  pts={len(pts)}  drawn={drawn}')
                except Exception as e:
                    print(f'  {ch}  錯誤：{e}')
        return

    base_path = os.path.join(args.font_dir, args.base_shp)
    big_path  = os.path.join(args.font_dir, args.big_shp)

    print('AutoCAD dumpshx SHP BigFont → KANDAT 產生器')
    for path in (base_path, big_path):
        if not os.path.isfile(path):
            print(f'\n[錯誤] 找不到：{path}'); return

    print('\n載入字型...')
    base_font = ShpFont(base_path)
    big_font  = ShpFont(big_path)
    print(f'  {args.base_shp}: {len(base_font.shapes):,} 形狀  '
          f'above={base_font.above} below={base_font.below}  '
          f'lead={[(hex(a),hex(b)) for a,b in base_font.lead_ranges]}')
    print(f'  {args.big_shp}:  {len(big_font.shapes):,} 形狀  '
          f'above={big_font.above} below={big_font.below}  '
          f'lead={[(hex(a),hex(b)) for a,b in big_font.lead_ranges]}')

    all_fonts = [big_font, base_font]

    print('\n掃描 Shift-JIS 編碼空間...')
    map1, map2 = build_xycode_maps()
    print(f'  KANDAT.DAT  對照：{len(map1)} 筆')
    print(f'  KANDAT2.DAT 對照：{len(map2)} 筆')

    print('\n[驗證] 測試字符渲染...')
    for ch in '見立川橋りょう軸力線図計算区間長、。？！':
        try:
            sjis  = ch.encode('cp932')
            sno   = ((sjis[0]<<8)|sjis[1]) if len(sjis)==2 else sjis[0]
            pts   = render_shape(sno, all_fonts)
            drawn = sum(1 for p in pts if not p[2])
            status = '✓' if drawn > 0 else '✗ 空白(缺子形狀)'
            print(f'  {ch} (0x{sno:04X}): {len(pts):3d}點 {drawn:3d}繪製  {status}')
        except Exception as e:
            print(f'  {ch}: 錯誤 {e}')
    print()

    dat1 = build_kandat_bytes(map1, all_fonts, big_font, MAX_SLOT1, args.verbose, 'KANDAT.DAT')
    with open(args.out1,'wb') as f: f.write(dat1)
    print(f'\n→ {args.out1} 完成')

    dat2 = build_kandat_bytes(map2, all_fonts, big_font, MAX_SLOT2, args.verbose, 'KANDAT2.DAT')
    with open(args.out2,'wb') as f: f.write(dat2)
    print(f'→ {args.out2} 完成')

if __name__ == '__main__':
    main()