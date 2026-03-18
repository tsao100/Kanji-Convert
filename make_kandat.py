#!/usr/bin/env python3
"""
make_kandat.py
==============
KanjiVG SVG フォント → KANDAT.DAT + KANDAT2.DAT  (Shift-JIS 内符)

BASIC コード KANJI2 サブルーチンからの逆算:

┌─────────────────────────────────────────────────────────────────────┐
│  KANDAT*.DAT フォーマット  (BASIC RANDOM ファイル, LEN=32)           │
│                                                                     │
│  Record  1 .. 8   : ヘッダー予約 (ゼロ埋め, 32 bytes × 8 = 256 B)  │
│  Record  XYCODE+8 : キャラクターデータ                               │
│    Word  0..14  (int16 × 15) : ストロークデータ (IP 値)              │
│    Word  15     (int16 × 1 ) : チェーンポインター                    │
│                                (次レコードのスロット番号, 0=終端)    │
└─────────────────────────────────────────────────────────────────────┘

IP 値エンコード:
  IP = pen_flag × 10000 + X × 100 + Y
  IPEN = INT(IP / 10000) + 1
    pen_flag = 1 → IPEN = 2 → 描線 (draw)
    pen_flag = 2 → IPEN = 3 → 移動 (pen up / new stroke)
  X = (IP mod 10000) / 100   整数  0..32 (左→右)
  Y = IP mod 100              整数  0..32 (下→上, SVG Y 軸反転)

KANDAT  ファイル割り当て (XYCODE ≤ 4000):
  非漢字 (KTYPE 1)  スロット    1 ..  453
  漢字一 (KTYPE 2)  スロット  454 .. 3461
  特殊   (KTYPE 4)  スロット 3519 .. 3693
  → 最大スロット = 3693

KANDAT2 ファイル割り当て (XYCODE > 4000, stored as XYCODE-4000):
  漢字二 (KTYPE 3)  スロット    1 .. 3572
"""

from __future__ import annotations
import os, re, math, struct, glob, argparse
from xml.etree import ElementTree as ET

# ═══════════════════════════════════════════════════════════════════════
# 定数
# ═══════════════════════════════════════════════════════════════════════

KANJI_DIR  = r'.\kanji'
OUTPUT1    = 'KANDAT.DAT'
OUTPUT2    = 'KANDAT2.DAT'

REC_SIZE   = 32     # bytes per record (BASIC LEN=32)
INTS_PER   = 16     # int16 per record
DATA_INTS  = 15     # data words per record (word 15 = chain ptr)
GRID       = 32.0   # 座標グリッド 0..32
SVG_VB     = 109.0  # KanjiVG 標準 viewBox 幅/高さ
MAX_RECS   = 8      # チェーン最大レコード数 (BASIC: NR < 8)

MAX_SLOT1  = 3693   # KANDAT.DAT  プライマリスロット上限
MAX_SLOT2  = 3572   # KANDAT2.DAT プライマリスロット上限

# BASIC ZKC 非漢字 JIS 範囲ペア
ZKC = [
    0x2120, 0x217F,   # 記号・句読点
    0x2220, 0x222F,   # 特殊記号
    0x232F, 0x233A,   # 全角数字 0-9
    0x2340, 0x235B,   # 全角大文字 A-Z
    0x2360, 0x237B,   # 全角小文字 a-z
    0x2420, 0x2474,   # ひらがな
    0x2520, 0x2577,   # カタカナ
    0x2620, 0x2639,   # ギリシャ大文字
    0x2640, 0x2659,   # ギリシャ小文字
    0x2720, 0x2742,   # キリル大文字
    0x2750, 0x2772,   # キリル小文字
]

# ═══════════════════════════════════════════════════════════════════════
# JIS / Shift-JIS / Unicode 変換
# ═══════════════════════════════════════════════════════════════════════

def mstojis(mscode: int) -> int:
    """Shift-JIS (MS 符号) → JIS X 0208。BASIC MSTOJIS 関数の完全再現。"""
    il = mscode & 0xFF
    ih = (mscode >> 8) & 0xFF
    # 第1バイト変換
    if ih <= 159:
        ihh = 2 * (ih - 129) + 33
    else:
        ihh = 2 * (ih - 224) + 95
    if il >= 159:
        ihh += 1
    # 第2バイト変換
    if   64  <= il <= 126: ill = il - 31
    elif 128 <= il <= 158: ill = il - 32
    elif 159 <= il <= 252: ill = il - 126
    else:
        return 0
    return (ihh << 8) | ill


