#!/usr/bin/env python3
"""
make_kandat.py
==============
TTF フォント (chogokubosogothic_5.ttf) → KANDAT.DAT + KANDAT2.DAT  (Shift-JIS 内符)

依存ライブラリ:
    pip install fonttools

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
  Y = IP mod 100              整数  0..32 (下→上)

KANDAT  ファイル割り当て (XYCODE ≤ 4000):
  非漢字 (KTYPE 1)  スロット    1 ..  453
  漢字一 (KTYPE 2)  スロット  454 .. 3461
  特殊   (KTYPE 4)  スロット 3519 .. 3693
  → 最大スロット = 3693

KANDAT2 ファイル割り当て (XYCODE > 4000, stored as XYCODE-4000):
  漢字二 (KTYPE 3)  スロット    1 .. 3572
"""

from __future__ import annotations
import os, re, math, struct, argparse
from fontTools.ttLib import TTFont
from fontTools.pens.recordingPen import RecordingPen

# ═══════════════════════════════════════════════════════════════════════
# 定数
# ═══════════════════════════════════════════════════════════════════════

FONT_PATH  = 'chogokubosogothic_5.ttf'
OUTPUT1    = 'KANDAT.DAT'
OUTPUT2    = 'KANDAT2.DAT'

REC_SIZE   = 32     # bytes per record (BASIC LEN=32)
INTS_PER   = 16     # int16 per record
DATA_INTS  = 15     # data words per record (word 15 = chain ptr)
GRID       = 32.0   # 座標グリッド 0..32
MAX_RECS   = 8      # チェーン最大レコード数 (BASIC: NR < 8)

MAX_SLOT1  = 3693   # KANDAT.DAT  プライマリスロット上限
MAX_SLOT2  = 3572   # KANDAT2.DAT プライマリスロット上限

# BASIC ZKC 非漢字 JIS 範囲ペア
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

# ═══════════════════════════════════════════════════════════════════════
# JIS / Shift-JIS / Unicode 変換
# ═══════════════════════════════════════════════════════════════════════

def mstojis(mscode: int) -> int:
    """Shift-JIS (MS 符号) → JIS X 0208。BASIC MSTOJIS 関数の完全再現。"""
    il = mscode & 0xFF
    ih = (mscode >> 8) & 0xFF
    if ih <= 159:
        ihh = 2 * (ih - 129) + 33
    else:
        ihh = 2 * (ih - 224) + 95
    if il >= 159:
        ihh += 1
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

    ktype = 0
    if 0x2120 < jiscode < 0x277E: ktype = 1
    if 0x30   <= hcd    <= 0x4F:  ktype = 2
    if 0x50   <= hcd    <= 0x75:  ktype = 3
    if 0x7620 < jiscode < 0x76D0: ktype = 4

    if ktype == 1:
        kcbase = 0
        for i in range(1, len(ZKC), 2):
            if ZKC[i-1] < jiscode < ZKC[i]:
                return jiscode - ZKC[i-1] + kcbase
            kcbase += ZKC[i] - ZKC[i-1] - 1
        return 0

    elif ktype == 2:
        if not (0x21 <= lcd <= 0x7E):
            return 0
        return (hcd - 0x30) * 94 + (lcd - 0x20) + 453

    elif ktype == 3:
        if not (0x21 <= lcd <= 0x7E):
            return 0
        return (hcd - 0x50) * 94 + (lcd - 0x20) + 4000

    elif ktype == 4:
        return jiscode - 0x7620 + 3518

    return 0


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
# TTF グリフ処理
# ═══════════════════════════════════════════════════════════════════════

def _sample_cubic(p0, p1, p2, p3, n=8):
    """3次ベジェ曲線を n 点でサンプリング（終点含む）。"""
    for k in range(1, n + 1):
        t = k / n; m = 1 - t
        yield (m**3*p0[0]+3*m**2*t*p1[0]+3*m*t**2*p2[0]+t**3*p3[0],
               m**3*p0[1]+3*m**2*t*p1[1]+3*m*t**2*p2[1]+t**3*p3[1])


