#!/usr/bin/env python3
"""
ttf2kandat.py
==============
MS Gothic TTC 字型 → KANDAT.DAT + KANDAT2.DAT  (Shift-JIS 內碼)

以 fontTools 擷取字符輪廓，將 TrueType 二次貝茲曲線以折線近似，
再轉換為 KANDAT 格式的 IP 值。

從 BASIC 程式 KANJI2 副程式逆向推導：

┌─────────────────────────────────────────────────────────────────────┐
│  KANDAT*.DAT 格式  (BASIC RANDOM 檔案, LEN=32)                      │
│                                                                     │
│  Record  1 .. 8   : 標頭保留區 (填零, 32 bytes × 8 = 256 B)         │
│  Record  XYCODE+8 : 字符資料                                         │
│    Word  0..14  (int16 × 15) : 筆畫資料 (IP 值)                     │
│    Word  15     (int16 × 1 ) : 鏈結指標                              │
│                                (下一個記錄的槽號, 0=結束)            │
└─────────────────────────────────────────────────────────────────────┘

IP 值編碼：
  IP = pen_flag × 10000 + X × 100 + Y
  IPEN = INT(IP / 10000) + 1
    pen_flag = 1 → IPEN = 2 → 畫線 (draw)
    pen_flag = 2 → IPEN = 3 → 移動 (pen up / 新筆畫)
  X = (IP mod 10000) / 100   整數  0..32 (左→右)
  Y = IP mod 100              整數  0..32 (下→上, TrueType Y 軸方向不變)

KANDAT 檔案配置 (XYCODE ≤ 4000)：
  非漢字 (KTYPE 1)  槽號    1 ..  453
  第一水準漢字 (KTYPE 2)  槽號  454 .. 3461
  特殊   (KTYPE 4)  槽號 3519 .. 3693
  → 最大槽號 = 3693

KANDAT2 檔案配置 (XYCODE > 4000, 儲存為 XYCODE-4000)：
  第二水準漢字 (KTYPE 3)  槽號    1 .. 3572
"""

from __future__ import annotations
import os, re, math, struct, argparse
import numpy as np
from PIL import Image, ImageDraw
from fontTools.ttLib import TTFont
from fontTools.pens.basePen import AbstractPen

# ═══════════════════════════════════════════════════════════════════════
# 常數
# ═══════════════════════════════════════════════════════════════════════

FONT_PATH   = r'chogokubosogothic_5.ttf'
FONT_NUMBER = 0          # TTC 內索引：0=MS Gothic, 1=MS PGothic, 2=MS UI Gothic
OUTPUT1     = 'KANDAT.DAT'
OUTPUT2     = 'KANDAT2.DAT'

REC_SIZE   = 32     # 每筆記錄位元組數 (BASIC LEN=32)
INTS_PER   = 16     # 每筆記錄的 int16 數
DATA_INTS  = 15     # 每筆記錄的資料字組數 (第 15 字組為鏈結指標)
GRID       = 32.0   # 座標格線 0..32
MAX_RECS   = 8      # 最大鏈結記錄數 (BASIC: NR < 8)

MAX_SLOT1  = 3693   # KANDAT.DAT  主要槽號上限
MAX_SLOT2  = 3572   # KANDAT2.DAT 主要槽號上限

# BASIC ZKC 非漢字 JIS 範圍對
ZKC = [
    0x2120, 0x217F,   # 符號・標點
    0x2220, 0x222F,   # 特殊符號
    0x232F, 0x233A,   # 全形數字 0-9
    0x2340, 0x235B,   # 全形大寫 A-Z
    0x2360, 0x237B,   # 全形小寫 a-z
    0x2420, 0x2474,   # 平假名
    0x2520, 0x2577,   # 片假名
    0x2620, 0x2639,   # 希臘大寫
    0x2640, 0x2659,   # 希臘小寫
    0x2720, 0x2742,   # 西里爾大寫
    0x2750, 0x2772,   # 西里爾小寫
]

# ═══════════════════════════════════════════════════════════════════════
# JIS / Shift-JIS / Unicode 轉換
# ═══════════════════════════════════════════════════════════════════════