def jistoxy(jiscode: int) -> int:
    """JIS X 0208 → XYCODE (KANDAT スロット番号)。BASIC JISTOXY 関数の完全再現。"""
    hcd = (jiscode >> 8) & 0xFF
    lcd = jiscode & 0xFF

    # 優先度順に KTYPE 判定 (後勝ち, 範囲重複なし)
    ktype = 0
    if 0x2120 < jiscode < 0x277E: ktype = 1
    if 0x30   <= hcd    <= 0x4F:  ktype = 2
    if 0x50   <= hcd    <= 0x75:  ktype = 3
    if 0x7620 < jiscode < 0x76D0: ktype = 4

    if ktype == 1:
        # 非漢字: ZKC 範囲ペアを順に走査
        kcbase = 0
        for i in range(1, len(ZKC), 2):
            if ZKC[i-1] < jiscode < ZKC[i]:
                return jiscode - ZKC[i-1] + kcbase
            kcbase += ZKC[i] - ZKC[i-1] - 1
        return 0

    elif ktype == 2:
        # 第一水準漢字
        if not (0x21 <= lcd <= 0x7E):
            return 0
        return (hcd - 0x30) * 94 + (lcd - 0x20) + 453

    elif ktype == 3:
        # 第二水準漢字 → KANDAT2.DAT (XYCODE > 4000)
        if not (0x21 <= lcd <= 0x7E):
            return 0
        return (hcd - 0x50) * 94 + (lcd - 0x20) + 4000

    elif ktype == 4:
        # 追加漢字
        return jiscode - 0x7620 + 3518

    return 0


def jis_to_ucp(jiscode: int):
    """JIS X 0208 コード → Unicode コードポイント (int) または None。
    EUC-JP: euc_hi = jis_hi | 0x80, euc_lo = jis_lo | 0x80
    """
    hi = (jiscode >> 8) & 0xFF
    lo = jiscode & 0xFF
    try:
        ch = bytes([hi | 0x80, lo | 0x80]).decode('euc-jp')
        return ord(ch) if len(ch) == 1 else None
    except (UnicodeDecodeError, ValueError):
        return None


def build_xycode_maps() -> tuple[dict, dict]:
    """Shift-JIS 2バイト空間を全走査して XYCODE → Unicode マップを構築。

    Returns:
        map1 : KANDAT.DAT  用 {xycode: unicode_cp}  (xycode 1..MAX_SLOT1)
        map2 : KANDAT2.DAT 用 {xycode: unicode_cp}  (xycode = raw_xycode-4000)
    """
    map1: dict[int, int] = {}
    map2: dict[int, int] = {}

    for hi in range(0x81, 0xF0):
        for lo in list(range(0x40, 0x7F)) + list(range(0x80, 0xFD)):
            try:
                ch = bytes([hi, lo]).decode('cp932')
            except (UnicodeDecodeError, ValueError):
                continue
            if len(ch) != 1:
                continue
            ucp = ord(ch)
            mscode  = (hi << 8) | lo
            jiscode = mstojis(mscode)
            if not jiscode:
                continue
            xycode  = jistoxy(jiscode)
            if xycode <= 0:
                continue

            if xycode > 4000:
                slot = xycode - 4000
                map2.setdefault(slot, ucp)
            else:
                map1.setdefault(xycode, ucp)

    return map1, map2


# ═══════════════════════════════════════════════════════════════════════
# SVG パスパーサー (KanjiVG 用)
# ═══════════════════════════════════════════════════════════════════════

def _cubic(p0, p1, p2, p3, n=8):
    """3次ベジェ曲線を n 点でサンプリング（終点含む）。"""
    for k in range(1, n + 1):
        t = k / n; m = 1 - t
        yield (m**3*p0[0]+3*m**2*t*p1[0]+3*m*t**2*p2[0]+t**3*p3[0],
               m**3*p0[1]+3*m**2*t*p1[1]+3*m*t**2*p2[1]+t**3*p3[1])


def _quadratic(p0, p1, p2, n=6):
    """2次ベジェ曲線を n 点でサンプリング（終点含む）。"""
    for k in range(1, n + 1):
        t = k / n; m = 1 - t
        yield (m**2*p0[0]+2*m*t*p1[0]+t**2*p2[0],
               m**2*p0[1]+2*m*t*p1[1]+t**2*p2[1])