def _sample_quadratic(p0, p1, p2, n=6):
    """2次ベジェ曲線を n 点でサンプリング（終点含む）。"""
    for k in range(1, n + 1):
        t = k / n; m = 1 - t
        yield (m**2*p0[0]+2*m*t*p1[0]+t**2*p2[0],
               m**2*p0[1]+2*m*t*p1[1]+t**2*p2[1])


def recording_to_pts(operations: list) -> list[tuple[float, float, bool]]:
    """RecordingPen の操作リスト → [(x, y, pen_up), ...]

    pen_up=True  : 移動 (新ストローク開始)
    pen_up=False : 描線

    TTF 座標系は Y 上向きのため、SVG のような Y 軸反転は不要。
    """
    pts: list[tuple[float, float, bool]] = []
    cx = cy = 0.0
    sx = sy = 0.0   # 現在のコンター開始点 (closePath 用)

    for op, args in operations:

        if op == 'moveTo':
            x, y = args[0]
            pts.append((x, y, True))
            cx, cy = x, y
            sx, sy = x, y

        elif op == 'lineTo':
            x, y = args[0]
            pts.append((x, y, False))
            cx, cy = x, y

        elif op == 'curveTo':
            # 3次ベジェ: args = (cp1, cp2, endpoint)  ※ CFF / OpenType
            *cps, ep = args
            if len(cps) == 2:
                x1, y1 = cps[0]
                x2, y2 = cps[1]
                x, y = ep
                for px, py in _sample_cubic((cx,cy),(x1,y1),(x2,y2),(x,y)):
                    pts.append((px, py, False))
            else:
                # 複数セグメント連鎖 (まれ)
                p0 = (cx, cy)
                for idx in range(0, len(cps) - 1, 2):
                    x1, y1 = cps[idx]
                    x2, y2 = cps[idx + 1]
                    x, y = cps[idx + 2] if idx + 2 < len(cps) else ep
                    for px, py in _sample_cubic(p0,(x1,y1),(x2,y2),(x,y)):
                        pts.append((px, py, False))
                    p0 = (x, y)
                x, y = ep
                pts.append((x, y, False))
            cx, cy = ep

        elif op == 'qCurveTo':
            # 2次スプライン: args = (off1, ..., offN, on_curve_end)  ※ TrueType
            # 連続するオフカーブ点の間には暗黙のオンカーブ点が存在する
            points = list(args)
            on_end = points[-1]
            off_pts = points[:-1]

            start = (cx, cy)
            if len(off_pts) == 1:
                # 単純な 2次ベジェ
                for px, py in _sample_quadratic(start, off_pts[0], on_end):
                    pts.append((px, py, False))
            else:
                # 複数オフカーブ: 暗黙オンカーブを挿入して分割
                for i, ctrl in enumerate(off_pts):
                    if i < len(off_pts) - 1:
                        nxt = off_pts[i + 1]
                        implicit = ((ctrl[0] + nxt[0]) / 2,
                                    (ctrl[1] + nxt[1]) / 2)
                        for px, py in _sample_quadratic(start, ctrl, implicit):
                            pts.append((px, py, False))
                        start = implicit
                    else:
                        for px, py in _sample_quadratic(start, ctrl, on_end):
                            pts.append((px, py, False))
            cx, cy = on_end

        elif op == 'closePath':
            # コンター閉鎖: 開始点に戻る (離れている場合のみ)
            if abs(cx - sx) > 0.5 or abs(cy - sy) > 0.5:
                pts.append((sx, sy, False))
            cx, cy = sx, sy

        elif op == 'endPath':
            pass   # オープンコンター終端 (KanjiVG では通常発生しない)

    return pts


def ttf_to_grid(pts: list, upm: float, y_offset: float, y_range: float) -> list:
    """TTF 座標 → KANDAT グリッド (X 0..32, Y 0..32)。

    TTF は Y 上向きのため反転不要。
    X は [0, upm]、Y は [y_offset, y_offset+y_range] を 0..32 にマップ。
    """
    return [
        (x / upm * GRID,
         (y - y_offset) / y_range * GRID,
         pu)
        for x, y, pu in pts
    ]