def mstojis(mscode: int) -> int:
    """Shift-JIS (MS 編碼) → JIS X 0208。完整重現 BASIC MSTOJIS 函式。"""
    il = mscode & 0xFF
    ih = (mscode >> 8) & 0xFF
    # 第1位元組轉換
    if ih <= 159:
        ihh = 2 * (ih - 129) + 33
    else:
        ihh = 2 * (ih - 224) + 95
    if il >= 159:
        ihh += 1
    # 第2位元組轉換
    if   64  <= il <= 126: ill = il - 31
    elif 128 <= il <= 158: ill = il - 32
    elif 159 <= il <= 252: ill = il - 126
    else:
        return 0
    return (ihh << 8) | ill


def jistoxy(jiscode: int) -> int:
    """JIS X 0208 → XYCODE (KANDAT 槽號)。完整重現 BASIC JISTOXY 函式。"""
    hcd = (jiscode >> 8) & 0xFF
    lcd = jiscode & 0xFF

    # 依優先順序判斷 KTYPE（後者優先，範圍不重疊）
    ktype = 0
    if 0x2120 < jiscode < 0x277E: ktype = 1
    if 0x30   <= hcd    <= 0x4F:  ktype = 2
    if 0x50   <= hcd    <= 0x75:  ktype = 3
    if 0x7620 < jiscode < 0x76D0: ktype = 4

    if ktype == 1:
        # 非漢字：依序掃描 ZKC 範圍對
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
        # 附加漢字
        return jiscode - 0x7620 + 3518

    return 0


