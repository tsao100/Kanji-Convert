#!/usr/bin/env python3
"""
shx2symbol.py
=============
AutoCAD unifont SHX フォントファイル → SYMBOL.COD  (QB45 BASIC 用ベクタフォント)

参考: ttf2symbol.py（同ディレクトリ）
SHX opcode 仕様: shx2kandat.py（同ディレクトリ）

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
AutoCAD unifont SHX バイナリフォーマット
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  ファイル先頭:
    "AutoCAD-86 unifont 1.0\r\n\x1A"
  その後:
    u16 LE  : nshapes (エントリ総数)
    u16 LE  : shape0_defbytes (フォントメトリクスブロックのバイト数)
    str+\x00: フォント名（null終端）
    shape0データ: above(1B), below(1B), modes(1B), ... \x00終端
  エントリ列 (0x4F 付近から):
    [unicode: u16 LE][dlen: u16 LE][opcodes: dlen bytes]  × nshapes

SHX opcodes (unifont 版):
  0x00  NOP（データ終端でもあるが、中間に現れた場合は無視）
  0x01  ペンダウン（描画モード ON）
  0x02  ペンアップ（描画モード OFF）
  0x03  スケール除算: 次の1バイトで sc /= v
  0x04  スケール乗算: 次の1バイトで sc *= v
  0x05  位置スタックPUSH（x, y, sc, draw_on を保存）
  0x06  位置スタックPOP
  0x07  サブシェイプ呼び出し（simplex.shx では未使用）
  0x08  相対移動/描画: 次の2バイト signed dx, dy (× sc)
  0x09  複数XYペア列: (dx, dy) ペアを (0, 0) まで繰り返す
  0x0A  八分円弧: [radius: 1B][octants: 1B]
          octants 上位4bit = 開始八分円(0-7), 下位4bit = スパン八分円数(0=8周)
  0x0B  分数弧: 5バイト（このフォントでは未使用）
  0x0C  バルジ弧: dx, dy, bulge（このフォントでは未使用）
  0x0D  ポリライン+バルジ（このフォントでは未使用）
  0x0E  次の命令をスキップ（水平モード時）
          ※ BigFont の "skip next instruction" と同じ意味
          ※ 0x0E 0x03 v → scale-divide を垂直専用にする
          ※ 0x0E 0x04 v → scale-multiply を垂直専用にする
          ※ 0x0E 0x08 dx dy → 絶対開始位置指定を垂直専用にする
  0x10+ ベクタバイト: 上位4bit = 長さ L (1-15), 下位4bit = 方向 N (0-15)
          dx = DIR16[N].x × L × sc,  dy = DIR16[N].y × L × sc

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SYMBOL.COD バイナリフォーマット（BASIC SYMBO2/READCODE より逆算）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Bytes   0–511 : Lookup Table (256 char × 2 bytes, big-endian)
                  lookup[c*2..c*2+1] = IADSYM (文字データの 0-based 位置)
  Bytes 512+    : 文字データブロック
                  [count:1B][pen₁:1B][data₁:1B][pen₂:1B][data₂:1B]...
                  pen  byte: 0 = 描線(IPEN=2), 1 = 移動(IPEN=3/pen-up)
                  data byte: 高4bit = X nibble (XSYM = nibble-1, -1..14)
                             低4bit = Y nibble (YSYM = nibble-2, -2..13)

座標グリッド: X 0..DD-1, Y 0..DD  (SYMBO2 DD=12)
  TTF版と同じグリッドを使用
"""
from __future__ import annotations

import os
import math
import argparse
import struct

# ═══════════════════════════════════════════════════════════════════════
# 定数
# ═══════════════════════════════════════════════════════════════════════

DD           = 12        # SYMBO2 グリッド幅 (X: 0..DD-1, Y: 0..DD)
MAX_STROKES  = 127       # 1文字あたり最大ストローク数
ARC_SEGS     = 4         # 八分円弧1区間あたりのサンプル点数

