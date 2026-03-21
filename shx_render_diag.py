#!/usr/bin/env python3
"""
shx_render_diag.py
==================
實際執行 render_shape（含追入子形狀 CALL），
顯示真實座標範圍，確認 above/below/advance 正規化參數。

使用方法：
  python shx_render_diag.py extfont.shx extfont2.shx
"""
import sys, struct, math

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
# 方向表（16方向）
# ══════════════════════════════════════════════════════════════
DIR16 = [
    ( 1,   0  ), ( 1,  .5 ), ( 1,   1 ), ( .5,  1 ),
    ( 0,   1  ), (-.5,  1 ), (-1,   1 ), (-1,  .5 ),
    (-1,   0  ), (-1, -.5 ), (-1,  -1 ), (-.5, -1 ),
    ( 0,  -1  ), ( .5, -1 ), ( 1,  -1 ), ( 1, -.5 ),
]

def get_shape_data(shapeno, all_shapes_list, all_raws):
    for shapes, raw in zip(all_shapes_list, all_raws):
        if shapeno in shapes:
            off, db = shapes[shapeno]
            return raw[off:off+db]
    return b''

# ══════════════════════════════════════════════════════════════
# _skip: 跳過一條完整指令（含參數）
# ══════════════════════════════════════════════════════════════
def skip_cmd(data, i):
    if i >= len(data): return i
    op = data[i]; i += 1
    if   op == 0x00: return i-1   # END: don't skip
    elif op in (0x01,0x02,0x05,0x06,0x0C,0x0D,0x0E,0x0F): pass
    elif op in (0x03,0x04): i += 1
    elif op == 0x07: i += 2
    elif op == 0x08: i += 2
    elif op == 0x09:
        while i+2 <= len(data):
            dx=data[i]; dy=data[i+1]; i+=2
            if dx==0 and dy==0: break
    elif op == 0x0A: i += 2
    elif op == 0x0B: i += 4
    # 0x10-0xFF: vector byte, no params
    return i

# ══════════════════════════════════════════════════════════════
# render_shape：追入 CALL，傳回 (x,y,pen_up) 點列
# ══════════════════════════════════════════════════════════════
def render_shape(shapeno, all_shapes_list, all_raws,
                 sc=1.0, depth=0, _visited=None):
    if depth > 8: return []
    if _visited is None: _visited = set()
    if shapeno in _visited: return []
    _visited = _visited | {shapeno}

    data = get_shape_data(shapeno, all_shapes_list, all_raws)
    if not data: return []

    pts   = []
    stack = []
    x = y = 0.0
    draw_on = False
    pen_up  = True

    def emit_move(nx, ny):
        nonlocal x, y, pen_up
        x, y = nx, ny; pen_up = True

    def emit_draw(nx, ny):
        nonlocal x, y, pen_up
        if pen_up:
            pts.append((x, y, True))
            pen_up = False
        pts.append((nx, ny, False))
        x, y = nx, ny

    def step(nx, ny):
        if draw_on: emit_draw(nx, ny)
        else:       emit_move(nx, ny)

    i = 0
    while i < len(data):
        op = data[i]; i += 1

        if op == 0x00: break

        elif op == 0x01: draw_on = True
        elif op == 0x02: draw_on = False; pen_up = True

        elif op == 0x03:
            if i < len(data): n=data[i]; i+=1
            if n: sc /= n
        elif op == 0x04:
            if i < len(data): n=data[i]; i+=1; sc *= n

        elif op == 0x05:
            stack.append((x, y, sc, draw_on, pen_up))
        elif op == 0x06:
            if stack: x, y, sc, draw_on, pen_up = stack.pop()

        elif op == 0x07:
            if i+2 > len(data): break
            lo=data[i]; hi=data[i+1]; i+=2
            subno = lo | (hi << 8)
            if subno == 0: continue
            sub = render_shape(subno, all_shapes_list, all_raws,
                               sc, depth+1, _visited)
            for sx, sy, spu in sub:
                pts.append((x+sx, y+sy, spu))
            if sub:
                lx, ly, _ = sub[-1]
                x += lx; y += ly
            pen_up = True

        elif op == 0x08:
            if i+2 > len(data): break
            dx = data[i]-256 if data[i]>=128 else data[i]; i+=1
            dy = data[i]-256 if data[i]>=128 else data[i]; i+=1
            step(x+dx*sc, y+dy*sc)

        elif op == 0x09:
            while i+2 <= len(data):
                dx = data[i]-256 if data[i]>=128 else data[i]; i+=1
                dy = data[i]-256 if data[i]>=128 else data[i]; i+=1
                if dx==0 and dy==0: break
                step(x+dx*sc, y+dy*sc)

        elif op == 0x0A:
            if i+2 > len(data): break
            r=data[i]*sc; i+=1
            ob=data[i]; i+=1
            a0_oct=(ob>>4)&7; n_octs=ob&0xF
            if n_octs==0: n_octs=8
            ccw = not bool(ob&0x80)
            a0=a0_oct*math.pi/4
            span=n_octs*math.pi/4*(1 if ccw else -1)
            cx_=x-r*math.cos(a0); cy_=y-r*math.sin(a0)
            segs=n_octs*4
            for k in range(1,segs+1):
                ang=a0+k/segs*span
                step(cx_+r*math.cos(ang), cy_+r*math.sin(ang))

        elif op == 0x0B: i=min(i+4,len(data))
        elif op == 0x0C: pass   # skip-if-NOT-vertical → execute in horiz = NOP
        elif op == 0x0D: i=skip_cmd(data,i)   # skip if vertical → skip in horiz
        elif op == 0x0E: i=skip_cmd(data,i)   # skip if horiz → ALWAYS skip
        elif op == 0x0F: pass   # skip if vertical → NOP in horiz

        elif op >= 0x10:
            L=(op>>4)&0xF; N=op&0xF
            dx,dy = DIR16[N]
            step(x+dx*L*sc, y+dy*L*sc)

    return pts

