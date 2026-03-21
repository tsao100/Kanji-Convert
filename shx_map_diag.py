#!/usr/bin/env python3
"""
shx_map_diag.py
===============
診斷 extfont.shx / extfont2.shx 的 shapeno 與 Shift-JIS 內碼對應關係。

使用方法：
  python shx_map_diag.py extfont.shx
  python shx_map_diag.py extfont2.shx
"""
import sys, struct

def load_shx_index(path):
    """讀取 SHX BigFont 索引，傳回 {shapeno: (abs_offset, defbytes)} 及原始資料。"""
    with open(path, 'rb') as f:
        raw = f.read()

    pos_1a = raw.find(0x1A)
    if pos_1a < 0:
        print(f'錯誤: 找不到 0x1A in {path}')
        return {}, raw

    pos = pos_1a + 1
    pos += 2  # skip unknown 08 00

    nshapes = struct.unpack_from('<H', raw, pos)[0]; pos += 2
    nranges = raw[pos]; pos += 1
    pos += nranges * 4
    pos += 3  # terminator

    index_start = pos
    data_start  = index_start + nshapes * 8

    shapes = {}
    p = index_start
    for _ in range(nshapes):
        if p + 8 > len(raw): break
        defbytes    = struct.unpack_from('<H', raw, p)[0]
        file_offset = struct.unpack_from('<I', raw, p+2)[0]
        shapeno     = struct.unpack_from('<H', raw, p+6)[0]
        p += 8
        if defbytes == 0: continue
        abs_off = data_start + file_offset
        if abs_off + defbytes > len(raw): continue
        shapes[shapeno] = (abs_off, defbytes)

    return shapes, raw


def shapeno_to_sjis(shapeno):
    """shapeno (u16) → Shift-JIS bytes (hi, lo)。"""
    hi = (shapeno >> 8) & 0xFF
    lo =  shapeno       & 0xFF
    return hi, lo


def try_decode(hi, lo):
    """嘗試將 Shift-JIS 雙位元組解碼為 Unicode 字元。"""
    try:
        ch = bytes([hi, lo]).decode('cp932')
        return ch
    except Exception:
        return None


def main():
    if len(sys.argv) < 2:
        print('用法: python shx_map_diag.py <extfont.shx 或 extfont2.shx>')
        sys.exit(1)

    path = sys.argv[1]
    shapes, raw = load_shx_index(path)

    print(f'\n{"="*70}')
    print(f'  檔案: {path}   形狀數: {len(shapes)}')
    print(f'{"="*70}')

    # ── 1. 顯示前 40 個形狀的 shapeno / SJIS / 字元 ──────────────────
    print('\n[A] 索引前 40 項：shapeno → Shift-JIS 內碼 → 字元')
    print(f'  {"#":>4}  {"shapeno":>8}  {"hi":>4}  {"lo":>4}  {"SJIS hex":>10}  char  data[0:8]')
    print('  ' + '-'*62)
    for k, (sno, (off, dbytes)) in enumerate(sorted(shapes.items())[:40]):
        hi, lo = shapeno_to_sjis(sno)
        ch = try_decode(hi, lo) or '----'
        data_preview = raw[off:off+8].hex(' ')
        print(f'  {k:>4}  {sno:#08x}  {hi:#04x}  {lo:#04x}  {hi:02X}{lo:02X}       '
              f' {ch!r:6}  {data_preview}')

    # ── 2. 統計 shapeno 的 hi-byte 分佈 ──────────────────────────────
    print('\n[B] shapeno hi-byte 分佈（前導位元組統計）')
    from collections import Counter
    hi_counts = Counter((sno >> 8) & 0xFF for sno in shapes)
    for hi in sorted(hi_counts):
        lo_list = sorted(sno & 0xFF for sno in shapes if (sno>>8)==hi)
        lo_min  = lo_list[0]  if lo_list else 0
        lo_max  = lo_list[-1] if lo_list else 0
        sample  = try_decode(hi, lo_list[0]) if lo_list else '?'
        print(f'    hi=0x{hi:02X}  count={hi_counts[hi]:5d}  '
              f'lo: 0x{lo_min:02X}..0x{lo_max:02X}  '
              f'sample={sample!r}')

    # ── 3. 特定字元查找：用已知漢字反查 ─────────────────────────────
    print('\n[C] 已知漢字 → Shift-JIS → shapeno 查找')
    test_chars = [
        '亜', '唖', '娃', '阿', '哀',   # JIS level-1 最前幾個
        '一', '二', '三', '四', '五',   # 數字漢字
        '人', '口', '山', '川', '田',   # 常用漢字
        '愛', '悪', '握', '圧', '胃',
    ]
    print(f'  {"char":>4}  {"cp932 hex":>10}  {"shapeno":>10}  in_shx  data[0:8]')
    print('  ' + '-'*58)
    for ch in test_chars:
        try:
            b = ch.encode('cp932')
        except Exception:
            print(f'  {ch!r:>4}  encode fail')
            continue
        if len(b) != 2:
            print(f'  {ch!r:>4}  not 2-byte SJIS')
            continue
        hi, lo = b[0], b[1]
        sno    = (hi << 8) | lo
        sno_be = sno  # big-endian interpretation
        # also try little-endian
        sno_le = (lo << 8) | hi

        in_be = sno_be in shapes
        in_le = sno_le in shapes

        if in_be:
            off, db = shapes[sno_be]
            preview = raw[off:off+8].hex(' ')
            print(f'  {ch!r:>4}  {hi:02X}{lo:02X}        {sno_be:#010x}  BE=YES  {preview}')
        elif in_le:
            off, db = shapes[sno_le]
            preview = raw[off:off+8].hex(' ')
            print(f'  {ch!r:>4}  {hi:02X}{lo:02X}        {sno_le:#010x}  LE=YES  {preview}')
        else:
            print(f'  {ch!r:>4}  {hi:02X}{lo:02X}        BE={sno_be:#06x}/LE={sno_le:#06x}  NOT FOUND')

    # ── 4. 反向查找：取前 10 個 shapeno，反解 cp932 字元 ─────────────
    print('\n[D] 前 20 個 shapeno → 嘗試 cp932 解碼（大端 & 小端）')
    print(f'  {"shapeno":>10}  {"BE bytes":>10}  {"BE char":>8}  {"LE bytes":>10}  {"LE char":>8}')
    print('  ' + '-'*56)
    for sno in sorted(shapes)[:20]:
        hi = (sno >> 8) & 0xFF
        lo =  sno       & 0xFF
        ch_be = try_decode(hi, lo) or '----'
        ch_le = try_decode(lo, hi) or '----'
        print(f'  {sno:#010x}  {hi:02X} {lo:02X}      {ch_be!r:>8}  {lo:02X} {hi:02X}      {ch_le!r:>8}')

    # ── 5. 找 shapeno=0（字型度量值）的內容 ──────────────────────────
    print('\n[E] shapeno=0（字型度量值 header）')
    if 0 in shapes:
        off, db = shapes[0]
        data = raw[off:off+db]
        print(f'  defbytes={db}  data={data.hex(" ")}')
        if db >= 2:
            print(f'  above={data[0]}  below={data[1]}')
    else:
        print('  shapeno=0 不在索引中')


if __name__ == '__main__':
    main()