DEFAULT_SHX  = 'simplex.shx'
DEFAULT_OUT  = 'SYMBOL.COD'

# 対象 ASCII コードポイント範囲
ASCII_RANGE  = range(0x20, 0x7F)   # 0x20 (space) .. 0x7E (~)

# 16方向ベクタ（DIR16）: (dx_unit, dy_unit)
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


# ═══════════════════════════════════════════════════════════════════════
# SHX unifont パーサ
# ═══════════════════════════════════════════════════════════════════════

class ShxUnifont:
    """AutoCAD SHX unifont バイナリパーサ。

    フォーマット:
        "AutoCAD-86 unifont 1.0\\r\\n\\x1A"
        u16 LE: nshapes
        u16 LE: shape0_defbytes  (shape0 = フォントメトリクス)
        str+\\x00: フォント名
        shape0 opcodes: above(1B), below(1B), modes(1B), ..., \\x00
        以降: [unicode u16 LE][dlen u16 LE][opcodes dlen bytes] × nshapes
    """

    def __init__(self, path: str):
        self.path      = path
        self.above     = 21
        self.below     = 7
        self.advance   = 28
        self.entries   : dict[int, bytes] = {}   # unicode → opdata bytes
        self._parse()

    # ── パース ────────────────────────────────────────────────────────
    def _parse(self) -> None:
        with open(self.path, 'rb') as f:
            raw = f.read()

        # 0x1A マーカーを探す
        pos_1a = raw.find(0x1A)
        if pos_1a < 0:
            print(f'  [警告] {self.path}: 0x1A マーカーが見つかりません')
            return

        pos = pos_1a + 1

        # ── ヘッダ構造 ────────────────────────────────────────────────
        # AutoCAD unifont SHX ヘッダ（0x1A の直後）:
        #   u32 LE : nshapes       (エントリ総数)
        #   u16 LE : shape0_defbytes (shape0 ブロックのバイト数, name+opcodes)
        #   str+\0 : フォント名 (null終端)
        #   opcodes: shape0 データ (above, below, modes, ...)
        #
        # 実測: simplex.shx
        #   0x19-0x1C : 4F 01 00 00 → nshapes=335
        #   0x1D-0x1E : 30 00       → defbytes=48
        #   0x1F-0x48 : "SIMPLEX  Copyright 1996 by Autodesk, Inc.\x00"
        #   0x49-0x4E : 15 07 02 00 00 00  (above=21, below=7)
        #   0x4F~     : 0A 00 0A 00 ...    (U+000A エントリ列)
        # ─────────────────────────────────────────────────────────────

        if pos + 4 > len(raw): return
        nshapes = struct.unpack_from('<I', raw, pos)[0]; pos += 4   # u32 LE

        if pos + 2 > len(raw): return
        shape0_db = struct.unpack_from('<H', raw, pos)[0]; pos += 2  # u16 LE

        # ── shape0 ブロック ───────────────────────────────────────────
        # shape0_db バイト数のブロック = フォント名 (null終端) + metrics opcodes
        # ブロック開始位置を記録し、ブロック末尾でエントリ列に入る
        #
        # 実測 simplex.shx:
        #   nshapes=335 (u32), shape0_db=48 (u16)
        #   shape0 block (48B): "SIMPLEX  Copyright 1996 by Autodesk, Inc.\x00"
        #                       + 15 07 02 00 00 00  (above=21, below=7)
        #   → entries_start = pos_after_shape0_db_field + 48 = 0x4F
        shape0_start   = pos              # shape0 ブロック先頭
        entries_start  = shape0_start + shape0_db  # エントリ列先頭

        # フォント名の null 終端を探してメトリクス位置を特定
        null_pos = raw.find(0, shape0_start, entries_start)
        if null_pos >= 0:
            metrics_pos = null_pos + 1
            if metrics_pos < len(raw):
                self.above = raw[metrics_pos]
            if metrics_pos + 1 < len(raw):
                self.below = raw[metrics_pos + 1]
        self.advance = (self.above + self.below) or 28

        # ── エントリ列: [unicode u16 LE][dlen u16 LE][opcodes dlen bytes] ──
        pos   = entries_start
        size  = len(raw)
        count = 0
        while pos + 4 <= size and count < nshapes:
            ucp  = struct.unpack_from('<H', raw, pos)[0]
            dlen = struct.unpack_from('<H', raw, pos + 2)[0]
            pos += 4
            if dlen == 0 or pos + dlen > size:
                break
            self.entries[ucp] = raw[pos : pos + dlen]
            pos   += dlen
            count += 1

    def get_opdata(self, ucp: int) -> bytes:
        return self.entries.get(ucp, b'')


