#!/usr/bin/env python3
"""
shx_xycode_diag.py
==================
驗證 SHX shapeno → Shift-JIS → JIS → XYCODE → KANDAT 槽號 的完整對應鏈。

使用方法：
  python shx_xycode_diag.py extfont.shx extfont2.shx
"""
import sys, struct

# ══════════════════════════════════════════════════════════════
# ZKC / JIS 轉換（與 make_kandat.py 完全相同）
# ══════════════════════════════════════════════════════════════
ZKC = [
    0x2120, 0x217F, 0x2220, 0x222F, 0x232F, 0x233A,
    0x2340, 0x235B, 0x2360, 0x237B, 0x2420, 0x2474,
    0x2520, 0x2577, 0x2620, 0x2639, 0x2640, 0x2659,
    0x2720, 0x2742, 0x2750, 0x2772,
]

def mstojis(ms):
    il = ms & 0xFF; ih = (ms >> 8) & 0xFF
    ihh = 2*(ih-129)+33 if ih <= 159 else 2*(ih-224)+95
    if il >= 159: ihh += 1
    if   64  <= il <= 126: ill = il - 31
    elif 128 <= il <= 158: ill = il - 32
    elif 159 <= il <= 252: ill = il - 126
    else: return 0
    return (ihh << 8) | ill

def jistoxy(jis):
    hcd = (jis >> 8) & 0xFF; lcd = jis & 0xFF
    if 0x2120 < jis < 0x277E:
        kcbase = 0
        for i in range(1, len(ZKC), 2):
            if ZKC[i-1] < jis < ZKC[i]:
                return jis - ZKC[i-1] + kcbase
            kcbase += ZKC[i] - ZKC[i-1] - 1
        return 0
    if 0x30 <= hcd <= 0x4F:
        if not (0x21 <= lcd <= 0x7E): return 0
        return (hcd-0x30)*94 + (lcd-0x20) + 453
    if 0x50 <= hcd <= 0x75:
        if not (0x21 <= lcd <= 0x7E): return 0
        return (hcd-0x50)*94 + (lcd-0x20) + 4000
    if 0x7620 < jis < 0x76D0:
        return jis - 0x7620 + 3518
    return 0

# ══════════════════════════════════════════════════════════════
# SHX 索引載入
# ══════════════════════════════════════════════════════════════
def load_index(path):
    with open(path, 'rb') as f: raw = f.read()
    pos = raw.find(0x1A) + 1 + 2
    nshapes = struct.unpack_from('<H', raw, pos)[0]; pos += 2
    nranges = raw[pos]; pos += 1
    pos += nranges * 4 + 3
    data_start = pos + nshapes * 8
    shapes = {}
    p = pos
    for _ in range(nshapes):
        if p + 8 > len(raw): break
        defbytes = struct.unpack_from('<H', raw, p)[0]
        file_off = struct.unpack_from('<I', raw, p+2)[0]
        shapeno  = struct.unpack_from('<H', raw, p+6)[0]
        p += 8
        if defbytes and (data_start + file_off + defbytes <= len(raw)):
            shapes[shapeno] = (data_start + file_off, defbytes)
    return shapes, raw