def load_font(font_path: str) -> TTFont:
    """TTF/OTF フォントを読み込んで返す。"""
    print(f'フォント読み込み: {font_path}')
    font = TTFont(font_path)
    upm = font['head'].unitsPerEm
    print(f'  UPM          : {upm}')

    # Y 基準の取得 (OS/2 → hhea → フォールバック)
    if 'OS/2' in font:
        asc  = font['OS/2'].sTypoAscender
        desc = font['OS/2'].sTypoDescender
        print(f'  Ascender     : {asc}  (OS/2 sTypoAscender)')
        print(f'  Descender    : {desc}  (OS/2 sTypoDescender)')
    elif 'hhea' in font:
        asc  = font['hhea'].ascent
        desc = font['hhea'].descent
        print(f'  Ascender     : {asc}  (hhea)')
        print(f'  Descender    : {desc}  (hhea)')
    else:
        asc  = upm
        desc = 0
        print(f'  Ascender     : {asc}  (フォールバック)')
        print(f'  Descender    : {desc}  (フォールバック)')

    n_glyphs = len(font.getGlyphOrder())
    print(f'  グリフ数     : {n_glyphs}')
    return font


def get_font_metrics(font: TTFont) -> tuple[float, float, float]:
    """(upm, y_offset, y_range) を返す。

    y_offset : グリッド Y=0 に対応する TTF Y 座標
    y_range  : グリッド全体 (32) に対応する TTF Y 幅
    """
    upm = float(font['head'].unitsPerEm)
    if 'OS/2' in font:
        asc  = float(font['OS/2'].sTypoAscender)
        desc = float(font['OS/2'].sTypoDescender)
    elif 'hhea' in font:
        asc  = float(font['hhea'].ascent)
        desc = float(font['hhea'].descent)
    else:
        asc  = upm
        desc = 0.0
    y_range = asc - desc if asc > desc else upm
    return upm, desc, y_range


def process_glyph(font: TTFont, glyph_name: str,
                  upm: float, y_offset: float, y_range: float) -> list[int]:
    """指定グリフの輪郭を KANDAT IP 値リストへ変換して返す。

    空グリフ・コンポーネント未解決の場合は空リストを返す。
    """
    try:
        gs = font.getGlyphSet()
        if glyph_name not in gs:
            return []

        pen = RecordingPen()
        gs[glyph_name].draw(pen)   # コンポーネントも自動展開

        if not pen.value:
            return []

        pts = recording_to_pts(pen.value)
        if not pts:
            return []

        grid  = ttf_to_grid(pts, upm, y_offset, y_range)
        dedup = deduplicate(grid)
        return to_ip_values(dedup)

    except Exception:
        return []


def find_glyph_name(font: TTFont, ucp: int) -> str | None:
    """Unicode コードポイント → グリフ名。cmap に存在しなければ None。"""
    cmap = font.getBestCmap()
    if cmap is None:
        return None
    return cmap.get(ucp)


# ═══════════════════════════════════════════════════════════════════════
# 座標変換 & IP エンコーディング  (変更なし)
# ═══════════════════════════════════════════════════════════════════════

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
# レコードパッキング  (変更なし)
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

    stream = [len(ip_values)] + ip_values

    records = []
    for i in range(0, len(stream), DATA_INTS):
        chunk = stream[i : i + DATA_INTS]
        chunk += [0] * (DATA_INTS - len(chunk))
        records.append(chunk + [0])

    if not records:
        records = [[0] * INTS_PER]

    return records


# ═══════════════════════════════════════════════════════════════════════
# KANDAT バイナリ生成
# ═══════════════════════════════════════════════════════════════════════