def _arc(x0, y0, rx, ry, x_rot_deg, large_arc, sweep, x1, y1, n=8):
    """SVG 楕円弧を n 点折れ線近似（終点含む）。"""
    if rx == 0 or ry == 0:
        yield (x1, y1); return
    phi = math.radians(x_rot_deg)
    cp, sp = math.cos(phi), math.sin(phi)
    dx2, dy2 = (x0 - x1) / 2, (y0 - y1) / 2
    x1p =  cp * dx2 + sp * dy2
    y1p = -sp * dx2 + cp * dy2
    lam = (x1p / rx) ** 2 + (y1p / ry) ** 2
    if lam > 1:
        s = math.sqrt(lam); rx *= s; ry *= s
    num = max(0.0, rx**2*ry**2 - rx**2*y1p**2 - ry**2*x1p**2)
    den = rx**2*y1p**2 + ry**2*x1p**2
    sq  = (math.sqrt(num / den) if den else 0.0) * (-1 if large_arc == sweep else 1)
    cxp =  sq * rx * y1p / ry
    cyp = -sq * ry * x1p / rx
    cx  = cp * cxp - sp * cyp + (x0 + x1) / 2
    cy  = sp * cxp + cp * cyp + (y0 + y1) / 2

    def _ang(ux, uy, vx, vy):
        return math.atan2(ux * vy - uy * vx, ux * vx + uy * vy)

    th1 = _ang(1, 0, (x1p - cxp) / rx, (y1p - cyp) / ry)
    dth = _ang((x1p-cxp)/rx, (y1p-cyp)/ry, (-x1p-cxp)/rx, (-y1p-cyp)/ry)
    if not sweep and dth > 0: dth -= 2 * math.pi
    if sweep and dth < 0:     dth += 2 * math.pi

    for k in range(1, n + 1):
        th = th1 + (k / n) * dth
        yield (cp*rx*math.cos(th) - sp*ry*math.sin(th) + cx,
               sp*rx*math.cos(th) + cp*ry*math.sin(th) + cy)


def _nums_from(tokens, i):
    """tokens[i] 以降の連続数値を読み出す。"""
    while i < len(tokens) and not tokens[i].isalpha():
        yield float(tokens[i]); i += 1
    return i