# ═══════════════════════════════════════════════════════════════════════
# SHX opcode レンダラ
# ═══════════════════════════════════════════════════════════════════════

def _skip_next_instruction(opdata: bytes, i: int) -> int:
    """0x0E に続く次の命令（＋全パラメータ）を読み飛ばして新 i を返す。"""
    n = len(opdata)
    if i >= n:
        return i
    nxt = opdata[i]; i += 1
    if nxt in (0x00, 0x01, 0x02, 0x05, 0x06, 0x0F):
        return i                  # 1バイト命令
    elif nxt in (0x03, 0x04, 0x07):
        return min(i + 1, n)      # opcode + 1 param
    elif nxt == 0x08:
        return min(i + 2, n)      # opcode + dx + dy
    elif nxt == 0x09:
        # (dx,dy) ペア列を (0,0) まで読み飛ばす
        while i + 1 < n:
            if opdata[i] == 0 and opdata[i + 1] == 0:
                return i + 2
            i += 2
        return i
    elif nxt == 0x0A:
        return min(i + 2, n)      # opcode + radius + octants
    elif nxt == 0x0B:
        return min(i + 5, n)      # opcode + 5 bytes
    elif nxt == 0x0C:
        return min(i + 3, n)      # opcode + dx + dy + bulge
    elif nxt == 0x0D:
        # (dx, dy, bulge) トリプレット列を (0,0,...) まで
        while i + 2 < n:
            if opdata[i] == 0 and opdata[i + 1] == 0:
                return i + 3
            i += 3
        return i
    elif nxt == 0x0E:
        return i                  # double-0E = NOP
    elif nxt >= 0x10:
        return i                  # ベクタバイトは既に消費済み (1バイト合計)
    return i


def _arc_points(cx: float, cy: float, r: float,
                a0: float, span: float,
                segs_per_oct: int = ARC_SEGS) -> list[tuple[float, float]]:
    """円弧を折れ線近似して点列を返す（終点含む・始点除く）。"""
    n_oct = abs(span) / (math.pi / 4)
    n = max(round(n_oct) * segs_per_oct, 1)
    return [
        (cx + r * math.cos(a0 + k / n * span),
         cy + r * math.sin(a0 + k / n * span))
        for k in range(1, n + 1)
    ]