# ══════════════════════════════════════════════════════════════
# 座標分析（估算字形的 above/below/advance）
# ══════════════════════════════════════════════════════════════
def analyze_coords(data):
    """快速掃描字形操作碼，找出 x/y 極值。"""
    DIR16 = [(1,0),(1,.5),(1,1),(.5,1),(0,1),(-.5,1),(-1,1),(-1,.5),
             (-1,0),(-1,-.5),(-1,-1),(-.5,-1),(0,-1),(.5,-1),(1,-1),(1,-.5)]
    x=y=0.0; sc=1.0
    xs=[0.0]; ys=[0.0]
    stack=[]; i=0
    while i < len(data):
        op=data[i]; i+=1
        if   op==0: break
        elif op in (1,2,0x0F): pass
        elif op==3:
            if i<len(data): sc/=max(data[i],1); i+=1
        elif op==4:
            if i<len(data): sc*=data[i]; i+=1
        elif op==5: stack.append((x,y,sc))
        elif op==6:
            if stack: x,y,sc=stack.pop()
        elif op==7: i+=2
        elif op==8:
            if i+2>len(data): break
            dx=data[i]-256 if data[i]>=128 else data[i]; i+=1
            dy=data[i]-256 if data[i]>=128 else data[i]; i+=1
            x+=dx*sc; y+=dy*sc; xs.append(x); ys.append(y)
        elif op==9:
            while i+2<=len(data):
                dx=data[i]-256 if data[i]>=128 else data[i]; i+=1
                dy=data[i]-256 if data[i]>=128 else data[i]; i+=1
                if dx==0 and dy==0: break
                x+=dx*sc; y+=dy*sc; xs.append(x); ys.append(y)
        elif op==0x0A: i+=2
        elif op==0x0B: i+=4
        elif op==0x0E:
            # skip next command
            if i<len(data):
                nb=data[i]
                if nb==8: i+=3
                elif nb in (3,4,7): i+=2
                elif nb==0x0A: i+=3
                else: i+=1
        elif op>=0x10:
            L=(op>>4)&0xF; N=op&0xF
            dx,dy=DIR16[N]; x+=dx*L*sc; y+=dy*L*sc
            xs.append(x); ys.append(y)
    return min(xs),max(xs),min(ys),max(ys)