def parse_svg_path(d: str) -> list[tuple[float, float, bool]]:
    """SVG path d 属性 → [(x, y, pen_up), ...]
    pen_up=True  : 移動 (新ストローク開始)
    pen_up=False : 描線
    """
    tok = re.findall(
        r'[MmLlHhVvCcSsQqTtAaZz]'
        r'|[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?',
        d
    )
    out = []
    cx = cy = sx = sy = 0.0
    prev_ctrl = None   # 直前の制御点 (S/T 用)
    i = 0

    while i < len(tok):
        cmd = tok[i]; i += 1
        rel = cmd.islower(); C = cmd.upper()

        if C == 'M':
            first = True
            while i < len(tok) and not tok[i].isalpha():
                x, y = float(tok[i]), float(tok[i+1]); i += 2
                if rel: x += cx; y += cy
                cx, cy = x, y
                if first:
                    sx, sy = x, y
                    out.append((x, y, True))
                    first = False
                else:
                    out.append((x, y, False))   # 暗黙の L
            prev_ctrl = None

        elif C == 'L':
            while i < len(tok) and not tok[i].isalpha():
                x, y = float(tok[i]), float(tok[i+1]); i += 2
                if rel: x += cx; y += cy
                cx, cy = x, y; out.append((x, y, False))
            prev_ctrl = None

        elif C == 'H':
            while i < len(tok) and not tok[i].isalpha():
                x = float(tok[i]); i += 1
                if rel: x += cx
                cx = x; out.append((cx, cy, False))
            prev_ctrl = None

        elif C == 'V':
            while i < len(tok) and not tok[i].isalpha():
                y = float(tok[i]); i += 1
                if rel: y += cy
                cy = y; out.append((cx, cy, False))
            prev_ctrl = None

        elif C == 'C':
            while i < len(tok) and not tok[i].isalpha():
                x1,y1,x2,y2,x,y = (float(tok[i+k]) for k in range(6)); i += 6
                if rel: x1+=cx;y1+=cy; x2+=cx;y2+=cy; x+=cx;y+=cy
                for px, py in _cubic((cx,cy),(x1,y1),(x2,y2),(x,y)):
                    out.append((px, py, False))
                prev_ctrl = (x2, y2); cx, cy = x, y

        elif C == 'S':
            while i < len(tok) and not tok[i].isalpha():
                x2,y2,x,y = (float(tok[i+k]) for k in range(4)); i += 4
                if rel: x2+=cx;y2+=cy; x+=cx;y+=cy
                x1 = 2*cx - prev_ctrl[0] if prev_ctrl else cx
                y1 = 2*cy - prev_ctrl[1] if prev_ctrl else cy
                for px, py in _cubic((cx,cy),(x1,y1),(x2,y2),(x,y)):
                    out.append((px, py, False))
                prev_ctrl = (x2, y2); cx, cy = x, y

        elif C == 'Q':
            while i < len(tok) and not tok[i].isalpha():
                x1,y1,x,y = (float(tok[i+k]) for k in range(4)); i += 4
                if rel: x1+=cx;y1+=cy; x+=cx;y+=cy
                for px, py in _quadratic((cx,cy),(x1,y1),(x,y)):
                    out.append((px, py, False))
                prev_ctrl = (x1, y1); cx, cy = x, y

        elif C == 'T':
            while i < len(tok) and not tok[i].isalpha():
                x, y = float(tok[i]), float(tok[i+1]); i += 2
                if rel: x += cx; y += cy
                x1 = 2*cx - prev_ctrl[0] if prev_ctrl else cx
                y1 = 2*cy - prev_ctrl[1] if prev_ctrl else cy
                for px, py in _quadratic((cx,cy),(x1,y1),(x,y)):
                    out.append((px, py, False))
                prev_ctrl = (x1, y1); cx, cy = x, y

        elif C == 'A':
            while i < len(tok) and not tok[i].isalpha():
                rx,ry,xr,la,sw,x,y = (float(tok[i+k]) for k in range(7)); i += 7
                if rel: x += cx; y += cy
                for px, py in _arc(cx, cy, rx, ry, xr, int(la), int(sw), x, y):
                    out.append((px, py, False))
                prev_ctrl = None; cx, cy = x, y

        elif C == 'Z':
            if abs(cx - sx) > 0.5 or abs(cy - sy) > 0.5:
                out.append((sx, sy, False))
            cx, cy = sx, sy; prev_ctrl = None

    return out


# ═══════════════════════════════════════════════════════════════════════
# 座標変換 & IP エンコーディング
# ═══════════════════════════════════════════════════════════════════════

def svg_to_grid(pts: list, vb_w: float, vb_h: float) -> list:
    """SVG 座標 (0..vb) → KANDAT グリッド (X 0..32, Y 0..32)。
    Y 軸反転: SVG 下向き → KANDAT 上向き (数学座標系)。
    """
    return [(x / vb_w * GRID, (vb_h - y) / vb_h * GRID, pu)
            for x, y, pu in pts]


def deduplicate(pts: list) -> list:
    """連続する同一グリッド点を除去（pen_up 点は位置関係なく保持）。"""
    out = []; prev = None
    for x, y, pu in pts:
        key = (round(x), round(y))
        if pu or key != prev:
            out.append((x, y, pu))
            if not pu:
                prev = key
    return out


def to_ip_values(pts: list) -> list[int]:
    """グリッド座標列 → KANDAT IP 値リスト。
    pen_flag=2 (IPEN=3, 移動), pen_flag=1 (IPEN=2, 描線)
    IP = pen_flag×10000 + X×100 + Y
    """
    ips = []
    for x, y, pu in pts:
        xi = max(0, min(32, round(x)))
        yi = max(0, min(32, round(y)))
        flag = 2 if pu else 1
        ips.append(flag * 10000 + xi * 100 + yi)
    return ips


# ═══════════════════════════════════════════════════════════════════════
# KanjiVG SVG ファイル検索 & 変換
# ═══════════════════════════════════════════════════════════════════════