def build_xycode_maps() -> tuple[dict, dict]:
    """掃描 Shift-JIS 雙位元組空間，建立 XYCODE → Unicode 對照表。

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
# 貝茲曲線取樣器
# ═══════════════════════════════════════════════════════════════════════

def _cubic(p0, p1, p2, p3, n=8):
    """對三次貝茲曲線取 n 個樣本點（含終點）。"""
    for k in range(1, n + 1):
        t = k / n; m = 1 - t
        yield (m**3*p0[0]+3*m**2*t*p1[0]+3*m*t**2*p2[0]+t**3*p3[0],
               m**3*p0[1]+3*m**2*t*p1[1]+3*m*t**2*p2[1]+t**3*p3[1])


def _quadratic(p0, p1, p2, n=6):
    """對二次貝茲曲線取 n 個樣本點（含終點）。"""
    for k in range(1, n + 1):
        t = k / n; m = 1 - t
        yield (m**2*p0[0]+2*m*t*p1[0]+t**2*p2[0],
               m**2*p0[1]+2*m*t*p1[1]+t**2*p2[1])


# ═══════════════════════════════════════════════════════════════════════
# TrueType 字符輪廓收集筆
# ═══════════════════════════════════════════════════════════════════════

class CollectorPen(AbstractPen):
    """fontTools AbstractPen 實作。將字符輪廓收集為 (x, y, pen_up) 點列。

    pen_up=True  : 移動（新筆畫 / 輪廓起始）
    pen_up=False : 畫線
    """

    def __init__(self):
        self.pts: list[tuple[float, float, bool]] = []
        self._cx = self._cy = 0.0

    def moveTo(self, pt):
        """輪廓起始點（pen up）。"""
        self._cx, self._cy = pt[0], pt[1]
        self.pts.append((pt[0], pt[1], True))

    def lineTo(self, pt):
        """直線段。"""
        self._cx, self._cy = pt[0], pt[1]
        self.pts.append((pt[0], pt[1], False))

    def qCurveTo(self, *points):
        """TrueType 二次貝茲（B-Spline）線段。

        fontTools 慣例：points 最後一個元素為曲線上控制點，其餘為曲線外控制點。
        連續曲線外控制點之間存在隱含的曲線上中間點。
        """
        off_pts = list(points[:-1])
        on_pt   = points[-1]
        seg_start = (self._cx, self._cy)

        if not off_pts:
            # 無曲線外控制點 → 視為直線
            self.pts.append((on_pt[0], on_pt[1], False))
        else:
            for i, off in enumerate(off_pts):
                if i < len(off_pts) - 1:
                    # 連續曲線外控制點 → 補插隱含曲線上中間點
                    next_off = off_pts[i + 1]
                    mid = ((off[0] + next_off[0]) / 2,
                           (off[1] + next_off[1]) / 2)
                    for px, py in _quadratic(seg_start, off, mid):
                        self.pts.append((px, py, False))
                    seg_start = mid
                else:
                    # 最後一個曲線外控制點 + 終點曲線上控制點
                    for px, py in _quadratic(seg_start, off, on_pt):
                        self.pts.append((px, py, False))

        self._cx, self._cy = on_pt[0], on_pt[1]

    def curveTo(self, *points):
        """三次貝茲線段（供 CFF/OTF 字型使用，備用實作）。

        fontTools 慣例：points = (ctrl1, ctrl2, ..., end_pt)
        多段曲線時以每 3 點為一組傳入。
        """
        pts = [(self._cx, self._cy)] + [(p[0], p[1]) for p in points]
        i = 0
        while i + 3 < len(pts):
            for px, py in _cubic(pts[i], pts[i+1], pts[i+2], pts[i+3]):
                self.pts.append((px, py, False))
            i += 3
        self._cx, self._cy = points[-1][0], points[-1][1]

    def closePath(self):
        """封閉輪廓（TrueType 輪廓均為封閉）。"""
        pass

    def endPath(self):
        """開放輪廓終端（通常不使用）。"""
        pass

    def addComponent(self, glyphName, transformation):
        """複合字符參照。透過 glyph_set 繪製時會自動展開，此處不需處理。"""
        pass


# ═══════════════════════════════════════════════════════════════════════
# 字型載入 & 字符轉換
# ═══════════════════════════════════════════════════════════════════════

def font_metrics(font) -> tuple[float, float, float]:
    """取得字型度量值，傳回 (em_size, ascender, descender)。

    descender 為負值（例：-200）。
    若 OS/2 表格可用則優先使用 sTypo 值。
    """
    em = float(font['head'].unitsPerEm)
    try:
        asc = float(font['OS/2'].sTypoAscender)
        dsc = float(font['OS/2'].sTypoDescender)   # 負值
    except (AttributeError, KeyError):
        asc = float(font['hhea'].ascent)
        dsc = float(font['hhea'].descent)           # 負值
    # descender 為 0 時使用備用值
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
    """TrueType 字符 → KANDAT IP 值列表（填充字形）。

    演算法：
      1. 以 CollectorPen 收集字符輪廓點列，並依輪廓分組
      2. 在高解析度（RENDER_SIZE×RENDER_SIZE）PIL 影像上逐輪廓以
         XOR 填充繪製，實現偶奇填充規則
         （處理含孔字符，如「口・日・目」等）
      3. 向下取樣為 33×33 格線以確定填充區域
      4. 逐行掃描格線，將填充區間轉換為水平筆畫
         （行頭 pen_up → 行尾 pen_down）

    座標系：
      TrueType：Y 向上，原點 = 基線左端
      PIL      ：Y 向下（上緣 = ascender，下緣 = descender）
      KANDAT   ：X 左→右 [0,32]，Y 下→上 [0,32]
    """
    RENDER_SIZE = 128   # 中間渲染解析度
    GRID_N      = 33    # 格線點數 (0..32)

    pen = CollectorPen()
    try:
        glyph_set[glyph_name].draw(pen)
    except Exception:
        return []

    if not pen.pts:
        return []

    y_range  = asc - dsc   # > 0
    y_offset = -dsc        # 將 descender 移至 0（正值）

    # ── 1. 輪廓分組 & 轉換為 PIL 座標 ───────────────────────────────────
    # PIL Y 向下，需反轉字型的 Y 上向座標
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

    # ── 2. 偶奇填充（逐輪廓 XOR）────────────────────────────────────────
    # 正確處理外輪廓與內輪廓（孔）：適用於「口・日・目」等含孔字符
    buf = np.zeros((RENDER_SIZE, RENDER_SIZE), dtype=np.uint8)
    for contour in contours:
        if len(contour) < 3:
            continue
        mask = Image.new('L', (RENDER_SIZE, RENDER_SIZE), 0)
        ImageDraw.Draw(mask).polygon(
            [(round(x), round(y)) for x, y in contour], fill=1
        )
        buf ^= np.array(mask, dtype=np.uint8)

    # ── 3. 向下取樣為 33×33 格線 ────────────────────────────────────────
    # 對應格線儲存格的像素區塊，若過半數為填充則視為填充
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

    # ── 4. 水平掃描線 → 線段列表 ────────────────────────────────────────
    # PIL 第 0 行 = ascender 側 = KANDAT Y=32
    # PIL 第 32 行 = descender 側 = KANDAT Y=0
    #
    # 行內有不連續區域的字（口・日・間・算等）分割為多段，
    # 避免將孔的內側誤填。
    all_segs: list[tuple[int, int, int]] = []   # (y_kandat, x_start, x_end)

    for row in range(GRID_N):
        y_kandat = (GRID_N - 1) - row
        filled = np.where(grid[row])[0]
        if len(filled) == 0:
            continue

        # 依連續像素塊產生各線段
        seg_start = int(filled[0])
        prev      = int(filled[0])
        for idx in range(1, len(filled)):
            cur = int(filled[idx])
            if cur > prev + 1:          # 不連續 → 確定一段
                all_segs.append((y_kandat, seg_start, prev))
                seg_start = cur
            prev = cur
        all_segs.append((y_kandat, seg_start, prev))

    if not all_segs:
        return []

    # ── 5. 依 IP 上限 (119) 進行均等抽樣 ────────────────────────────────
    # 不採用簡單末尾截斷，改以均等抽樣保留字的上、中、下各部分
    MAX_IPS  = MAX_RECS * DATA_INTS - 1   # 119
    max_segs = MAX_IPS // 2               # 每段 = 2 個 IP 值

    if len(all_segs) > max_segs:
        step     = len(all_segs) / max_segs
        all_segs = [all_segs[round(i * step)] for i in range(max_segs)]

    # ── 6. IP 值編碼 ─────────────────────────────────────────────────────
    ips: list[int] = []
    for y_kandat, x_start, x_end in all_segs:
        ips.append(2 * 10000 + x_start * 100 + y_kandat)  # pen up（移動）
        ips.append(1 * 10000 + x_end   * 100 + y_kandat)  # pen down（畫線）

    return ips


# ═══════════════════════════════════════════════════════════════════════
# 座標轉換 & IP 編碼
# ═══════════════════════════════════════════════════════════════════════

def deduplicate(pts: list) -> list:
    """移除連續重複的格線點（pen_up 點無論位置均保留）。"""
    out = []; prev = None
    for x, y, pu in pts:
        key = (round(x), round(y))
        if pu or key != prev:
            out.append((x, y, pu))
            if not pu:
                prev = key
    return out


def to_ip_values(pts: list) -> list[int]:
    """格線座標列 → KANDAT IP 值列表。
    pen_flag=2 (IPEN=3, 移動), pen_flag=1 (IPEN=2, 畫線)
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
# 記錄封裝
# ═══════════════════════════════════════════════════════════════════════