def render_shx_glyph(
    ucp  : int,
    font : ShxUnifont,
    depth: int = 0,
) -> list[tuple[float, float, bool]]:
    """SHX unifont の1グリフを描画し (x, y, pen_up) 点列を返す。

    pen_up=True  : 移動（新ストローク開始）
    pen_up=False : 描線

    座標系: SHX 設計座標 (0,0) = ベースライン左端
      X: 右が正 (0 → advance)
      Y: 上が正 (-below → above)

    対応 opcode:
      0x00 NOP, 0x01 pen_down, 0x02 pen_up,
      0x03 sc /= v,  0x04 sc *= v,
      0x05 push, 0x06 pop,
      0x08 REL dx dy,  0x09 XY_SEQ,
      0x0A ARC_OCTANT,  0x0E SKIP_NEXT,
      0x10+ VECTOR_BYTE
    """
    if depth > 8:
        return []

    opdata = font.get_opdata(ucp)
    if not opdata:
        return []

    pts     : list[tuple[float, float, bool]] = []
    stack   : list[tuple[float, float, float, bool]] = []
    x       = 0.0
    y       = 0.0
    sc      = 1.0
    draw_on = False
    pen_up  = True      # 現在ペンが上がっているか

    def _emit(nx: float, ny: float) -> None:
        nonlocal x, y, pen_up
        if draw_on:
            if pen_up:
                pts.append((x, y, True))   # 移動点（新ストローク開始）
                pen_up = False
            pts.append((nx, ny, False))    # 描線点
        else:
            # ペンアップ中: 移動記録だけ（後で draw に使う）
            x = nx; y = ny
            pen_up = True
            return
        x = nx; y = ny

    i = 0
    n = len(opdata)

    while i < n:
        op = opdata[i]; i += 1

        # ── 制御 ─────────────────────────────────────────────────────
        if op == 0x00:
            pass                             # NOP

        elif op == 0x01:
            draw_on = True

        elif op == 0x02:
            draw_on = False
            pen_up  = True

        elif op == 0x03:
            if i >= n: break
            v = opdata[i]; i += 1
            if v:
                sc /= v

        elif op == 0x04:
            if i >= n: break
            sc *= opdata[i]; i += 1

        elif op == 0x05:
            stack.append((x, y, sc, draw_on))

        elif op == 0x06:
            if stack:
                x, y, sc, draw_on = stack.pop()

        elif op == 0x07:
            # サブシェイプ呼び出し (simplex.shx では未使用だが念のため)
            # 1バイトのシェイプインデックスを読み飛ばす
            if i < n: i += 1

        # ── 移動/描画 ──────────────────────────────────────────────────
        elif op == 0x08:
            # 相対移動/描画: signed dx, signed dy
            if i + 2 > n: break
            dx = struct.unpack_from('b', opdata, i)[0]; i += 1
            dy = struct.unpack_from('b', opdata, i)[0]; i += 1
            _emit(x + dx * sc, y + dy * sc)

        elif op == 0x09:
            # 複数 XY ペア列: (dx, dy) を (0,0) まで繰り返す
            while i + 1 < n:
                dx = struct.unpack_from('b', opdata, i)[0]
                dy = struct.unpack_from('b', opdata, i + 1)[0]
                i += 2
                if dx == 0 and dy == 0:
                    break
                _emit(x + dx * sc, y + dy * sc)

        # ── 弧線 ───────────────────────────────────────────────────────
        elif op == 0x0A:
            # 八分円弧: [radius: 1B][octants: 1B]
            #   octants 上位4bit = 開始八分円 (0-7)
            #   octants 下位4bit = スパン八分円数 (0 → 8 = 全周)
            if i + 2 > n: break
            r        = opdata[i] * sc;  i += 1
            oct_byte = opdata[i];       i += 1
            a0_oct   = (oct_byte >> 4) & 0x07
            span_oct =  oct_byte       & 0x0F
            if span_oct == 0:
                span_oct = 8            # 0 = 8 八分円 = 全周

            a0   = a0_oct * math.pi / 4
            span = span_oct * math.pi / 4
            # 現在位置が弧の始点 → 中心を逆算
            cx = x - r * math.cos(a0)
            cy = y - r * math.sin(a0)

            for px, py in _arc_points(cx, cy, r, a0, span):
                _emit(px, py)

        elif op == 0x0B:
            # 分数弧 (5 bytes) — simplex.shx では未使用
            i = min(i + 5, n)

        elif op == 0x0C:
            # バルジ弧 (3 bytes) — simplex.shx では未使用
            i = min(i + 3, n)

        elif op == 0x0D:
            # ポリライン+バルジ — simplex.shx では未使用
            while i + 2 < n:
                if opdata[i] == 0 and opdata[i + 1] == 0:
                    i += 3; break
                i += 3

        # ── 条件スキップ ────────────────────────────────────────────────
        elif op == 0x0E:
            # 水平モードでは次の命令（＋全パラメータ）をスキップ
            i = _skip_next_instruction(opdata, i)

        elif op == 0x0F:
            pass                             # 保留 / NOP

        # ── ベクタバイト (0x10–0xFF) ────────────────────────────────────
        elif op >= 0x10:
            L    = (op >> 4) & 0x0F
            N    =  op       & 0x0F
            dx_u, dy_u = _DIR16[N]
            _emit(x + dx_u * L * sc, y + dy_u * L * sc)

    return pts