def find_svg(kanji_dir: str, ucp: int) -> str | None:
    """Unicode コードポイントに対応する KanjiVG SVG を検索。
    命名規則: {codepoint_5hex}.svg  例: 04e00.svg
    バリアント (例: 04e00-Kaisho.svg) は base が無い場合に使用。
    """
    base = f'{ucp:05x}'
    exact = os.path.join(kanji_dir, f'{base}.svg')
    if os.path.isfile(exact):
        return exact
    variants = sorted(glob.glob(os.path.join(kanji_dir, f'{base}-*.svg')))
    return variants[0] if variants else None


def process_svg(svg_path: str) -> list[int]:
    """KanjiVG SVG を読み込み KANDAT 用 IP 値リストを返す。
    SVG が無効または空の場合は空リストを返す。
    """
    try:
        tree = ET.parse(svg_path)
    except ET.ParseError:
        return []

    root = tree.getroot()
    m = re.match(r'\{([^}]*)\}', root.tag)
    ns = m.group(1) if m else ''
    tag = (lambda t: f'{{{ns}}}{t}') if ns else (lambda t: t)

    vb_str = root.get('viewBox', f'0 0 {SVG_VB} {SVG_VB}')
    vb = list(map(float, vb_str.split()))
    vb_w = vb[2] if len(vb) >= 3 else SVG_VB
    vb_h = vb[3] if len(vb) >= 4 else SVG_VB

    all_pts = []
    for elem in root.iter(tag('path')):
        d = elem.get('d', '').strip()
        if d:
            all_pts.extend(parse_svg_path(d))

    if not all_pts:
        return []

    grid = svg_to_grid(all_pts, vb_w, vb_h)
    dedup = deduplicate(grid)
    return to_ip_values(dedup)


# ═══════════════════════════════════════════════════════════════════════
# レコードパッキング
# ═══════════════════════════════════════════════════════════════════════

def pack_to_records(ip_values: list[int]) -> list[list[int]]:
    """IP 値リストを 16-int レコードリスト (チェーン=0 初期化) に変換。

    レコード構造:
      Word 0..14 : データ (最初レコードの Word 0 = NPKAN カウント)
      Word 15    : チェーンポインター (スロット番号, 0=終端)

    最大 8 レコードチェーン: count(1) + 14 + 7×15 = 120 語 → IP 最大 119 個。
    """
    max_pts = MAX_RECS * DATA_INTS - 1   # 8×15-1 = 119
    if len(ip_values) > max_pts:
        ip_values = ip_values[:max_pts]

    # データストリーム: [count, ip1, ip2, ...]
    stream = [len(ip_values)] + ip_values

    records = []
    for i in range(0, len(stream), DATA_INTS):
        chunk = stream[i : i + DATA_INTS]
        chunk += [0] * (DATA_INTS - len(chunk))  # パディング
        records.append(chunk + [0])               # chain=0 (後で設定)

    if not records:
        records = [[0] * INTS_PER]   # 空文字: count=0 のみ

    return records


# ═══════════════════════════════════════════════════════════════════════
# KANDAT バイナリ生成
# ═══════════════════════════════════════════════════════════════════════

