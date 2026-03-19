#!/usr/bin/env python3
"""
make_kandat.py
==============
MS Gothic TTC フォント → KANDAT.DAT + KANDAT2.DAT  (Shift-JIS 内符)

fontTools でグリフ輪郭を抽出し、TrueType 二次ベジェ曲線を折れ線近似して
KANDAT 形式の IP 値に変換する。

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
  Y = IP mod 100              整数  0..32 (下→上, TrueType Y 軸そのまま)

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
import numpy as np
from PIL import Image, ImageDraw
from fontTools.ttLib import TTFont
from fontTools.pens.basePen import AbstractPen

# ═══════════════════════════════════════════════════════════════════════
# 定数
# ═══════════════════════════════════════════════════════════════════════

FONT_PATH   = r'msgothic.ttc'
FONT_NUMBER = 2          # TTC 内インデックス: 0=MS Gothic, 1=MS PGothic, 2=MS UI Gothic
OUTPUT1     = 'KANDAT.DAT'
OUTPUT2     = 'KANDAT2.DAT'

REC_SIZE   = 32     # bytes per record (BASIC LEN=32)
INTS_PER   = 16     # int16 per record
DATA_INTS  = 15     # data words per record (word 15 = chain ptr)
GRID       = 32.0   # 座標グリッド 0..32
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
# ベジェ曲線サンプラー
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


# ═══════════════════════════════════════════════════════════════════════
# TrueType グリフ輪郭収集ペン
# ═══════════════════════════════════════════════════════════════════════

class CollectorPen(AbstractPen):
    """fontTools AbstractPen 実装。グリフ輪郭を (x, y, pen_up) 点列として収集する。

    pen_up=True  : 移動 (新ストローク / 輪郭開始)
    pen_up=False : 描線
    """

    def __init__(self):
        self.pts: list[tuple[float, float, bool]] = []
        self._cx = self._cy = 0.0

    def moveTo(self, pt):
        """輪郭開始点 (pen up)。"""
        self._cx, self._cy = pt[0], pt[1]
        self.pts.append((pt[0], pt[1], True))

    def lineTo(self, pt):
        """直線セグメント。"""
        self._cx, self._cy = pt[0], pt[1]
        self.pts.append((pt[0], pt[1], False))

    def qCurveTo(self, *points):
        """TrueType 二次ベジェ (B-スプライン) セグメント。

        fontTools の規約: points の最後の要素がオンカーブ、それ以前がオフカーブ。
        連続するオフカーブ点の間には暗黙のオンカーブ中点が存在する。
        """
        off_pts = list(points[:-1])
        on_pt   = points[-1]
        seg_start = (self._cx, self._cy)

        if not off_pts:
            # オフカーブなし → 直線扱い
            self.pts.append((on_pt[0], on_pt[1], False))
        else:
            for i, off in enumerate(off_pts):
                if i < len(off_pts) - 1:
                    # 連続オフカーブ → 暗黙オンカーブ中点を補間
                    next_off = off_pts[i + 1]
                    mid = ((off[0] + next_off[0]) / 2,
                           (off[1] + next_off[1]) / 2)
                    for px, py in _quadratic(seg_start, off, mid):
                        self.pts.append((px, py, False))
                    seg_start = mid
                else:
                    # 最後のオフカーブ + 終点オンカーブ
                    for px, py in _quadratic(seg_start, off, on_pt):
                        self.pts.append((px, py, False))

        self._cx, self._cy = on_pt[0], on_pt[1]

    def curveTo(self, *points):
        """3次ベジェセグメント (CFF/OTF フォント用, 念のため実装)。

        fontTools の規約: points = (ctrl1, ctrl2, ..., end_pt)
        ポリカーブの場合は 3点ずつに分割されて渡される。
        """
        pts = [(self._cx, self._cy)] + [(p[0], p[1]) for p in points]
        i = 0
        while i + 3 < len(pts):
            for px, py in _cubic(pts[i], pts[i+1], pts[i+2], pts[i+3]):
                self.pts.append((px, py, False))
            i += 3
        self._cx, self._cy = points[-1][0], points[-1][1]

    def closePath(self):
        """輪郭クローズ (TrueType は常にクローズド)。"""
        pass

    def endPath(self):
        """オープン輪郭終端 (通常未使用)。"""
        pass

    def addComponent(self, glyphName, transformation):
        """複合グリフ参照。glyph_set 経由で描画すると自動展開されるため不要。"""
        pass


# ═══════════════════════════════════════════════════════════════════════
# フォント読み込み & グリフ変換
# ═══════════════════════════════════════════════════════════════════════

def font_metrics(font) -> tuple[float, float, float]:
    """フォントメトリクスを返す。(em_size, ascender, descender)

    descender は負値 (例: -200)。
    OS/2 テーブルが利用可能な場合は sTypo 値を優先。
    """
    em = float(font['head'].unitsPerEm)
    try:
        asc = float(font['OS/2'].sTypoAscender)
        dsc = float(font['OS/2'].sTypoDescender)   # 負値
    except (AttributeError, KeyError):
        asc = float(font['hhea'].ascent)
        dsc = float(font['hhea'].descent)           # 負値
    # descender が 0 の場合はフォールバック
    if dsc >= 0:
        dsc = -em * 0.2
    return em, asc, dsc


def process_glyph(
    glyph_set,
    glyph_name: str,
    em: float,
    asc: float,
    dsc: float,
) -> list[int]:
    """TrueType グリフ → KANDAT IP 値リスト (塗りつぶし字形)。

    アルゴリズム:
      1. CollectorPen でグリフ輪郭点列を収集し輪郭ごとに分割
      2. 高解像度 (RENDER_SIZE×RENDER_SIZE) PIL イメージに各輪郭を
         XOR 塗りつぶし描画 → 偶奇塗りつぶし則を実現
         (TrueType の穴抜き文字「口・日・目」等に対応)
      3. 33×33 グリッドにダウンサンプルして塗りつぶし領域を確定
      4. 各グリッド行を走査し、塗りつぶし区間を水平ストロークに変換
         (行頭 pen_up → 行末 pen_down)

    座標系:
      TrueType: Y 上向き, 原点 = ベースライン左端
      PIL      : Y 下向き (上辺 = ascender, 下辺 = descender)
      KANDAT   : X 左→右 [0,32], Y 下→上 [0,32]
    """
    RENDER_SIZE = 128   # 中間レンダリング解像度
    GRID_N      = 33    # グリッド点数 (0..32)

    pen = CollectorPen()
    try:
        glyph_set[glyph_name].draw(pen)
    except Exception:
        return []

    if not pen.pts:
        return []

    y_range  = asc - dsc   # > 0
    y_offset = -dsc        # descender を 0 に (正値)

    # ── 1. 輪郭分割 & PIL 座標へ変換 ────────────────────────────────────
    # PIL Y: 上向きフォント座標を反転
    contours: list[list[tuple[float, float]]] = []
    current:  list[tuple[float, float]] = []

    for x, y, pu in pen.pts:
        xp = x / em * (RENDER_SIZE - 1)
        yp = (1.0 - (y + y_offset) / y_range) * (RENDER_SIZE - 1)
        if pu:
            if current:
                contours.append(current)
            current = [(xp, yp)]
        else:
            current.append((xp, yp))
    if current:
        contours.append(current)

    if not contours:
        return []

    # ── 2. 偶奇則塗りつぶし (輪郭ごとに XOR) ────────────────────────────
    # 外側輪郭と内側輪郭(穴)を正しく処理: 口・日・目 等の穴抜き文字に対応
    buf = np.zeros((RENDER_SIZE, RENDER_SIZE), dtype=np.uint8)
    for contour in contours:
        if len(contour) < 3:
            continue
        mask = Image.new('L', (RENDER_SIZE, RENDER_SIZE), 0)
        ImageDraw.Draw(mask).polygon(
            [(round(x), round(y)) for x, y in contour], fill=1
        )
        buf ^= np.array(mask, dtype=np.uint8)

    # ── 3. 33×33 グリッドへダウンサンプル ────────────────────────────────
    # 各グリッドセルに対応するブロックの過半数が塗られていれば塗りとみなす
    grid = np.zeros((GRID_N, GRID_N), dtype=bool)
    for gy in range(GRID_N):
        r0 = round(gy       / (GRID_N - 1) * (RENDER_SIZE - 1))
        r1 = round((gy + 1) / (GRID_N - 1) * (RENDER_SIZE - 1))
        r0, r1 = min(r0, RENDER_SIZE - 1), min(max(r1, r0 + 1), RENDER_SIZE)
        for gx in range(GRID_N):
            c0 = round(gx       / (GRID_N - 1) * (RENDER_SIZE - 1))
            c1 = round((gx + 1) / (GRID_N - 1) * (RENDER_SIZE - 1))
            c0, c1 = min(c0, RENDER_SIZE - 1), min(max(c1, c0 + 1), RENDER_SIZE)
            block = buf[r0:r1, c0:c1]
            grid[gy, gx] = block.sum() * 2 >= block.size

    # ── 4. 水平スキャンライン → セグメントリスト ────────────────────────
    # PIL row 0 = ascender 側 = KANDAT Y=32
    # PIL row 32 = descender 側 = KANDAT Y=0
    #
    # 行内に不連続領域がある字（口・日・間・算 等）は複数セグメントに分割し、
    # 穴の中を誤って塗り潰さないようにする。
    all_segs: list[tuple[int, int, int]] = []   # (y_kandat, x_start, x_end)

    for row in range(GRID_N):
        y_kandat = (GRID_N - 1) - row
        filled = np.where(grid[row])[0]
        if len(filled) == 0:
            continue

        # 連続ピクセル塊ごとにセグメントを生成
        seg_start = int(filled[0])
        prev      = int(filled[0])
        for idx in range(1, len(filled)):
            cur = int(filled[idx])
            if cur > prev + 1:          # 不連続 → セグメント確定
                all_segs.append((y_kandat, seg_start, prev))
                seg_start = cur
            prev = cur
        all_segs.append((y_kandat, seg_start, prev))

    if not all_segs:
        return []

    # ── 5. IP 上限 (119) に合わせて均等間引き ────────────────────────────
    # 単純な末尾切り捨てではなく均等サンプリングにより、
    # 字の上部・中部・下部を均等に保持する。
    MAX_IPS  = MAX_RECS * DATA_INTS - 1   # 119
    max_segs = MAX_IPS // 2               # 各セグメント = IP 2 個

    if len(all_segs) > max_segs:
        step     = len(all_segs) / max_segs
        all_segs = [all_segs[round(i * step)] for i in range(max_segs)]

    # ── 6. IP 値エンコード ────────────────────────────────────────────────
    ips: list[int] = []
    for y_kandat, x_start, x_end in all_segs:
        ips.append(2 * 10000 + x_start * 100 + y_kandat)  # pen up (移動)
        ips.append(1 * 10000 + x_end   * 100 + y_kandat)  # pen down (描線)

    return ips


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
    font_path  : str,
    font_number: int,
    max_primary: int,
    verbose    : bool,
    label      : str,
) -> bytes:
    """KANDAT バイナリデータを生成して返す。

    Args:
        xycode_map  : {slot_index: unicode_cp}
        font_path   : TTC/TTF フォントパス
        font_number : TTC 内フォントインデックス
        max_primary : プライマリスロット最大値
        verbose     : 詳細ログ表示
        label       : ログ用ラベル

    Returns:
        bytes: KANDAT ファイルバイナリ
    """
    # ── フォント読み込み ──────────────────────────────────────────────────
    print(f'\n{"═"*60}')
    print(f'  [{label}]  プライマリスロット上限: {max_primary}')
    print(f'  フォント読み込み中: {font_path} [#{font_number}] ...')
    font      = TTFont(font_path, fontNumber=font_number)
    em, asc, dsc = font_metrics(font)
    cmap      = font.getBestCmap() or {}
    glyph_set = font.getGlyphSet()
    print(f'  em={em:.0f}  ascender={asc:.0f}  descender={dsc:.0f}  '
          f'グリフ数={len(glyph_set)}')
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

        glyph_name = cmap.get(ucp)
        if glyph_name and glyph_name in glyph_set:
            ips = process_glyph(glyph_set, glyph_name, em, asc, dsc)
            found += 1
            if verbose:
                try:    ch = chr(ucp)
                except  ValueError: ch = '?'
                print(f'  slot {slot:4d}  U+{ucp:04X} {ch}  '
                      f'{glyph_name:20s}  {len(ips):3d}pt')
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
            flat[slot] = records[0]
        else:
            cont_count = len(records) - 1
            cont_slots = list(range(next_extra, next_extra + cont_count))
            next_extra += cont_count
            all_slots  = [slot] + cont_slots

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
    print(f'  グリフなし  : {missing:5d}  (Unicode マップなし: {empty})')
    print(f'  継続レコード: {n_cont:5d}')
    print(f'  最大スロット: {max_slot:5d}')
    print(f'  ファイルサイズ: {len(buf):,} bytes ({len(buf)//1024} KB)')
    return bytes(buf)


# ═══════════════════════════════════════════════════════════════════════
# エントリーポイント
# ═══════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description='MS Gothic TTC フォント → KANDAT.DAT + KANDAT2.DAT'
    )
    parser.add_argument(
        '--font', default=FONT_PATH,
        help=f'TTC/TTF フォントパス (デフォルト: {FONT_PATH})'
    )
    parser.add_argument(
        '--font-number', type=int, default=FONT_NUMBER,
        help=f'TTC 内フォントインデックス (デフォルト: {FONT_NUMBER}  '
             f'0=MS Gothic / 1=MS PGothic / 2=MS UI Gothic)'
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

    print('MS Gothic TTC → KANDAT ジェネレーター')
    print(f'  フォント        : {args.font} [#{args.font_number}]')
    print(f'  KANDAT.DAT  出力 : {args.out1}')
    print(f'  KANDAT2.DAT 出力 : {args.out2}')

    # ── Shift-JIS → XYCODE → Unicode マップを構築 ────────────────────────
    print('\nShift-JIS コード空間をスキャン中...')
    map1, map2 = build_xycode_maps()
    print(f'  KANDAT.DAT  マップ: {len(map1)} エントリ')
    print(f'  KANDAT2.DAT マップ: {len(map2)} エントリ')

    # ── KANDAT.DAT 生成 ───────────────────────────────────────────────────
    dat1 = build_kandat_bytes(
        map1, args.font, args.font_number, MAX_SLOT1, args.verbose, 'KANDAT.DAT'
    )
    with open(args.out1, 'wb') as f:
        f.write(dat1)
    print(f'\n  → {args.out1} 書き込み完了')

    # ── KANDAT2.DAT 生成 ──────────────────────────────────────────────────
    dat2 = build_kandat_bytes(
        map2, args.font, args.font_number, MAX_SLOT2, args.verbose, 'KANDAT2.DAT'
    )
    with open(args.out2, 'wb') as f:
        f.write(dat2)
    print(f'  → {args.out2} 書き込み完了')

    print('\n完了。')


if __name__ == '__main__':
    main()