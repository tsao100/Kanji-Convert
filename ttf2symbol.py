#!/usr/bin/env python3
"""
make_symbol_cod_ttf.py
======================
TTF フォントファイル → SYMBOL.COD  (QB45 BASIC 用ベクタフォント)

SYMBOL.COD バイナリフォーマット（BASIC SYMBO2/READCODE より逆算）:
  Bytes   0–511 : Lookup Table (256 char × 2 bytes, big-endian)
                  lookup[c*2..c*2+1] = IADSYM (文字データの 0-based 位置)
  Bytes 512+    : 文字データブロック
                  [count:1B][pen₁:1B][data₁:1B][pen₂:1B][data₂:1B]...
                  pen  byte: 0 = 描線(IPEN=2), 1 = 移動(IPEN=3/pen-up)
                  data byte: 高4bit = X nibble (XSYM = nibble-1, -1..14)
                             低4bit = Y nibble (YSYM = nibble-2, -2..13)

座標グリッド: X 0..11, Y 0..12 (SYMBO2 DD=12)
"""
from __future__ import annotations

import os
import math
import argparse
import struct

from fontTools import ttLib
from fontTools.pens.recordingPen import RecordingPen

# ═══════════════════════════════════════════════════════════════════════
# 定数
# ═══════════════════════════════════════════════════════════════════════

DD          = 12        # SYMBO2 グリッド幅 (X: 0..DD-1, Y: 0..DD)
MAX_STROKES = 127       # 1文字あたり最大ストローク数 (1 byte に収まる上限)
BEZIER_STEPS = 8        # ベジェ曲線サンプリング数

# デフォルト TTF: システムの IPA Gothic (Windows では別指定)
DEFAULT_TTF = (
    r'C:\Windows\Fonts\romans__.ttf'   # Windows MS Gothic
    if os.name == 'nt' else
    '/usr/share/fonts/truetype/fonts-japanese-gothic.ttf'
)
DEFAULT_OUT = 'SYMBOL.COD'

# 対象 ASCII コードポイント範囲
ASCII_RANGE = range(0x20, 0x7F)   # 0x20 (space) .. 0x7E (~)


# ═══════════════════════════════════════════════════════════════════════
# ベジェ曲線サンプリング
# ═══════════════════════════════════════════════════════════════════════

def _cubic(p0, p1, p2, p3, n=BEZIER_STEPS):
    """3次ベジェを n 点サンプリング（終点含む・起点除く）。"""
    for k in range(1, n + 1):
        t = k / n; m = 1 - t
        yield (m**3*p0[0]+3*m**2*t*p1[0]+3*m*t**2*p2[0]+t**3*p3[0],
               m**3*p0[1]+3*m**2*t*p1[1]+3*m*t**2*p2[1]+t**3*p3[1])


def _quadratic(p0, p1, p2, n=BEZIER_STEPS):
    """2次ベジェを n 点サンプリング（終点含む・起点除く）。"""
    for k in range(1, n + 1):
        t = k / n; m = 1 - t
        yield (m**2*p0[0]+2*m*t*p1[0]+t**2*p2[0],
               m**2*p0[1]+2*m*t*p1[1]+t**2*p2[1])


# ═══════════════════════════════════════════════════════════════════════
# RecordingPen → ストローク列変換
# ═══════════════════════════════════════════════════════════════════════