# ═══════════════════════════════════════════════════════════════════════
# 座標変換・量子化
# ═══════════════════════════════════════════════════════════════════════

def transform_to_grid(
    pts    : list[tuple[float, float, bool]],
    above  : int,
    below  : int,
    advance: int,
) -> list[tuple[int, int, bool]]:
    """SHX 設計座標 → SYMBOL.COD グリッド (X -1..14, Y -2..13)。

    SHX 座標系:
      X: 0 → advance  (左端 → 右端)
      Y: -below → above  (ベースライン下 → 上)  ※上が正

    SYMBOL グリッド (ttf2symbol.py と同仕様):
      X: gx = round(x / advance * (DD-1))       → -1..14 にクランプ
      Y: gy = round((y + below) / total_h * DD) → -2..13 にクランプ
    """
    total_h = above + below
    if total_h <= 0 or advance <= 0:
        return []

    out   : list[tuple[int, int, bool]] = []
    prev  : tuple[int, int] | None = None

    for x, y, pu in pts:
        gx = int(round(x / advance * (DD - 1)))
        gy = int(round((y + below) / total_h * DD))
        gx = max(-1, min(14, gx))
        gy = max(-2, min(13, gy))

        key = (gx, gy)
        if pu:
            # ペンアップ: 始点として記録（直後に draw 点がある場合のみ有効）
            out.append((gx, gy, True))
            prev = None
        else:
            # 重複点は除去
            if key != prev:
                out.append((gx, gy, False))
                prev = key

    # ペンアップ点の直後に draw 点がない場合は除去
    clean : list[tuple[int, int, bool]] = []
    ng = len(out)
    for k in range(ng):
        gx, gy, pu = out[k]
        if pu:
            if k + 1 < ng and not out[k + 1][2]:
                clean.append((gx, gy, True))
        else:
            clean.append((gx, gy, False))

    return clean


# ═══════════════════════════════════════════════════════════════════════
# IP エンコード (ttf2symbol.py と同仕様)
# ═══════════════════════════════════════════════════════════════════════

