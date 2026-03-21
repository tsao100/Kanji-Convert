#!/usr/bin/env python3
"""
shx_diag.py  v2
===============
SHX 二進位結構詳細診斷（含形狀資料驗證）

用法：
    python shx_diag.py extfont.shx
    python shx_diag.py extfont2.shx
"""

import sys, struct, os

def hexdump(data: bytes, start: int = 0, length: int = 64, label: str = ''):
    if label:
        print(f'\n── {label} ──')
    end = min(start + length, len(data))
    for i in range(start, end, 16):
        row = data[i : i + 16]
        hex_ = ' '.join(f'{b:02X}' for b in row)
        asc  = ''.join(chr(b) if 32 <= b < 127 else '.' for b in row)
        print(f'  {i:06X}  {hex_:<47s}  {asc}')


def is_valid_sjis_2byte(shapeno: int) -> bool:
    hi = (shapeno >> 8) & 0xFF
    lo = shapeno & 0xFF
    if not ((0x81 <= hi <= 0x9F) or (0xE0 <= hi <= 0xFC)):
        return False
    if not ((0x40 <= lo <= 0x7E) or (0x80 <= lo <= 0xFE)):
        return False
    return True


def sjis_to_char(shapeno: int) -> str:
    try:
        hi = (shapeno >> 8) & 0xFF
        lo = shapeno & 0xFF
        return bytes([hi, lo]).decode('cp932')
    except Exception:
        return '?'