def build_kandat_bytes(
    xycode_map : dict[int, int],
    font       : TTFont,
    max_primary: int,
    verbose    : bool,
    label      : str,
) -> bytes:
    """KANDAT バイナリデータを生成して返す。

    Args:
        xycode_map : {slot_index: unicode_cp}
        font       : 読み込み済み TTFont オブジェクト
        max_primary: プライマリスロット最大値
        verbose    : 詳細ログ表示
        label      : ログ用ラベル

    Returns:
        bytes: KANDAT ファイルバイナリ
    """
    print(f'\n{"═"*60}')
    print(f'  [{label}]  プライマリスロット上限: {max_primary}')
    print(f'{"═"*60}')

    upm, y_offset, y_range = get_font_metrics(font)

    # ── Step 1: 各スロットの IP 値を生成 ─────────────────────────────────
    ip_by_slot: dict[int, list[int]] = {}
    found = missing = empty = 0

    for slot in range(1, max_primary + 1):
        ucp = xycode_map.get(slot)
        if ucp is None:
            ip_by_slot[slot] = []
            empty += 1
            continue

        glyph_name = find_glyph_name(font, ucp)
        if glyph_name:
            ips = process_glyph(font, glyph_name, upm, y_offset, y_range)
            found += 1
            if verbose:
                try:    ch = chr(ucp)
                except  ValueError: ch = '?'
                print(f'  slot {slot:4d}  U+{ucp:04X} {ch}  {glyph_name:22s}  {len(ips):3d}pt')
        else:
            ips = []
            missing += 1

        ip_by_slot[slot] = ips

    # ── Step 2: レコードパッキング & チェーンスロット割り当て ─────────────
    flat: dict[int, list[int]] = {}
    next_extra = max_primary + 1

    for slot in range(1, max_primary + 1):
        records = pack_to_records(ip_by_slot.get(slot, []))

        if len(records) == 1:
            flat[slot] = records[0]
        else:
            cont_count = len(records) - 1
            cont_slots = list(range(next_extra, next_extra + cont_count))
            next_extra += cont_count
            all_slots = [slot] + cont_slots

            for idx in range(len(records) - 1):
                records[idx][15] = all_slots[idx + 1]
            records[-1][15] = 0

            for s, r in zip(all_slots, records):
                flat[s] = r

    # ── Step 3: バイト配列生成 ────────────────────────────────────────────
    # BASIC: RECNO = XYCODE + 8  →  file_offset = (RECNO-1)*32 = (slot+7)*32
    max_slot  = max(flat.keys()) if flat else max_primary
    file_size = (max_slot + 8) * REC_SIZE
    buf       = bytearray(file_size)

    for slot, record in flat.items():
        offset = (slot + 7) * REC_SIZE
        for j, val in enumerate(record):
            struct.pack_into('<h', buf, offset + j * 2, val)

    n_cont = next_extra - max_primary - 1
    print(f'\n  グリフあり  : {found:5d}')
    print(f'  グリフなし  : {missing:5d}  (Unicodeマップなし: {empty})')
    print(f'  継続レコード: {n_cont:5d}')
    print(f'  最大スロット: {max_slot:5d}')
    print(f'  ファイルサイズ: {len(buf):,} bytes ({len(buf)//1024} KB)')
    return bytes(buf)


# ═══════════════════════════════════════════════════════════════════════
# エントリーポイント
# ═══════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description='TTF フォント → KANDAT.DAT + KANDAT2.DAT'
    )
    parser.add_argument(
        '--font', default=FONT_PATH,
        help=f'入力 TTF/OTF フォントパス (デフォルト: {FONT_PATH})'
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

    print('TTF → KANDAT ジェネレーター')
    print(f'  フォント     : {args.font}')
    print(f'  KANDAT.DAT   : {args.out1}')
    print(f'  KANDAT2.DAT  : {args.out2}')

    # ── フォント読み込み ──────────────────────────────────────────────────
    font = load_font(args.font)

    # ── Shift-JIS → XYCODE → Unicode マップを構築 ────────────────────────
    print('\nShift-JIS コード空間をスキャン中...')
    map1, map2 = build_xycode_maps()
    print(f'  KANDAT.DAT  マップ: {len(map1)} エントリ')
    print(f'  KANDAT2.DAT マップ: {len(map2)} エントリ')

    # ── KANDAT.DAT 生成 ───────────────────────────────────────────────────
    dat1 = build_kandat_bytes(map1, font, MAX_SLOT1, args.verbose, 'KANDAT.DAT')
    with open(args.out1, 'wb') as f:
        f.write(dat1)
    print(f'\n  → {args.out1} 書き込み完了')

    # ── KANDAT2.DAT 生成 ──────────────────────────────────────────────────
    dat2 = build_kandat_bytes(map2, font, MAX_SLOT2, args.verbose, 'KANDAT2.DAT')
    with open(args.out2, 'wb') as f:
        f.write(dat2)
    print(f'  → {args.out2} 書き込み完了')

    print('\n完了。')


if __name__ == '__main__':
    main()