def encode_strokes(pts: list[tuple[int, int, bool]]) -> list[tuple[int, int]]:
    """グリッド座標列 → (pen_byte, data_byte) リスト。

    pen_byte : 0 = 描線 (IPEN=2),  1 = 移動 (IPEN=3)
    data_byte: bits[7:4] = x + 1,  bits[3:0] = y + 2
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
    shx_path: str,
    output  : str  = DEFAULT_OUT,
    verbose : bool = False,
) -> int:
    """SHX から SYMBOL.COD を生成して output に書き込む。

    Returns: 生成したファイルサイズ (bytes)
    """
    print(f'SHX フォント読み込み中: {shx_path}')
    font = ShxUnifont(shx_path)
    print(f'  エントリ数: {len(font.entries)}')
    print(f'  above={font.above}, below={font.below}, advance={font.advance}')
    print(f'  Unicode 範囲: '
          f'{min(font.entries):04X}h – {max(font.entries):04X}h'
          if font.entries else '  (エントリなし)')

    # ─── ASCII 範囲のビルド ───────────────────────────────────────────
    lookup    = bytearray(512)
    char_data = bytearray()
    found = missing = 0

    for cp in range(256):
        # 現在の文字データ開始位置
        iadsym = 512 + len(char_data)
        lookup[cp * 2]     = (iadsym >> 8) & 0xFF
        lookup[cp * 2 + 1] =  iadsym       & 0xFF

        encoded: list[tuple[int, int]] = []

        if cp in ASCII_RANGE and cp in font.entries:
            # SHX → 設計座標点列
            raw_pts = render_shx_glyph(cp, font)

            if raw_pts:
                # → グリッド座標
                grid = transform_to_grid(
                    raw_pts, font.above, font.below, font.advance)
                # → (pen, data) バイトペア
                encoded = encode_strokes(grid)
                found += 1
                if verbose:
                    drawn = sum(1 for _, _, pu in grid if not pu)
                    try:   ch = chr(cp)
                    except ValueError: ch = '?'
                    print(f'  U+{cp:04X} {repr(ch):<6s} → '
                          f'{len(grid):3d} pt ({drawn} drawn)')
            else:
                missing += 1
                if verbose:
                    print(f'  U+{cp:04X} {repr(chr(cp)):<6s} [点なし]')
        elif cp in ASCII_RANGE:
            missing += 1
            if verbose:
                print(f'  U+{cp:04X} {repr(chr(cp)):<6s} [SHX 未収録]')

        # 文字データブロック書き込み: [count][pen][data]...
        n_pts = min(len(encoded), MAX_STROKES)
        char_data.append(n_pts)
        for pen_b, dat_b in encoded[:n_pts]:
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
        print(f'  未描画/未収録 : {missing} 文字')
    print(f'  BASIC 更新   : NKANF = {sz}')
    print('=' * 58)
    return sz


# ═══════════════════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(
        description='AutoCAD SHX unifont → SYMBOL.COD (QB45 ベクタフォント)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
例:
  # simplex.shx から SYMBOL.COD を生成
  python shx2symbol.py --shx simplex.shx

  # 詳細表示
  python shx2symbol.py --shx simplex.shx --verbose

  # 出力ファイル名を変更
  python shx2symbol.py --shx romans.shx --output SYMBOL_ROMANS.COD

  # 動作確認 (文字一覧表示のみ)
  python shx2symbol.py --shx simplex.shx --dump
""")
    parser.add_argument('--shx',     default=DEFAULT_SHX,
                        help=f'SHX ファイルパス (デフォルト: {DEFAULT_SHX})')
    parser.add_argument('--output',  default=DEFAULT_OUT,
                        help=f'出力ファイル名 (デフォルト: {DEFAULT_OUT})')
    parser.add_argument('--verbose', '-v', action='store_true',
                        help='各文字の処理詳細を表示')
    parser.add_argument('--dump',    action='store_true',
                        help='SHX の全エントリを表示してレンダリング確認のみ行う')
    args = parser.parse_args()

    if not os.path.isfile(args.shx):
        print(f'[エラー] SHX ファイルが見つかりません: {args.shx}')
        return

    font = ShxUnifont(args.shx)
    print(f'SHX 読み込み完了: {args.shx}')
    print(f'  エントリ数={len(font.entries)}, '
          f'above={font.above}, below={font.below}, advance={font.advance}')

    if args.dump:
        print()
        print(f'{"Unicode":<8s} {"Char":<6s} {"pts":>5s} {"drawn":>6s}')
        print('-' * 28)
        for ucp in sorted(font.entries.keys()):
            try:   ch = chr(ucp)
            except ValueError: ch = '?'
            raw_pts = render_shx_glyph(ucp, font)
            grid    = transform_to_grid(
                raw_pts, font.above, font.below, font.advance)
            drawn   = sum(1 for _, _, pu in grid if not pu)
            print(f'U+{ucp:04X}  {repr(ch):<6s} {len(grid):5d} {drawn:6d}')
        return

    build_symbol_cod(
        shx_path = args.shx,
        output   = args.output,
        verbose  = args.verbose,
    )


if __name__ == '__main__':
    main()