def try_parse(raw: bytes, path: str):
    print(f'\n{"="*60}')
    print(f'  檔案：{path}  ({len(raw):,} bytes)')
    print(f'{"="*60}')

    hexdump(raw, 0, 64, '檔案開頭 (前 64 bytes)')

    # ── 1. 找 0x1A ────────────────────────────────────────────────────
    pos_1a = raw.find(0x1A)
    if pos_1a < 0:
        print('找不到 0x1A'); return
    print(f'\n0x1A 位置：{pos_1a} (0x{pos_1a:04X})')

    # ── 2. 解析標頭 ───────────────────────────────────────────────────
    pos = pos_1a + 1
    unk1  = raw[pos];     pos += 1   # 未知 u8
    unk2  = raw[pos];     pos += 1   # 未知 u8
    nshapes = struct.unpack_from('<H', raw, pos)[0]; pos += 2
    nranges = raw[pos];   pos += 1

    print(f'\n標頭解析：')
    print(f'  unknown[0]  = 0x{unk1:02X} = {unk1}')
    print(f'  unknown[1]  = 0x{unk2:02X} = {unk2}')
    print(f'  nshapes     = {nshapes}  (0x{nshapes:04X})')
    print(f'  nranges     = {nranges}')

    # 讀取 escape 範圍
    ranges = []
    for r in range(nranges):
        if pos + 4 > len(raw): break
        z1, s, z2, e = raw[pos], raw[pos+1], raw[pos+2], raw[pos+3]
        ranges.append((s, e))
        print(f'  range[{r}]     = 0x{s:02X}..0x{e:02X} '
              f'(raw: {z1:02X} {s:02X} {z2:02X} {e:02X})')
        pos += 4

    # 3 bytes 終止符
    term = raw[pos:pos+3]
    print(f'  終止符 3B   = {term.hex(" ")}')
    pos += 3

    index_start = pos
    print(f'\n  索引表起始位置 = {index_start} (0x{index_start:04X})')
    data_start = index_start + nshapes * 8
    print(f'  資料區起始位置 = {data_start} (0x{data_start:04X})')

    # ── 3. 讀取索引（前 20 項） ───────────────────────────────────────
    print(f'\n索引前 20 項（格式：defbytes u16 + file_offset u32 + shapeno u16）：')
    print(f'  {"#":>4}  {"defbytes":>8}  {"file_offset":>12}  {"shapeno":>8}  {"abs_offset":>12}  {"char"}')
    print(f'  {"-"*75}')

    index_entries = []
    p = index_start
    for k in range(min(nshapes, 20)):
        if p + 8 > len(raw): break
        defbytes    = struct.unpack_from('<H', raw, p)[0]
        file_offset = struct.unpack_from('<I', raw, p+2)[0]
        shapeno     = struct.unpack_from('<H', raw, p+6)[0]
        abs_off     = data_start + file_offset
        ch          = sjis_to_char(shapeno) if is_valid_sjis_2byte(shapeno) else f'(0x{shapeno:04X})'
        valid_sjis  = '✓' if is_valid_sjis_2byte(shapeno) else ' '
        print(f'  {k:>4}  {defbytes:>8}  {file_offset:>12}  '
              f'0x{shapeno:04X} {valid_sjis}  {abs_off:>12}  {ch}')
        index_entries.append((defbytes, file_offset, shapeno))
        p += 8

    # ── 4. 驗證：以相對偏移讀取形狀資料 ─────────────────────────────
    print(f'\n形狀資料驗證（相對偏移 = data_start + file_offset）：')
    for k, (defbytes, file_offset, shapeno) in enumerate(index_entries[:5]):
        abs_off = data_start + file_offset
        print(f'\n  [{k}] shapeno=0x{shapeno:04X}  defbytes={defbytes}'
              f'  abs_offset={abs_off}')
        if abs_off + defbytes <= len(raw):
            shape_data = raw[abs_off : abs_off + defbytes]
            hexdump(raw, abs_off, min(defbytes, 32),
                    f'形狀 0x{shapeno:04X} 資料（前 {min(defbytes,32)} bytes）')
            # 計算以 0x00 結尾的位置
            zero_pos = shape_data.find(0x00)
            print(f'    → 第一個 0x00（END）在 offset {zero_pos}')
        else:
            print(f'    → ⚠️  超出檔案範圍！'
                  f'（abs={abs_off}+{defbytes}={abs_off+defbytes} > {len(raw)}）')

    # ── 5. 也試試絕對偏移 ─────────────────────────────────────────────
    print(f'\n形狀資料驗證（絕對偏移 = file_offset）：')
    for k, (defbytes, file_offset, shapeno) in enumerate(index_entries[:5]):
        print(f'\n  [{k}] shapeno=0x{shapeno:04X}  abs_offset={file_offset}')
        if file_offset + defbytes <= len(raw):
            hexdump(raw, file_offset, min(defbytes, 32),
                    f'形狀 0x{shapeno:04X} 資料（絕對偏移）')
            shape_data = raw[file_offset : file_offset + defbytes]
            zero_pos = shape_data.find(0x00)
            print(f'    → 第一個 0x00（END）在 offset {zero_pos}')
        else:
            print(f'    → ⚠️  超出檔案範圍！')

    # ── 6. 形狀 0（字型度量值）────────────────────────────────────────
    print(f'\n搜尋形狀 0（字型度量值）：')
    for k in range(min(nshapes, index_start)):
        p2 = index_start + k * 8
        if p2 + 8 > len(raw): break
        defbytes    = struct.unpack_from('<H', raw, p2)[0]
        file_offset = struct.unpack_from('<I', raw, p2+2)[0]
        shapeno     = struct.unpack_from('<H', raw, p2+6)[0]
        if shapeno == 0:
            abs_off = data_start + file_offset
            print(f'  在索引 [{k}]：defbytes={defbytes}, file_offset={file_offset}, '
                  f'abs(相對)={abs_off}')
            hexdump(raw, abs_off, min(defbytes, 16), '形狀 0 內容（相對偏移）')
            hexdump(raw, file_offset, min(defbytes, 16), '形狀 0 內容（絕對偏移）')
            break
    else:
        print('  未在前 20 項中找到形狀 0')


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else 'extfont.shx'
    try:
        with open(path, 'rb') as f:
            raw = f.read()
    except FileNotFoundError:
        print(f'找不到：{path}')
        return
    try_parse(raw, path)


if __name__ == '__main__':
    main()