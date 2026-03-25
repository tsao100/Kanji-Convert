#!/usr/bin/env python3
"""
kandat_viewer.py — KANDAT 字符可視化工具
=========================================
將 KANDAT.DAT / KANDAT2.DAT 中的漢字 IP 值讀出，
以 Pillow 渲染成圖像，並提供互動式查詢功能。

用法:
  python kandat_viewer.py                  # 渲染預設測試字符
  python kandat_viewer.py --char 算橋計間   # 指定字符
  python kandat_viewer.py --all            # 渲染所有字符到 grid 圖
  python kandat_viewer.py --grid 32        # 每行 32 字，生成 grid
  python kandat_viewer.py --shx1 extfont.shx --shx2 extfont2.shx
"""
from __future__ import annotations
import os, sys, struct, math, argparse
from PIL import Image, ImageDraw, ImageFont

# ──────────────────────────────────────────────────────
# SHX 字型讀取（與 make_kandat.py 相同邏輯）
# ──────────────────────────────────────────────────────
FONT_DIR = '.'
BASE_SHX = 'extfont.shx'
BIG_SHX  = 'extfont2.shx'

_DIR16 = [
    ( 1, 0),( 1,.5),( 1, 1),(.5, 1),
    ( 0, 1),(-.5, 1),(-1, 1),(-1,.5),
    (-1, 0),(-1,-.5),(-1,-1),(-.5,-1),
    ( 0,-1),( .5,-1),( 1,-1),( 1,-.5),
]

class ShxFont:
    def __init__(self, path: str):
        self.path = path
        with open(path, 'rb') as f:
            self.raw = f.read()
        self._parse()

    def _parse(self):
        raw = self.raw
        pos = raw.find(0x1A) + 1 + 2
        nshapes = struct.unpack_from('<H', raw, pos)[0]; pos += 2
        nranges = raw[pos]; pos += 1
        self.lead_ranges = []
        for i in range(nranges):
            b = raw[pos+i*4:pos+i*4+4]
            self.lead_ranges.append((b[1], b[3]))
        pos += nranges * 4
        if raw[pos] == 0x00: pos += 1
        self.shapes: dict[int, tuple] = {}
        for _ in range(nshapes):
            if pos + 8 > len(raw): break
            sno  = struct.unpack_from('<H', raw, pos)[0]
            db   = struct.unpack_from('<H', raw, pos+2)[0]
            foff = struct.unpack_from('<I', raw, pos+4)[0]
            pos += 8
            if db: self.shapes[sno] = (foff, db)
        self.above = self.below = self.advance = 15
        if 0 in self.shapes:
            foff, db = self.shapes[0]
            d = raw[foff:foff+db]; null = d.find(0)
            if null >= 0 and null + 2 < len(d):
                self.above = d[null+1]; self.below = d[null+2]

    def is_lead(self, b: int) -> bool:
        return any(lo <= b <= hi for lo, hi in self.lead_ranges)

    def get_opdata(self, sno: int) -> bytes:
        e = self.shapes.get(sno)
        if not e: return b''
        foff, db = e
        d = self.raw[foff:foff+db]; null = d.find(0)
        return d[null+1:] if null >= 0 else d


def _bulge_arc(x0, y0, x1, y1, b):
    if abs(b) < 1e-9: return [(x1, y1)]
    hc = math.hypot(x1-x0, y1-y0) / 2
    if hc < 1e-9: return [(x1, y1)]
    r  = hc * (1 + b**2) / (2*abs(b))
    mx, my = (x0+x1)/2, (y0+y1)/2
    dx, dy = x1-x0, y1-y0
    sign = 1 if b > 0 else -1
    d2 = math.sqrt(max(r*r - hc*hc, 0))
    h  = math.hypot(dx, dy)
    if h < 1e-9: return [(x1, y1)]
    cx = mx - sign*dy/h*d2; cy = my + sign*dx/h*d2
    a0 = math.atan2(y0-cy, x0-cx); a1 = math.atan2(y1-cy, x1-cx)
    sp = a1 - a0
    if b > 0 and sp < 0: sp += 2*math.pi
    if b < 0 and sp > 0: sp -= 2*math.pi
    np_ = max(abs(round(sp/(math.pi/4)))*4, 1)
    return [(cx+r*math.cos(a0+k/np_*sp), cy+r*math.sin(a0+k/np_*sp))
            for k in range(1, np_+1)]