def pack_to_records(ip_values: list[int]) -> list[list[int]]:
    """將 IP 值列表轉換為 16-int 記錄列表（鏈結指標初始化為 0）。

    記錄結構：
      Word 0..14 : 資料（首筆記錄 Word 0 = NPKAN 計數）
      Word 15    : 鏈結指標（槽號，0=結束）

    最大 8 筆記錄鏈：count(1) + 14 + 7×15 = 120 字組 → 最多 119 個 IP。
    """
    max_pts = MAX_RECS * DATA_INTS - 1   # 8×15-1 = 119
    if len(ip_values) > max_pts:
        ip_values = ip_values[:max_pts]

    # 資料串流：[count, ip1, ip2, ...]
    stream = [len(ip_values)] + ip_values

    records = []
    for i in range(0, len(stream), DATA_INTS):
        chunk = stream[i : i + DATA_INTS]
        chunk += [0] * (DATA_INTS - len(chunk))  # 填補
        records.append(chunk + [0])               # chain=0（稍後設定）

    if not records:
        records = [[0] * INTS_PER]   # 空字符：僅 count=0

    return records


# ═══════════════════════════════════════════════════════════════════════
# KANDAT 二進位生成
# ═══════════════════════════════════════════════════════════════════════

def build_kandat_bytes(
    xycode_map : dict[int, int],
    font_path  : str,
    font_number: int,
    max_primary: int,
    verbose    : bool,
    label      : str,
) -> bytes:
    """生成並傳回 KANDAT 二進位資料。

    Args:
        xycode_map  : {slot_index: unicode_cp}
        font_path   : TTC/TTF 字型路徑
        font_number : TTC 內字型索引
        max_primary : 主要槽號上限
        verbose     : 顯示詳細記錄
        label       : 記錄用標籤

    Returns:
        bytes: KANDAT 檔案二進位內容
    """
    # ── 載入字型 ─────────────────────────────────────────────────────────
    print(f'\n{"═"*60}')
    print(f'  [{label}]  主要槽號上限：{max_primary}')
    print(f'  載入字型中：{font_path} [#{font_number}] ...')
    font      = TTFont(font_path, fontNumber=font_number)
    em, asc, dsc = font_metrics(font)
    cmap      = font.getBestCmap() or {}
    glyph_set = font.getGlyphSet()
    print(f'  em={em:.0f}  ascender={asc:.0f}  descender={dsc:.0f}  '
          f'字符數={len(glyph_set)}')
    print(f'{"═"*60}')

    # ── Step 1：產生各槽的 IP 值 ─────────────────────────────────────────
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

    # ── Step 2：記錄封裝 & 鏈結槽號配置 ────────────────────────────────
    flat: dict[int, list[int]] = {}   # slot → record (16 ints)
    next_extra = max_primary + 1      # 延續記錄槽號起始位置

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

    # ── Step 3：產生位元組陣列 ───────────────────────────────────────────
    # BASIC：RECNO = XYCODE + 8  →  file_offset = (RECNO-1)*32 = (slot+7)*32
    max_slot  = max(flat.keys()) if flat else max_primary
    file_size = (max_slot + 8) * REC_SIZE
    buf       = bytearray(file_size)

    for slot, record in flat.items():
        offset = (slot + 7) * REC_SIZE
        for j, val in enumerate(record):
            struct.pack_into('<h', buf, offset + j * 2, val)

    n_cont = next_extra - max_primary - 1
    print(f'\n  有字符    ：{found:5d}')
    print(f'  無字符    ：{missing:5d}  (無 Unicode 對照：{empty})')
    print(f'  延續記錄  ：{n_cont:5d}')
    print(f'  最大槽號  ：{max_slot:5d}')
    print(f'  檔案大小  ：{len(buf):,} bytes ({len(buf)//1024} KB)')
    return bytes(buf)