def build_kandat_bytes(
    xycode_map : dict[int, int],
    kanji_dir  : str,
    max_primary: int,
    verbose    : bool,
    label      : str,
) -> bytes:
    """KANDAT バイナリデータを生成して返す。

    Args:
        xycode_map : {slot_index: unicode_cp}
        kanji_dir  : KanjiVG SVG ディレクトリ
        max_primary: プライマリスロット最大値
        verbose    : 詳細ログ表示
        label      : ログ用ラベル

    Returns:
        bytes: KANDAT ファイルバイナリ
    """
    print(f'\n{"═"*60}')
    print(f'  [{label}]  プライマリスロット上限: {max_primary}')
    print(f'{"═"*60}')

    # ── Step 1: 各スロットの IP 値を生成 ─────────────────────────────────
    ip_by_slot: dict[int, list[int]] = {}
    found = missing = empty = 0

    for slot in range(1, max_primary + 1):
        ucp = xycode_map.get(slot)
        if ucp is None:
            ip_by_slot[slot] = []
            empty += 1
            continue

        svg = find_svg(kanji_dir, ucp)
        if svg:
            ips = process_svg(svg)
            found += 1
            if verbose:
                try:    ch = chr(ucp)
                except  ValueError: ch = '?'
                print(f'  slot {slot:4d}  U+{ucp:04X} {ch}  {os.path.basename(svg):22s}  {len(ips):3d}pt')
        else:
            ips = []
            missing += 1

        ip_by_slot[slot] = ips

    # ── Step 2: レコードパッキング & チェーンスロット割り当て ─────────────
    flat: dict[int, list[int]] = {}   # slot → record (16 ints)
    next_extra = max_primary + 1      # 継続レコード用スロット番号開始位置

    for slot in range(1, max_primary + 1):
        records = pack_to_records(ip_by_slot.get(slot, []))

        if len(records) == 1:
            # チェーン不要
            flat[slot] = records[0]
        else:
            # 継続スロットを割り当てチェーンポインターを設定
            cont_count = len(records) - 1
            cont_slots = list(range(next_extra, next_extra + cont_count))
            next_extra += cont_count
            all_slots = [slot] + cont_slots

            for idx in range(len(records) - 1):
                records[idx][15] = all_slots[idx + 1]  # 次スロット→チェーン
            records[-1][15] = 0                         # 最後は 0 (終端)

            for s, r in zip(all_slots, records):
                flat[s] = r

    # ── Step 3: バイト配列生成 ────────────────────────────────────────────
    # BASIC: RECNO = XYCODE + 8  →  file_offset = (RECNO-1)*32 = (slot+7)*32
    max_slot  = max(flat.keys()) if flat else max_primary
    file_size = (max_slot + 8) * REC_SIZE   # records 1..8 (header) + 1..max_slot (data)
    buf       = bytearray(file_size)

    for slot, record in flat.items():
        offset = (slot + 7) * REC_SIZE
        for j, val in enumerate(record):
            # CVI = signed 16-bit little-endian
            struct.pack_into('<h', buf, offset + j * 2, val)

    n_cont = next_extra - max_primary - 1
    print(f'\n  SVG あり    : {found:5d}')
    print(f'  SVG なし    : {missing:5d}  (Unicode マップなし: {empty})')
    print(f'  継続レコード: {n_cont:5d}')
    print(f'  最大スロット: {max_slot:5d}')
    print(f'  ファイルサイズ: {len(buf):,} bytes ({len(buf)//1024} KB)')
    return bytes(buf)


# ═══════════════════════════════════════════════════════════════════════
# エントリーポイント
# ═══════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description='KanjiVG SVG → KANDAT.DAT + KANDAT2.DAT'
    )
    parser.add_argument(
        '--kanji-dir', default=KANJI_DIR,
        help=f'KanjiVG SVG ディレクトリ (デフォルト: {KANJI_DIR})'
    )
    parser.add_argument(
        '--out1', default=OUTPUT1,
        help=f'KANDAT.DAT 出力パス (デフォルト: {OUTPUT1})'
    )
    parser.add_argument(
        '--out2', default=OUTPUT2,
        help=f'KANDAT2.DAT 出力パス (デフォルト: {OUTPUT2})'
    )
    parser.add_argument(
        '--verbose', '-v', action='store_true',
        help='各文字の処理詳細を表示'
    )
    args = parser.parse_args()

    print('KanjiVG → KANDAT ジェネレーター')
    print(f'  SVG ディレクトリ : {args.kanji_dir}')
    print(f'  KANDAT.DAT  出力 : {args.out1}')
    print(f'  KANDAT2.DAT 出力 : {args.out2}')

    # ── Shift-JIS → XYCODE → Unicode マップを構築 ────────────────────────
    print('\nShift-JIS コード空間をスキャン中...')
    map1, map2 = build_xycode_maps()
    print(f'  KANDAT.DAT  マップ: {len(map1)} エントリ')
    print(f'  KANDAT2.DAT マップ: {len(map2)} エントリ')

    # ── KANDAT.DAT 生成 ───────────────────────────────────────────────────
    dat1 = build_kandat_bytes(map1, args.kanji_dir, MAX_SLOT1, args.verbose, 'KANDAT.DAT')
    with open(args.out1, 'wb') as f:
        f.write(dat1)
    print(f'\n  → {args.out1} 書き込み完了')

    # ── KANDAT2.DAT 生成 ──────────────────────────────────────────────────
    dat2 = build_kandat_bytes(map2, args.kanji_dir, MAX_SLOT2, args.verbose, 'KANDAT2.DAT')
    with open(args.out2, 'wb') as f:
        f.write(dat2)
    print(f'  → {args.out2} 書き込み完了')

    print('\n完了。')


if __name__ == '__main__':
    main()