def exec_shape(sno, fonts, ox=0.0, oy=0.0, sc=1.0, depth=0, seen=None):
    """SHX 指令流執行器 → 線段列表 [(x1,y1,x2,y2)]"""
    if depth > 8: return []
    if seen is None: seen = frozenset()
    if sno in seen: return []
    seen = seen | {sno}

    opdata = b''; font_used = None
    for fnt in fonts:
        d = fnt.get_opdata(sno)
        if d: opdata = d; font_used = fnt; break
    if not opdata: return []

    segs = []; px = py = 0.0; sc_cur = sc; draw = False; stk = []

    def step(nx, ny):
        nonlocal px, py
        if draw: segs.append((ox+px, oy+py, ox+nx, oy+ny))
        px, py = nx, ny

    i = 0; n = len(opdata)
    while i < n:
        op = opdata[i]; i += 1
        if   op == 0x00: continue
        elif op == 0x01: draw = True
        elif op == 0x02: draw = False
        elif op == 0x03:
            if i < n: v = opdata[i]; i += 1
            if v: sc_cur /= v
        elif op == 0x04:
            if i < n: v = opdata[i]; i += 1
            sc_cur *= v
        elif op == 0x05: stk.append((px, py, sc_cur, draw))
        elif op == 0x06:
            if stk: px, py, sc_cur, draw = stk.pop()
        elif op == 0x07:
            csno = None
            while i < n:
                c = opdata[i]; i += 1
                if c == 0x00: continue
                elif font_used.is_lead(c):
                    if i < n: c2 = opdata[i]; i += 1; csno = (c<<8)|c2
                    break
                else: csno = c; break
            if csno:
                segs.extend(exec_shape(csno, fonts,
                                       ox+px, oy+py, sc_cur, depth+1, seen))
        elif op == 0x08:
            if i+2 > n: break
            dx = struct.unpack_from('b', opdata, i)[0]; i += 1
            dy = struct.unpack_from('b', opdata, i)[0]; i += 1
            step(px+dx*sc_cur, py+dy*sc_cur)
        elif op == 0x09: i = min(i+1, n)
        elif op == 0x0A:
            # CIRCLE(r): 1 バイト半径のみ（dirb なし）
            if i+1 > n: break
            r = opdata[i]*sc_cur; i += 1
            cx = px; cy = py
            for k in range(1, 17):
                step(cx+r*math.cos(k/16*2*math.pi),
                     cy+r*math.sin(k/16*2*math.pi))
        elif op == 0x0B:
            if i+5 > n: break
            _ = opdata[i:i+3]; i += 3
            r  = opdata[i]*sc_cur; i += 1
            b5 = opdata[i];        i += 1
            a0_oct = (b5>>4)&7; n_oct = b5&0xf
            if n_oct == 0: n_oct = 8
            cw_flag = bool(b5 & 0x80)
            a0 = a0_oct*math.pi/4
            cx = px - r*math.cos(a0); cy = py - r*math.sin(a0)
            span = n_oct*math.pi/4*(-1 if cw_flag else 1)
            np_ = max(abs(round(span/(math.pi/4)))*4, 1)
            for k in range(1, np_+1):
                step(cx+r*math.cos(a0+k/np_*span),
                     cy+r*math.sin(a0+k/np_*span))
        elif op == 0x0C:
            if i+3 > n: break
            dx  = struct.unpack_from('b', opdata, i)[0]; i += 1
            dy  = struct.unpack_from('b', opdata, i)[0]; i += 1
            bul = struct.unpack_from('b', opdata, i)[0]; i += 1
            for apx, apy in _bulge_arc(px, py,
                                       px+dx*sc_cur, py+dy*sc_cur,
                                       bul/127.0):
                step(apx, apy)
        elif op == 0x0D:
            while i+2 <= n:
                dx = struct.unpack_from('b', opdata, i)[0]; i += 1
                dy = struct.unpack_from('b', opdata, i)[0]; i += 1
                if dx == 0 and dy == 0:
                    if i < n: i += 1; break
                if i >= n: break
                bul = struct.unpack_from('b', opdata, i)[0]; i += 1
                for apx, apy in _bulge_arc(px, py,
                                           px+dx*sc_cur, py+dy*sc_cur,
                                           bul/127.0):
                    step(apx, apy)
        elif op == 0x0E:
            if i < n:
                nop = opdata[i]
                if   nop == 0x08: i += 3
                elif nop == 0x09: i += 2
                elif nop == 0x0A: i += 2   # CIRCLE: op+r
                elif nop == 0x0B: i += 6
                elif nop == 0x0C: i += 4
                elif nop == 0x0D:
                    k = i+1
                    while k+2 <= n:
                        d1,d2 = opdata[k],opdata[k+1]; k += 2
                        if d1==0 and d2==0:
                            if k < n: k += 1; break
                        if k < n: k += 1
                    i = k
                else: i += 1
        elif op == 0x0F: pass
        elif op >= 0x10:
            L = (op>>4)&0xf; N = op&0xf
            dx2, dy2 = _DIR16[N]
            step(px+dx2*L*sc_cur, py+dy2*L*sc_cur)
    return segs