# ═══════════════════════════════════════════════════════════════════════
# 程式進入點
# ═══════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description='MS Gothic TTC 字型 → KANDAT.DAT + KANDAT2.DAT'
    )
    parser.add_argument(
        '--font', default=FONT_PATH,
        help=f'TTC/TTF 字型路徑（預設：{FONT_PATH}）'
    )
    parser.add_argument(
        '--font-number', type=int, default=FONT_NUMBER,
        help=f'TTC 內字型索引（預設：{FONT_NUMBER}  '
             f'0=MS Gothic / 1=MS PGothic / 2=MS UI Gothic）'
    )
    parser.add_argument(
        '--out1', default=OUTPUT1,
        help=f'KANDAT.DAT 輸出路徑（預設：{OUTPUT1}）'
    )
    parser.add_argument(
        '--out2', default=OUTPUT2,
        help=f'KANDAT2.DAT 輸出路徑（預設：{OUTPUT2}）'
    )
    parser.add_argument(
        '--verbose', '-v', action='store_true',
        help='顯示每個字符的處理詳情'
    )
    args = parser.parse_args()

    print('MS Gothic TTC → KANDAT 產生器')
    print(f'  字型          ：{args.font} [#{args.font_number}]')
    print(f'  KANDAT.DAT 輸出：{args.out1}')
    print(f'  KANDAT2.DAT輸出：{args.out2}')

    # ── 建立 Shift-JIS → XYCODE → Unicode 對照表 ─────────────────────
    print('\n掃描 Shift-JIS 編碼空間中...')
    map1, map2 = build_xycode_maps()
    print(f'  KANDAT.DAT  對照表：{len(map1)} 筆')
    print(f'  KANDAT2.DAT 對照表：{len(map2)} 筆')

    # ── 產生 KANDAT.DAT ──────────────────────────────────────────────
    dat1 = build_kandat_bytes(
        map1, args.font, args.font_number, MAX_SLOT1, args.verbose, 'KANDAT.DAT'
    )
    with open(args.out1, 'wb') as f:
        f.write(dat1)
    print(f'\n  → {args.out1} 寫入完成')

    # ── 產生 KANDAT2.DAT ─────────────────────────────────────────────
    dat2 = build_kandat_bytes(
        map2, args.font, args.font_number, MAX_SLOT2, args.verbose, 'KANDAT2.DAT'
    )
    with open(args.out2, 'wb') as f:
        f.write(dat2)
    print(f'  → {args.out2} 寫入完成')

    print('\n完成。')


if __name__ == '__main__':
    main()