# ══════════════════════════════════════════════════════════════
# 主程式
# ══════════════════════════════════════════════════════════════
def main():
    if len(sys.argv) < 3:
        print('用法: python shx_xycode_diag.py extfont.shx extfont2.shx')
        sys.exit(1)

    path1, path2 = sys.argv[1], sys.argv[2]
    shapes1, raw1 = load_index(path1)
    shapes2, raw2 = load_index(path2)
    print(f'載入: {path1} ({len(shapes1)} shapes), {path2} ({len(shapes2)} shapes)')

    # ── A. 驗證 SJIS→JIS→XYCODE 對應鏈 ──────────────────────
    print('\n' + '='*70)
    print('[A] 已知漢字的完整對應鏈驗證')
    print('='*70)
    test_chars = [
        ('亜','一','二','三','四','五'),   # 常用基準
        ('愛','右','雨','円','王','音'),
        ('下','火','花','貝','学','気'),
        ('九','休','玉','金','空','月'),
    ]
    hdr = f'  {"ch":>2}  {"SJIS":>6}  {"shapeno":>8}  {"JIS":>6}  {"XYCODE":>7}  {"slot":>5}  {"in1":>4}  {"in2":>4}'
    print(hdr)
    print('  ' + '-'*62)
    for row in test_chars:
        for ch in row:
            try: b = ch.encode('cp932')
            except: continue
            if len(b) != 2: continue
            sjis = (b[0]<<8)|b[1]
            jis  = mstojis(sjis)
            xy   = jistoxy(jis) if jis else 0
            if xy > 4000: slot = xy-4000; dat='DAT2'
            elif xy > 0:  slot = xy;      dat='DAT1'
            else:         slot = 0;       dat='----'
            in1 = 'YES' if sjis in shapes1 else 'no'
            in2 = 'YES' if sjis in shapes2 else 'no'
            print(f'  {ch:>2}  {sjis:06X}  {sjis:08x}  {jis:06X}  {xy:7d}  {slot:5d}  {in1:>4}  {in2:>4}  {dat}')

    # ── B. 槽號範圍覆蓋檢查 ───────────────────────────────────
    print('\n' + '='*70)
    print('[B] XYCODE 槽號覆蓋分析')
    print('='*70)

    # 建立 xycode→sjis 映射（正向）
    xy_to_sjis: dict[int,int] = {}
    for hi in range(0x81, 0xF0):
        for lo in list(range(0x40,0x7F)) + list(range(0x80,0xFD)):
            try: ch = bytes([hi,lo]).decode('cp932')
            except: continue
            if len(ch)!=1: continue
            sjis = (hi<<8)|lo
            jis  = mstojis(sjis)
            if not jis: continue
            xy = jistoxy(jis)
            if xy > 0:
                xy_to_sjis.setdefault(xy, sjis)

    # 統計槽號覆蓋
    dat1_total = dat1_found1 = dat1_found2 = dat1_missing = 0
    dat2_total = dat2_found1 = dat2_found2 = dat2_missing = 0
    missing_samples = []

    for xy, sjis in sorted(xy_to_sjis.items()):
        if 1 <= xy <= 3693:
            dat1_total += 1
            in1 = sjis in shapes1
            in2 = sjis in shapes2
            if in1: dat1_found1 += 1
            if in2: dat1_found2 += 1
            if not in1 and not in2:
                dat1_missing += 1
                if len(missing_samples) < 20:
                    try: ch=bytes([sjis>>8,sjis&0xFF]).decode('cp932')
                    except: ch='?'
                    missing_samples.append(f'{ch}({sjis:04X}/xy={xy})')
        elif xy > 4000:
            s = xy-4000
            if 1 <= s <= 3572:
                dat2_total += 1
                in1 = sjis in shapes1
                in2 = sjis in shapes2
                if in1: dat2_found1 += 1
                if in2: dat2_found2 += 1
                if not in1 and not in2: dat2_missing += 1

    print(f'\n  KANDAT.DAT  (DAT1):')
    print(f'    總槽數:       {dat1_total}')
    print(f'    extfont  有字形: {dat1_found1}')
    print(f'    extfont2 有字形: {dat1_found2}')
    print(f'    兩者皆缺:     {dat1_missing}')
    print(f'\n  KANDAT2.DAT (DAT2):')
    print(f'    總槽數:       {dat2_total}')
    print(f'    extfont  有字形: {dat2_found1}')
    print(f'    extfont2 有字形: {dat2_found2}')
    print(f'    兩者皆缺:     {dat2_missing}')

    if missing_samples:
        print(f'\n  DAT1 缺字前 20 個: {", ".join(missing_samples)}')

    # ── C. 估算 above/below/advance 度量值 ────────────────────
    print('\n' + '='*70)
    print('[C] 實際字形座標統計（估算 above/below/advance）')
    print('='*70)

    sample_sjis = [
        0x889F,  # 亜
        0x88EA,  # 一
        0x906C,  # 人
        0x8E52,  # 山
        0x8141,  # 、
        0x8340,  # ァ
    ]

    for sjis in sample_sjis:
        try: ch=bytes([sjis>>8,sjis&0xFF]).decode('cp932')
        except: ch='?'
        for label, shapes, raw in [('ext1',shapes1,raw1),('ext2',shapes2,raw2)]:
            if sjis in shapes:
                off, db = shapes[sjis]
                data = raw[off:off+db]
                xmin,xmax,ymin,ymax = analyze_coords(data)
                print(f'  {ch}({sjis:04X}) [{label}]: '
                      f'x=[{xmin:.1f}..{xmax:.1f}] y=[{ymin:.1f}..{ymax:.1f}]  '
                      f'width={xmax-xmin:.1f} height={ymax-ymin:.1f}')
                break
        else:
            print(f'  {ch}({sjis:04X}): NOT FOUND in either font')

    # ── D. Shape 0 的真實內容（確認 above/below）───────────────
    print('\n' + '='*70)
    print('[D] Shape 0 內容分析（BigFont 度量值來源）')
    print('='*70)
    for label, shapes, raw in [('extfont',shapes1,raw1),('extfont2',shapes2,raw2)]:
        if 0 in shapes:
            off, db = shapes[0]
            data = raw[off:off+db]
            xmin,xmax,ymin,ymax = analyze_coords(data)
            print(f'  [{label}] shape 0: defbytes={db}  raw[0:4]={data[:4].hex(" ")}')
            print(f'    座標範圍: x=[{xmin:.1f}..{xmax:.1f}] y=[{ymin:.1f}..{ymax:.1f}]')
            print(f'    raw[0]={data[0]}(0x{data[0]:02X}) raw[1]={data[1]}(0x{data[1]:02X}) '
                  f'→ 若為度量值: above={data[0]}, below={data[1]}')
            print(f'    但 raw[0]=0x{data[0]:02X} 是 opcode {"DrawOFF" if data[0]==2 else "DrawON" if data[0]==1 else hex(data[0])}'
                  f' → 非度量值標頭')

    # ── E. 座標分佈統計（取 200 個漢字樣本）─────────────────
    print('\n' + '='*70)
    print('[E] 漢字字形座標分佈（200 字樣本，確認 above/below/advance）')
    print('='*70)
    all_xmin=[] ; all_xmax=[] ; all_ymin=[] ; all_ymax=[]
    count = 0
    # 取 extfont.shx 中的漢字（hi=0x88-0x9F, 0xE0-0xE5）
    for sjis in sorted(shapes1):
        hi = (sjis>>8)&0xFF
        if not (0x88 <= hi <= 0x9F or 0xE0 <= hi <= 0xE5): continue
        off, db = shapes1[sjis]
        data = raw1[off:off+db]
        xmin,xmax,ymin,ymax = analyze_coords(data)
        if xmax-xmin < 0.1: continue   # 空字形略過
        all_xmin.append(xmin); all_xmax.append(xmax)
        all_ymin.append(ymin); all_ymax.append(ymax)
        count += 1
        if count >= 200: break

    if all_xmax:
        import statistics
        print(f'  樣本數: {count}')
        print(f'  xmin: min={min(all_xmin):.1f}  median={statistics.median(all_xmin):.1f}  max={max(all_xmin):.1f}')
        print(f'  xmax: min={min(all_xmax):.1f}  median={statistics.median(all_xmax):.1f}  max={max(all_xmax):.1f}')
        print(f'  ymin: min={min(all_ymin):.1f}  median={statistics.median(all_ymin):.1f}  max={max(all_ymin):.1f}')
        print(f'  ymax: min={min(all_ymax):.1f}  median={statistics.median(all_ymax):.1f}  max={max(all_ymax):.1f}')
        print(f'\n  推估:')
        print(f'    above  ≈ {statistics.median(all_ymax):.1f}  (字形最高點中位數)')
        print(f'    below  ≈ {-statistics.median(all_ymin):.1f}  (字形最低點中位數的絕對值)')
        print(f'    advance≈ {statistics.median(all_xmax):.1f}  (字形最右點中位數，即字寬)')
        print(f'\n    → 建議在 make_kandat.py 設定:')
        print(f'      self.above   = {round(statistics.median(all_ymax))}')
        print(f'      self.below   = {round(-statistics.median(all_ymin))}')
        print(f'      self.advance = {round(statistics.median(all_xmax))}')

    # ── F. 抽樣比對：前 10 個漢字的槽號映射 ─────────────────
    print('\n' + '='*70)
    print('[F] 前 10 個漢字槽號映射詳細驗證')
    print('='*70)
    print(f'  {"slot":>5}  {"ch":>2}  {"SJIS":>6}  {"JIS":>6}  {"XYCODE":>7}  {"檔案":>6}  data[0:6]')
    print('  ' + '-'*58)
    count2 = 0
    for xy in range(454, 465):   # level-1 kanji 槽 454..464
        sjis = xy_to_sjis.get(xy)
        if not sjis: continue
        jis = mstojis(sjis)
        try: ch=bytes([sjis>>8,sjis&0xFF]).decode('cp932')
        except: ch='?'
        for label, shapes, raw in [('ext1',shapes1,raw1),('ext2',shapes2,raw2)]:
            if sjis in shapes:
                off, db = shapes[sjis]
                preview = raw[off:off+6].hex(' ')
                print(f'  {xy:5d}  {ch:>2}  {sjis:06X}  {jis:06X}  {xy:7d}  {label:>6}  {preview}')
                break
        else:
            print(f'  {xy:5d}  {ch:>2}  {sjis:06X}  {jis:06X}  {xy:7d}  {"MISSING":>6}')

if __name__ == '__main__':
    main()