# ──────────────────────────────────────────────────────
# 字符 SHX code 的 Shift-JIS マッピング
# ──────────────────────────────────────────────────────
def char_to_sno(ch: str) -> int | None:
    try:
        b = ch.encode('cp932')
        if len(b) == 2: return (b[0]<<8)|b[1]
        elif len(b) == 1: return b[0]
    except: pass
    return None


def sno_to_char(sno: int) -> str:
    try:
        hi = (sno>>8)&0xFF; lo = sno&0xFF
        if hi: return bytes([hi,lo]).decode('cp932')
        else: return bytes([lo]).decode('cp932')
    except: return '?'


# ──────────────────────────────────────────────────────
# 字符描画エンジン
# ──────────────────────────────────────────────────────
def render_char_to_image(
    sno: int,
    fonts: list[ShxFont],
    cell_px: int = 128,
    bg_color  = (18, 18, 30),
    fg_color  = (100, 200, 255),
    grid_color = (35, 35, 55),
    dot_color  = (255, 120, 80),
    show_grid: bool = True,
    show_dots: bool = True,
    show_label: bool = True,
    label_font = None,
) -> Image.Image:
    """
    1字を cell_px × cell_px の PIL Image にレンダリング。
    座標系: SHX 設計空間 → [0,32]×[0,32] → ピクセル
    """
    font_big = fonts[0]
    above = font_big.above; below = font_big.below; adv = font_big.above
    total_y = (above + below) or adv

    segs = exec_shape(sno, fonts)

    # ── 正規化 ──────────────────────────────────────
    if segs:
        xs = [s[0] for s in segs] + [s[2] for s in segs]
        ys = [s[1] for s in segs] + [s[3] for s in segs]
        xmn, xmx = min(xs), max(xs)
        ymn, ymx = min(ys), max(ys)
        in_range = (xmx <= adv*1.1 and ymn >= -total_y*0.1 and ymx <= total_y*1.1)
        if in_range:
            def norm(x, y):
                xi = x / adv * 32
                yi = (y + below) / total_y * 32
                return xi, yi
        else:
            xr = xmx - xmn or float(adv)
            yr = ymx - ymn or float(total_y)
            def norm(x, y):
                xi = (x - xmn) / xr * 32
                yi = (y - ymn) / yr * 32
                return xi, yi
    else:
        def norm(x, y): return x/adv*32, (y+below)/total_y*32

    # ── 描画 ──────────────────────────────────────
    pad = max(4, cell_px // 16)
    draw_w = cell_px - 2*pad
    draw_h = cell_px - 2*pad

    img = Image.new('RGB', (cell_px, cell_px), bg_color)
    d = ImageDraw.Draw(img)

    # グリッド線
    if show_grid:
        for step in [8, 16, 24, 32]:
            gx = pad + step/32*draw_w
            gy = pad + step/32*draw_h
            d.line([(gx, pad), (gx, pad+draw_h)], fill=grid_color, width=1)
            d.line([(pad, gy), (pad+draw_w, gy)], fill=grid_color, width=1)
        # 外枠
        d.rectangle([pad, pad, pad+draw_w, pad+draw_h],
                    outline=grid_color, width=1)

    # 線段を描画
    dots = []
    for x1, y1, x2, y2 in segs:
        nx1, ny1 = norm(x1, y1)
        nx2, ny2 = norm(x2, y2)
        # スクリーン座標変換（y軸反転）
        px1 = pad + nx1/32*draw_w
        py1 = pad + (1 - ny1/32)*draw_h
        px2 = pad + nx2/32*draw_w
        py2 = pad + (1 - ny2/32)*draw_h
        # 線分の太さをセルサイズに比例
        lw = max(1, cell_px // 48)
        d.line([(px1, py1), (px2, py2)], fill=fg_color, width=lw)
        if show_dots:
            dots.append((px1, py1))
            dots.append((px2, py2))

    # 端点ドット
    if show_dots:
        dr = max(1, cell_px // 40)
        for px, py in dots:
            d.ellipse([px-dr, py-dr, px+dr, py+dr], fill=dot_color)

    # ラベル（字符と SHX コード）
    if show_label:
        ch = sno_to_char(sno)
        n_segs = len(segs)
        label = f"{ch}  0x{sno:04X}  {n_segs}segs"
        # テキストを左下に
        try:
            if label_font:
                d.text((pad, cell_px - pad - 12), label,
                       fill=(130, 140, 160), font=label_font)
            else:
                d.text((pad, cell_px - pad - 10), label,
                       fill=(130, 140, 160))
        except: pass

    return img


# ──────────────────────────────────────────────────────
# KANDAT DAT 読み込み（検証用）
# ──────────────────────────────────────────────────────
def read_kandat_ips(dat_path: str, slot: int) -> list[int]:
    """
    KANDAT.DAT から指定スロットの IP 値を読む。
    slot は JIS コードから計算した KANDAT スロット番号。
    """
    REC_SIZE = 32; INTS_PER = 16
    if not os.path.exists(dat_path): return []
    with open(dat_path, 'rb') as f:
        buf = f.read()

    def read_rec(s):
        off = (s + 7) * REC_SIZE
        if off + REC_SIZE > len(buf): return None
        return list(struct.unpack_from(f'<{INTS_PER}h', buf, off))

    rec = read_rec(slot)
    if not rec: return []

    # Primary record: [fmt_val, adv, ip0..ip5, 0 or cont_ptr]
    fmt_val = rec[0]; adv_val = rec[1]
    ip_data = rec[2:2+6]
    cont = rec[15]

    # Count field = ip_data[0]
    if not ip_data or ip_data[0] <= 0: return []
    n_ips = ip_data[0]
    all_ips = ip_data[1:]

    # Follow continuation records
    visited = {slot}
    while cont != 0 and cont not in visited:
        visited.add(cont)
        crec = read_rec(abs(cont))
        if not crec: break
        # cont record: [0, orig_slot, ip0..ip5, next_cont]
        cips = crec[2:2+6]
        all_ips.extend(cips)
        cont = crec[15]

    return [v for v in all_ips[:n_ips] if v != 0]


def render_ips_to_image(
    ips: list[int],
    cell_px: int = 128,
    bg_color  = (18, 18, 30),
    fg_color  = (80, 200, 120),
    grid_color = (35, 35, 55),
    dot_color  = (255, 180, 60),
    show_grid: bool = True,
    show_dots: bool = True,
    title: str = "",
    label_font = None,
) -> Image.Image:
    """KANDAT IP 値列からキャラクター画像を生成。"""
    pad = max(4, cell_px // 16)
    draw_w = cell_px - 2*pad
    draw_h = cell_px - 2*pad

    img = Image.new('RGB', (cell_px, cell_px), bg_color)
    d = ImageDraw.Draw(img)

    if show_grid:
        for step in [8, 16, 24, 32]:
            gx = pad + step/32*draw_w
            gy = pad + step/32*draw_h
            d.line([(gx, pad), (gx, pad+draw_h)], fill=grid_color, width=1)
            d.line([(pad, gy), (pad+draw_w, gy)], fill=grid_color, width=1)
        d.rectangle([pad, pad, pad+draw_w, pad+draw_h],
                    outline=grid_color, width=1)

    # IP 値 → 線段
    cur = None; lw = max(1, cell_px // 48)
    dots = []
    for ip in ips:
        t  = ip // 10000
        xi = (ip % 10000) // 100
        yi = ip % 100
        if not (0 <= xi <= 32 and 0 <= yi <= 32): continue
        px = pad + xi/32*draw_w
        py = pad + (1 - yi/32)*draw_h
        if t == 2:   # MOVE
            cur = (px, py)
        elif t == 1 and cur:  # DRAW
            d.line([cur, (px, py)], fill=fg_color, width=lw)
            if show_dots:
                dots.append(cur); dots.append((px, py))
            cur = (px, py)

    if show_dots:
        dr = max(1, cell_px // 40)
        for px, py in dots:
            d.ellipse([px-dr, py-dr, px+dr, py+dr], fill=dot_color)

    if title:
        try:
            if label_font:
                d.text((pad, cell_px-pad-12), title,
                       fill=(130,140,160), font=label_font)
            else:
                d.text((pad, cell_px-pad-10), title, fill=(130,140,160))
        except: pass

    return img


# ──────────────────────────────────────────────────────
# グリッド画像生成
# ──────────────────────────────────────────────────────
def make_char_grid(
    char_list: list[str],
    fonts: list[ShxFont],
    cell_px: int = 96,
    cols: int = 16,
    title: str = "KANDAT v7 字符可視化",
    show_grid: bool = True,
    show_dots: bool = True,
) -> Image.Image:
    """複数字符を格子状に並べたグリッド画像を生成。"""
    valid = [(ch, char_to_sno(ch)) for ch in char_list
             if char_to_sno(ch) is not None]

    rows = math.ceil(len(valid) / cols)
    header_h = 40
    margin = 8
    W = cols * cell_px + 2*margin
    H = rows * cell_px + header_h + 2*margin

    bg = (12, 12, 20)
    img = Image.new('RGB', (W, H), bg)
    d = ImageDraw.Draw(img)

    # タイトル
    try:
        d.text((margin, 10), title, fill=(160, 180, 220))
    except: pass

    for idx, (ch, sno) in enumerate(valid):
        row = idx // cols; col = idx % cols
        x = margin + col * cell_px
        y = header_h + margin + row * cell_px

        cell = render_char_to_image(
            sno, fonts, cell_px=cell_px,
            show_grid=show_grid, show_dots=show_dots,
            show_label=True,
        )
        img.paste(cell, (x, y))

    return img


def make_comparison_grid(
    char_list: list[str],
    fonts: list[ShxFont],
    cell_px: int = 128,
) -> Image.Image:
    """
    上段: SHX から直接レンダリング（青い線）
    下段: KANDAT IP 値から読み出しレンダリング（緑の線）
    → 2段グリッドで比較
    """
    valid = [(ch, char_to_sno(ch)) for ch in char_list
             if char_to_sno(ch) is not None]

    cols = min(8, len(valid))
    header_h = 50
    row_h = cell_px + 24  # セル + ラベル領域
    margin = 10
    W = cols * cell_px + 2*margin
    H = 2 * row_h + header_h + 2*margin

    bg = (12, 12, 20)
    img = Image.new('RGB', (W, H), bg)
    d = ImageDraw.Draw(img)

    # ヘッダー
    d.text((margin, 12), "KANDAT v7 比較ビュー  上段=SHX直接  下段=IP値", fill=(160,180,220))
    d.text((margin, 28), "青=SHX線  緑=IP値線  橙=端点", fill=(100,110,130))

    for idx, (ch, sno) in enumerate(valid[:cols]):
        col = idx
        x = margin + col * cell_px

        # 上段: SHX 直接レンダリング
        y1 = header_h + margin
        cell_shx = render_char_to_image(
            sno, fonts, cell_px=cell_px,
            fg_color=(80, 160, 255), dot_color=(255, 100, 60),
            show_label=True, show_grid=True, show_dots=True,
        )
        img.paste(cell_shx, (x, y1))

        # 下段: IP 値から
        y2 = y1 + row_h
        pts = render_shape_to_ips(sno, fonts, fonts[0])
        cell_ip = render_ips_to_image(
            pts, cell_px=cell_px,
            fg_color=(60, 200, 100), dot_color=(255, 180, 60),
            show_label=True, show_grid=True, show_dots=True,
            title=f"{ch} 0x{sno:04X} IP値",
        )
        img.paste(cell_ip, (x, y2))

    return img


def render_shape_to_ips(sno, fonts, font_big) -> list[int]:
    """exec_shape → normalize → IP値"""
    above = font_big.above; below = font_big.below; adv = font_big.above
    total_y = (above+below) or adv
    GRID = 32.0; MAX_IPS = 9*6-1

    segs = exec_shape(sno, fonts)
    if not segs: return []

    xs = [s[0] for s in segs]+[s[2] for s in segs]
    ys = [s[1] for s in segs]+[s[3] for s in segs]
    xmn,xmx = min(xs),max(xs); ymn,ymx = min(ys),max(ys)
    in_range = (xmx<=adv*1.1 and ymn>=-total_y*0.1 and ymx<=total_y*1.1)

    if in_range:
        def norm(x,y):
            xi=max(0,min(32,round(x/adv*GRID)))
            yi=max(0,min(32,round((y+below)/total_y*GRID)))
            return xi,yi
    else:
        xr=xmx-xmn or float(adv); yr=ymx-ymn or float(total_y)
        def norm(x,y):
            xi=max(0,min(32,round((x-xmn)/xr*GRID)))
            yi=max(0,min(32,round((y-ymn)/yr*GRID)))
            return xi,yi

    raw=[]; prev=None
    for x1,y1,x2,y2 in segs:
        xi1,yi1=norm(x1,y1); xi2,yi2=norm(x2,y2)
        if (xi1,yi1)!=prev: raw.append((xi1,yi1,True))
        raw.append((xi2,yi2,False)); prev=(xi2,yi2)

    clean=[]
    for k in range(len(raw)):
        xi,yi,pu=raw[k]
        if pu:
            if k+1<len(raw) and not raw[k+1][2]: clean.append((xi,yi,True))
        else: clean.append((xi,yi,False))

    if not clean: return []
    if len(clean)>MAX_IPS:
        step=len(clean)/MAX_IPS
        clean=[clean[round(j*step)] for j in range(MAX_IPS)]
    return [(2 if pu else 1)*10000+xi*100+yi for xi,yi,pu in clean]


# ──────────────────────────────────────────────────────
# コンソール出力（テキスト ASCII プレビュー）
# ──────────────────────────────────────────────────────
def print_char_ascii(sno, fonts, size=20):
    segs = exec_shape(sno, fonts)
    if not segs:
        print(f"  (no segments for 0x{sno:04X})")
        return

    font_big = fonts[0]
    above = font_big.above; below = font_big.below; adv = font_big.above
    total_y = (above+below) or adv

    xs=[s[0] for s in segs]+[s[2] for s in segs]
    ys=[s[1] for s in segs]+[s[3] for s in segs]
    xmn,xmx=min(xs),max(xs); ymn,ymx=min(ys),max(ys)
    in_range=(xmx<=adv*1.1 and ymn>=-total_y*0.1 and ymx<=total_y*1.1)

    if in_range:
        def norm(x,y): return x/adv*size, (y+below)/total_y*size
    else:
        xr=xmx-xmn or 1; yr=ymx-ymn or 1
        def norm(x,y): return (x-xmn)/xr*size, (y-ymn)/yr*size

    g=[['·'for _ in range(size+1)]for _ in range(size+1)]
    for x1,y1,x2,y2 in segs:
        nx1,ny1=norm(x1,y1); nx2,ny2=norm(x2,y2)
        xi1=round(nx1); yi1=size-round(ny1)
        xi2=round(nx2); yi2=size-round(ny2)
        dx=abs(xi2-xi1); dy=abs(yi2-yi1)
        sx=1 if xi1<xi2 else -1; sy=1 if yi1<yi2 else -1
        e=dx-dy; cx,cy=xi1,yi1
        while True:
            if 0<=cx<=size and 0<=cy<=size: g[cy][cx]='█'
            if cx==xi2 and cy==yi2: break
            e2=2*e
            if e2>-dy: e-=dy; cx+=sx
            if e2<dx: e+=dx; cy+=sy

    ch=sno_to_char(sno); n_segs=len(segs)
    print(f"  {ch}  0x{sno:04X}  {n_segs} segs")
    for r in g: print("  "+''.join(r))


# ──────────────────────────────────────────────────────
# メイン
# ──────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description='KANDAT 字符可視化ツール')
    ap.add_argument('--char', '-c', default='見立川算橋計間軸線図区亜幕',
                    help='表示する文字列（デフォルト: 見立川算橋計間軸線図区亜幕）')
    ap.add_argument('--dir', '-d', default=FONT_DIR, help='SHX フォントディレクトリ')
    ap.add_argument('--shx1', default=BASE_SHX)
    ap.add_argument('--shx2', default=BIG_SHX)
    ap.add_argument('--cell', type=int, default=128, help='セルサイズ (px)')
    ap.add_argument('--cols', type=int, default=7, help='グリッド列数')
    ap.add_argument('--out', '-o', default='kandat_view.png', help='出力ファイル')
    ap.add_argument('--compare', action='store_true',
                    help='SHX直接描画とIP値描画の比較グリッド')
    ap.add_argument('--ascii', action='store_true',
                    help='ASCII プレビューをコンソールに出力')
    ap.add_argument('--no-grid', action='store_true', help='グリッド線を非表示')
    ap.add_argument('--no-dots', action='store_true', help='端点ドットを非表示')
    args = ap.parse_args()

    # フォント読み込み
    shx1_path = os.path.join(args.dir, args.shx1)
    shx2_path = os.path.join(args.dir, args.shx2)
    print(f"フォント読み込み...")
    try:
        font1 = ShxFont(shx1_path)
        font2 = ShxFont(shx2_path)
    except FileNotFoundError as e:
        print(f"エラー: {e}"); sys.exit(1)
    fonts = [font2, font1]
    print(f"  {args.shx2}: {len(font2.shapes):,} 形狀  above={font2.above}")
    print(f"  {args.shx1}: {len(font1.shapes):,} 形狀")

    char_list = list(dict.fromkeys(args.char))  # 重複除去

    # ASCII プレビュー
    if args.ascii:
        print("\n=== ASCII プレビュー ===")
        for ch in char_list:
            sno = char_to_sno(ch)
            if sno is None: continue
            print_char_ascii(sno, fonts)
            print()

    # 比較グリッド
    if args.compare:
        print(f"\n比較グリッド生成中 ({len(char_list[:8])} 字)...")
        grid = make_comparison_grid(char_list[:8], fonts, cell_px=args.cell)
        out = args.out.replace('.png', '_compare.png')
        grid.save(out)
        print(f"保存: {out}  ({grid.width}×{grid.height}px)")
        return

    # 通常グリッド
    print(f"\nグリッド生成中 ({len(char_list)} 字, {args.cell}px/cell, {args.cols}列)...")
    grid = make_char_grid(
        char_list, fonts,
        cell_px=args.cell,
        cols=args.cols,
        show_grid=not args.no_grid,
        show_dots=not args.no_dots,
    )
    grid.save(args.out)
    print(f"保存: {args.out}  ({grid.width}×{grid.height}px)")

    # セグメント統計
    print("\n=== 各字符統計 ===")
    print(f"{'字':3} {'0xSNO':7} {'segs':>5} {'正規化':5}  bbox")
    print("─" * 55)
    for ch in char_list:
        sno = char_to_sno(ch)
        if sno is None: continue
        segs = exec_shape(sno, fonts)
        if segs:
            xs=[s[0] for s in segs]+[s[2] for s in segs]
            ys=[s[1] for s in segs]+[s[3] for s in segs]
            adv=font2.above; total_y=(font2.above+font2.below) or adv
            in_range=(max(xs)<=adv*1.1 and min(ys)>=-total_y*0.1 and max(ys)<=total_y*1.1)
            mode="std " if in_range else "bbox"
            bb=f"x=[{min(xs):.0f},{max(xs):.0f}] y=[{min(ys):.0f},{max(ys):.0f}]"
        else:
            mode="----"; bb="(none)"
        print(f"  {ch:3} 0x{sno:04X} {len(segs):>5} {mode}  {bb}")


if __name__ == '__main__':
    main()