def recording_to_strokes(ops: list) -> list[tuple[float, float, bool]]:
    """RecordingPen の ops を [(x, y, pen_up), ...] に展開。
    pen_up=True: 移動（新ストローク開始）
    pen_up=False: 描線
    TrueType は qCurveTo (2次 B-spline)、
    CFF/OTF は curveTo (3次ベジェ) を使用する。
    """
    pts: list[tuple[float, float, bool]] = []
    cur = (0.0, 0.0)

    for op, args in ops:

        if op == 'moveTo':
            cur = args[0]
            pts.append((cur[0], cur[1], True))

        elif op == 'lineTo':
            cur = args[0]
            pts.append((cur[0], cur[1], False))

        elif op == 'curveTo':
            # 3次ベジェ (CFF): args = [(cx1,cy1),(cx2,cy2),(end)]
            # 複数セグメントが連結される場合もある
            seg = list(args)
            while len(seg) >= 3:
                p1, p2, p3 = seg[0], seg[1], seg[2]
                seg = seg[3:]
                for px, py in _cubic(cur, p1, p2, p3):
                    pts.append((px, py, False))
                cur = p3
            # 残り (通常は 0)
            if seg:
                cur = seg[-1]
                pts.append((cur[0], cur[1], False))

        elif op == 'qCurveTo':
            # TrueType 2次 B-spline:
            # 複数の off-curve 制御点が連続する場合は on-curve 点を補間して展開
            seg = list(args)
            if not seg:
                continue
            # 最後の点が on-curve (終点)
            end = seg[-1]
            offs = seg[:-1]
            if not offs:
                # lineTo 相当
                pts.append((end[0], end[1], False))
                cur = end
            else:
                # 各区間を 2次ベジェとして処理
                # off-curve が連続する場合は中間 on-curve を補間
                on_pts = []
                for i in range(len(offs) - 1):
                    mid = ((offs[i][0]+offs[i+1][0])/2,
                           (offs[i][1]+offs[i+1][1])/2)
                    on_pts.append((offs[i], mid))
                on_pts.append((offs[-1], end))

                start = cur
                for ctrl, ep in on_pts:
                    for px, py in _quadratic(start, ctrl, ep):
                        pts.append((px, py, False))
                    start = ep
                cur = end

        elif op in ('closePath', 'endPath'):
            # 閉じパスは最初の moveTo 点へ戻る線は既に処理済み
            pass

    return pts


# ═══════════════════════════════════════════════════════════════════════
# 座標変換・量子化
# ═══════════════════════════════════════════════════════════════════════

def transform_to_grid(
    pts    : list[tuple[float, float, bool]],
    ascent : float,
    descent: float,
) -> list[tuple[int, int, bool]]:
    """フォント座標 (TTF units) → SYMBOL.COD グリッド (X 0..DD-1, Y 0..DD)。

    TTF Y軸: 上が正（ascent > 0, descent < 0）
    SYMBOL Y軸: 上が正（同じ向き、反転不要）

    X: [0 .. advance_width] → [0 .. DD-1]
    Y: [descent .. ascent]  → [0 .. DD]
    """
    total_h = ascent - descent
    out: list[tuple[int, int, bool]] = []
    prev = None

    for x, y, pu in pts:
        gx = int(round(x / (ascent) * (DD - 1)))          # 0..DD-1
        gy = int(round((y - descent) / total_h * DD))      # 0..DD
        gx = max(-1, min(14, gx))
        gy = max(-2, min(13, gy))
        key = (gx, gy)
        if pu:
            out.append((gx, gy, True))
            prev = key
        else:
            if key != prev:
                out.append((gx, gy, False))
                prev = key

    return out


# ═══════════════════════════════════════════════════════════════════════
# IP エンコード
# ═══════════════════════════════════════════════════════════════════════

def encode_strokes(pts: list[tuple[int, int, bool]]) -> list[tuple[int, int]]:
    """グリッド座標列 → (pen_byte, data_byte) リスト。
    pen_byte : 0=描線(IPEN=2), 1=移動(IPEN=3)
    data_byte: bits[7:4] = x+1, bits[3:0] = y+2
    """
    out = []
    for x, y, pu in pts:
        x_nib = max(0, min(15, x + 1))
        y_nib = max(0, min(15, y + 2))
        out.append((1 if pu else 0, (x_nib << 4) | y_nib))
    return out


# ═══════════════════════════════════════════════════════════════════════
# メイン処理
# ═══════════════════════════════════════════════════════════════════════