# ══════════════════════════════════════════════════════════════
# 主程式
# ══════════════════════════════════════════════════════════════
def main():
    if len(sys.argv) < 3:
        print('用法: python shx_render_diag.py extfont.shx extfont2.shx')
        sys.exit(1)

    s1, raw1 = load_index(sys.argv[1])
    s2, raw2 = load_index(sys.argv[2])
    all_shapes = [s1, s2]
    all_raws   = [raw1, raw2]
    print(f'載入: {sys.argv[1]} ({len(s1)} shapes), {sys.argv[2]} ({len(s2)} shapes)')

    # ── A. 渲染已知漢字，顯示實際座標範圍 ───────────────────────
    print('\n' + '='*70)
    print('[A] 渲染樣本字：實際座標範圍（含子形狀）')
    print('='*70)
    samples = [
        ('亜', 0x889F), ('唖', 0x88A0), ('娃', 0x88A1),
        ('阿', 0x88A2), ('哀', 0x88A3), ('愛', 0x88A4),
        ('一', 0x88EA), ('二', 0x93F1), ('三', 0x8E4F),
        ('人', 0x906C), ('口', 0x8CFB), ('山', 0x8E52),
        ('ペ', 0x8369), ('あ', 0x82A0), ('ア', 0x8340),
        ('、', 0x8141), ('。', 0x8142),
    ]
    print(f'  {"ch":>2}  {"sjis":>6}  {"pts":>5}  {"draw":>5}  '
          f'{"xmin":>6} {"xmax":>6}  {"ymin":>6} {"ymax":>6}  '
          f'{"width":>6} {"height":>6}')
    print('  ' + '-'*72)

    all_widths=[]; all_ymins=[]; all_ymaxs=[]

    for ch, sjis in samples:
        pts = render_shape(sjis, all_shapes, all_raws)
        if not pts:
            print(f'  {ch:>2}  {sjis:06X}  (no data)')
            continue
        draw_pts = [p for p in pts if not p[2]]
        if not draw_pts:
            print(f'  {ch:>2}  {sjis:06X}  {len(pts):5d}  {0:5d}  '
                  f'(move-only, no strokes)')
            continue
        xs=[p[0] for p in pts]; ys=[p[1] for p in pts]
        xmin,xmax = min(xs),max(xs)
        ymin,ymax = min(ys),max(ys)
        w=xmax-xmin; h=ymax-ymin
        print(f'  {ch:>2}  {sjis:06X}  {len(pts):5d}  {len(draw_pts):5d}  '
              f'{xmin:6.1f} {xmax:6.1f}  {ymin:6.1f} {ymax:6.1f}  '
              f'{w:6.1f} {h:6.1f}')
        if sjis >= 0x889F:   # kanji only
            all_widths.append(xmax-xmin)
            all_ymins.append(ymin)
            all_ymaxs.append(ymax)

    # ── B. 統計 200 個漢字的座標分佈 ──────────────────────────
    print('\n' + '='*70)
    print('[B] 200 個漢字渲染統計（含子形狀的真實座標）')
    print('='*70)
    wxs=[]; ymins=[]; ymaxs=[]; xmins=[]; xmaxs=[]
    count=0
    for sjis in sorted(s1):
        hi=(sjis>>8)&0xFF
        if not (0x88<=hi<=0x9F or 0xE0<=hi<=0xE5): continue
        pts=render_shape(sjis, all_shapes, all_raws)
        draw_pts=[p for p in pts if not p[2]]
        if len(draw_pts)<3: continue
        xs=[p[0] for p in pts]; ys=[p[1] for p in pts]
        xmins.append(min(xs)); xmaxs.append(max(xs))
        ymins.append(min(ys)); ymaxs.append(max(ys))
        wxs.append(max(xs)-min(xs))
        count+=1
        if count>=200: break

    if wxs:
        import statistics
        def pct(lst, p): return sorted(lst)[int(len(lst)*p/100)]
        print(f'  樣本數: {count}')
        print(f'  {"":12s}  {"min":>8}  {"p10":>8}  {"p50":>8}  {"p90":>8}  {"max":>8}')
        print('  ' + '-'*56)
        for name, lst in [('xmin',xmins),('xmax',xmaxs),
                           ('ymin',ymins),('ymax',ymaxs),('width',wxs)]:
            print(f'  {name:<12}  {min(lst):8.1f}  {pct(lst,10):8.1f}  '
                  f'{statistics.median(lst):8.1f}  {pct(lst,90):8.1f}  {max(lst):8.1f}')

        med_xmin = statistics.median(xmins)
        med_xmax = statistics.median(xmaxs)
        med_ymin = statistics.median(ymins)
        med_ymax = statistics.median(ymaxs)
        med_w    = statistics.median(wxs)

        print(f'\n  推算正規化參數：')
        print(f'    xmin_typical ≈ {med_xmin:.1f}')
        print(f'    xmax_typical ≈ {med_xmax:.1f}')
        print(f'    ymin_typical ≈ {med_ymin:.1f}  →  below ≈ {-med_ymin:.1f}')
        print(f'    ymax_typical ≈ {med_ymax:.1f}  →  above ≈ {med_ymax:.1f}')
        print(f'    width_typical≈ {med_w:.1f}    →  advance ≈ {med_w:.1f}')
        print(f'\n    X 軸偏移量 (x_origin): {-med_xmin:.1f}')
        print(f'    → xi = (x + {-med_xmin:.1f}) / {med_w:.1f} * 32')

    # ── C. 子形狀 0xFD00 分析 ─────────────────────────────────
    print('\n' + '='*70)
    print('[C] 子形狀樣本分析（0xFD00, 0xFD01, 0xFE00 等）')
    print('='*70)
    for subno in [0xFD00, 0xFD01, 0xFD02, 0xFE00, 0xFE01, 0xFE52]:
        pts = render_shape(subno, all_shapes, all_raws)
        draw_pts = [p for p in pts if not p[2]]
        if not pts:
            print(f'  {subno:#06x}: not found')
            continue
        xs=[p[0] for p in pts]; ys=[p[1] for p in pts]
        xmin,xmax=min(xs),max(xs); ymin,ymax=min(ys),max(ys)
        data = get_shape_data(subno, all_shapes, all_raws)
        print(f'  {subno:#06x}: pts={len(pts)} draw={len(draw_pts)} '
              f'x=[{xmin:.1f}..{xmax:.1f}] y=[{ymin:.1f}..{ymax:.1f}]  '
              f'raw[0:8]={data[:8].hex(" ")}')

    # ── D. 文字 IP 值預覽（愛、一、口） ──────────────────────
    print('\n' + '='*70)
    print('[D] 以不同 above/below/advance 測試 IP 值輸出')
    print('='*70)

    def encode_test(pts, above, below, adv, x_origin=0):
        if not pts or adv<=0: return []
        total_y = above+below or adv
        grid=[]; prev=None
        for x,y,pu in pts:
            xi=max(0,min(32,round((x+x_origin)/adv*32)))
            yi=max(0,min(32,round((y+below)/total_y*32)))
            if pu:
                grid.append((xi,yi,True)); prev=None
            else:
                k=(xi,yi)
                if k!=prev: grid.append((xi,yi,False)); prev=k
        clean=[]
        for k in range(len(grid)):
            xi,yi,pu=grid[k]
            if pu:
                if k+1<len(grid) and not grid[k+1][2]:
                    clean.append((xi,yi,True))
            else:
                clean.append((xi,yi,False))
        return [(2 if pu else 1)*10000+xi*100+yi for xi,yi,pu in clean]

    test_chars_d = [('愛',0x88A4), ('口',0x8CFB), ('人',0x906C), ('一',0x88EA)]
    for ch, sjis in test_chars_d:
        pts = render_shape(sjis, all_shapes, all_raws)
        if not pts:
            print(f'  {ch}: no data'); continue
        xs=[p[0] for p in pts]; ys=[p[1] for p in pts]
        xmin,xmax=min(xs),max(xs); ymin,ymax=min(ys),max(ys)
        print(f'\n  {ch} ({sjis:04X}): raw x=[{xmin:.1f}..{xmax:.1f}] y=[{ymin:.1f}..{ymax:.1f}]')

        # Try multiple parameter sets
        configs = [
            ('above=21 below=7  adv=28  xorg=0 ',  21,  7, 28,  0),
            ('above=21 below=7  adv=18  xorg=9 ',  21,  7, 18,  9),
            ('above=14 below=14 adv=28  xorg=14',  14, 14, 28, 14),
            ('above=21 below=7  adv=28  xorg=14',  21,  7, 28, 14),
            # Auto-derived from actual range
            (f'above={-ymin:.0f} below=0 adv={xmax-xmin:.0f} xorg={-xmin:.0f}',
             -ymin, 0, max(xmax-xmin,1), -xmin),
        ]
        for desc, a, b, adv, xorg in configs:
            ips = encode_test(pts, a, b, adv, xorg)
            draw_count = sum(1 for v in ips if v < 20000)
            print(f'    [{desc}]: {len(ips):3d} IPs, {draw_count} draw pts')
            if ips:
                preview = str(ips[:6])[:-1] + ', ...]'
                print(f'      → {preview}')

    # ── E. shape 0 完整解碼 ────────────────────────────────────
    print('\n' + '='*70)
    print('[E] shape 0 完整操作碼解碼（確認非度量值）')
    print('='*70)
    for label, shapes, raw in [('extfont',s1,raw1),('extfont2',s2,raw2)]:
        if 0 not in shapes: continue
        off, db = shapes[0]
        data = raw[off:off+db]
        print(f'\n  [{label}] defbytes={db}')
        print(f'  raw: {data.hex(" ")}')
        print(f'  decode:')
        i=0
        DNAMES=['E','ENE','NE','NNE','N','NNW','NW','WNW',
                'W','WSW','SW','SSW','S','SSE','SE','ESE']
        while i<len(data):
            b=data[i]; i+=1
            if   b==0: print(f'    END'); break
            elif b==1: print(f'    DrawON')
            elif b==2: print(f'    DrawOFF')
            elif b==3:
                n=data[i] if i<len(data) else 0; i+=1
                print(f'    scale÷{n}')
            elif b==4:
                n=data[i] if i<len(data) else 0; i+=1
                print(f'    scale×{n}')
            elif b==5: print(f'    PUSH')
            elif b==6: print(f'    POP')
            elif b==7:
                lo=data[i]; hi=data[i+1] if i+1<len(data) else 0; i+=2
                print(f'    CALL({lo|(hi<<8):#06x})')
            elif b==8:
                dx=data[i]-256 if data[i]>=128 else data[i]; i+=1
                dy=data[i]-256 if data[i]>=128 else data[i]; i+=1
                print(f'    XY({dx:+d},{dy:+d})')
            elif b==0x0E:
                print(f'    SKIP-NEXT-IF-HORIZ')
                i=skip_cmd(data,i)
            elif b==0x0F:
                print(f'    SKIP-NEXT-IF-VERT (NOP in horiz)')
            elif b>=0x10:
                L=(b>>4)&0xF; N=b&0xF
                print(f'    VEC(len={L}, dir={DNAMES[N]})')
            else:
                print(f'    opcode 0x{b:02X}')

if __name__ == '__main__':
    main()