def build_symbol_cod(
    ttf_path : str,
    output   : str  = DEFAULT_OUT,
    ttc_index: int  = 0,
    verbose  : bool = False,
) -> int:
    """TTF から SYMBOL.COD を生成して output に書き込む。

    Returns: 生成したファイルサイズ (bytes)
    """
    print(f'フォント読み込み中: {ttf_path}')
    font  = ttLib.TTFont(ttf_path, fontNumber=ttc_index)
    cmap  = font.getBestCmap()
    gs    = font.getGlyphSet()

    # フォントメトリクス取得
    upm   = font['head'].unitsPerEm
    try:
        ascent  = font['OS/2'].sTypoAscender
        descent = font['OS/2'].sTypoDescender
    except Exception:
        ascent  = font['hhea'].ascent
        descent = font['hhea'].descent
    print(f'  unitsPerEm={upm}, ascent={ascent}, descent={descent}')

    # ─── ビルド ───────────────────────────────────────────────────────────
    lookup    = bytearray(512)
    char_data = bytearray()
    found = missing = empty = 0

    for cp in range(256):
        # 現在の文字データ開始位置
        iadsym = 512 + len(char_data)
        lookup[cp * 2]     = (iadsym >> 8) & 0xFF
        lookup[cp * 2 + 1] =  iadsym       & 0xFF

        encoded: list[tuple[int, int]] = []

        if cp in ASCII_RANGE:
            if cp in cmap:
                gname = cmap[cp]
                if gname in gs:
                    pen = RecordingPen()
                    gs[gname].draw(pen)
                    raw   = recording_to_strokes(pen.value)
                    grid  = transform_to_grid(raw, ascent, descent)
                    encoded = encode_strokes(grid)
                    found += 1
                    if verbose:
                        print(f'  U+{cp:04X} {repr(chr(cp)):<4s} '
                              f'{gname:<20s} → {len(encoded):3d} pt')
                else:
                    missing += 1
            else:
                missing += 1
                if verbose:
                    print(f'  U+{cp:04X} {repr(chr(cp)):<4s} [cmap なし]')

        # 文字データブロック書き込み: [count][pen][data]...
        n = min(len(encoded), MAX_STROKES)
        char_data.append(n)
        for pen_b, dat_b in encoded[:n]:
            char_data.append(pen_b)
            char_data.append(dat_b)

    result = bytes(lookup) + bytes(char_data)
    with open(output, 'wb') as f:
        f.write(result)

    sz = len(result)
    print()
    print('=' * 58)
    print(f'  出力         : {output}')
    print(f'  ファイルサイズ : {sz:,} bytes')
    print(f'  ASCII 変換済み: {found} / {len(ASCII_RANGE)} 文字')
    if missing:
        print(f'  フォントに未収録: {missing} 文字')
    print(f'  BASIC 更新   : NKANF = {sz}')
    print('=' * 58)
    return sz


# ═══════════════════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description='TTF フォント → SYMBOL.COD (QB45 ベクタフォント)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
例:
  # Windows MS Gothic (msgothic.ttc の 0 番目)
  python make_symbol_cod_ttf.py --ttf "C:\\Windows\\Fonts\\msgothic.ttc"

  # Linux IPA Gothic
  python make_symbol_cod_ttf.py --ttf /usr/share/fonts/truetype/fonts-japanese-gothic.ttf

  # TTC の特定フォント番号を指定
  python make_symbol_cod_ttf.py --ttf msgothic.ttc --ttc-index 1
""")
    parser.add_argument('--ttf',       default=DEFAULT_TTF,
                        help=f'TTF/TTC ファイルパス (デフォルト: {DEFAULT_TTF})')
    parser.add_argument('--ttc-index', type=int, default=0,
                        help='TTC の場合のフォント番号 (デフォルト: 0)')
    parser.add_argument('--output',    default=DEFAULT_OUT,
                        help=f'出力ファイル名 (デフォルト: {DEFAULT_OUT})')
    parser.add_argument('--verbose', '-v', action='store_true',
                        help='各文字の処理詳細を表示')
    args = parser.parse_args()

    build_symbol_cod(
        ttf_path  = args.ttf,
        output    = args.output,
        ttc_index = args.ttc_index,
        verbose   = args.verbose,
    )


if __name__ == '__main__':
    main()