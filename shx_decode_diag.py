#!/usr/bin/env python3
"""
shx_decode_diag.py  (fixed)
============================
修正清單：
  1. render() 的 0x08 handler 缺少邊界檢查 → IndexError
  2. decode_ops() 的 0x08 handler 同樣缺少邊界檢查
  3. render() 的 CALL(0x07) handler：座標累積邏輯錯誤
     子形狀從 (0,0) 出發，終點 (lx,ly) 即位移量，
     但原碼對 ALL sub-points 已加 (x,y) offset，
     所以 x+=lx; y+=ly 會重複累加 → 改為只更新 x,y 到最後絕對座標
  4. skip_cmd() 缺少邊界保護
  5. 0x09 XY-SEQ：render() 與 decode_ops() 終止條件不一致
  6. render() 的 0x0A arc 起點應先 move 到弧線起點再畫

用法：
  python shx_decode_diag.py extfont.shx extfont2.shx
"""
import sys, struct, math, statistics

# ─────────────────────────────────────────────────────────
# SHX 索引載入
# ─────────────────────────────────────────────────────────
def load_index(path):
    with open(path, 'rb') as f:
        raw = f.read()
    pos = raw.find(0x1A) + 1 + 2
    nshapes = struct.unpack_from('<H', raw, pos)[0]; pos += 2
    nranges = raw[pos]; pos += 1
    pos += nranges * 4 + 3
    data_start = pos + nshapes * 8
    shapes = {}
    p = pos
    for _ in range(nshapes):
        if p + 8 > len(raw): break
        db  = struct.unpack_from('<H', raw, p)[0]
        fof = struct.unpack_from('<I', raw, p + 2)[0]
        sno = struct.unpack_from('<H', raw, p + 6)[0]
        p += 8
        if db and (data_start + fof + db <= len(raw)):
            shapes[sno] = (data_start + fof, db)
    return shapes, raw

DIRS = ['E','ENE','NE','NNE','N','NNW','NW','WNW',
        'W','WSW','SW','SSW','S','SSE','SE','ESE']
DIR16 = [(1,0),(1,.5),(1,1),(.5,1),(0,1),(-.5,1),(-1,1),(-1,.5),
         (-1,0),(-1,-.5),(-1,-1),(-.5,-1),(0,-1),(.5,-1),(1,-1),(1,-.5)]

def get_data(sno, sl, rl):
    for s, r in zip(sl, rl):
        if sno in s:
            off, db = s[sno]
            return r[off:off + db]
    return b''

# ─────────────────────────────────────────────────────────
# 安全讀取 helpers
# ─────────────────────────────────────────────────────────
def _rd(data, i):
    """安全讀取一個 byte，越界回傳 0"""
    return data[i] if i < len(data) else 0

def _srd(data, i):
    """安全讀取一個有號 byte，越界回傳 0"""
    v = _rd(data, i)
    return v - 256 if v >= 128 else v

# ─────────────────────────────────────────────────────────
# skip_cmd（消耗下一條完整指令）
# ─────────────────────────────────────────────────────────
def skip_cmd(data, i):
    if i >= len(data): return i
    op = data[i]; i += 1
    if   op == 0x00: return i - 1          # END：不跳過，留給主迴圈處理
    elif op in (0x01, 0x02, 0x05, 0x06,
                0x0C, 0x0D, 0x0E, 0x0F):  pass
    elif op in (0x03, 0x04):               i = min(i + 1, len(data))
    elif op == 0x07:                        i = min(i + 2, len(data))
    elif op == 0x08:                        i = min(i + 2, len(data))
    elif op == 0x09:
        while i + 2 <= len(data):
            dx = data[i]; dy = data[i + 1]; i += 2
            if dx == 0 and dy == 0: break
    elif op == 0x0A:                        i = min(i + 2, len(data))
    elif op == 0x0B:                        i = min(i + 4, len(data))
    return i

# ─────────────────────────────────────────────────────────
# 操作碼解碼（文字說明用）
# ─────────────────────────────────────────────────────────
def decode_ops(data, indent=4):
    sp = ' ' * indent; lines = []; i = 0
    while i < len(data):
        b = data[i]; i += 1
        if b == 0x00:
            lines.append(f'{sp}00  END'); break
        elif b == 0x01: lines.append(f'{sp}01  DrawON')
        elif b == 0x02: lines.append(f'{sp}02  DrawOFF')
        elif b == 0x03:
            n = _rd(data, i); i += 1
            lines.append(f'{sp}03 {n:02X}  scale÷{n}')
        elif b == 0x04:
            n = _rd(data, i); i += 1
            lines.append(f'{sp}04 {n:02X}  scale×{n}')
        elif b == 0x05: lines.append(f'{sp}05  PUSH')
        elif b == 0x06: lines.append(f'{sp}06  POP')
        elif b == 0x07:
            lo = _rd(data, i); hi = _rd(data, i + 1); i += 2
            subno = lo | (hi << 8)
            lines.append(f'{sp}07 {lo:02X} {hi:02X}  CALL({subno:#06x})')
        elif b == 0x08:
            if i + 2 > len(data):
                lines.append(f'{sp}08  !! TRUNCATED (need 2 more bytes)')
                break
            dx = _srd(data, i); i += 1
            dy = _srd(data, i); i += 1
            lines.append(f'{sp}08 {data[i-2]:02X} {data[i-1]:02X}  XY({dx:+d},{dy:+d})')
        elif b == 0x09:
            lines.append(f'{sp}09  XY-SEQ:')
            while i + 2 <= len(data):
                dx = _srd(data, i); dy = _srd(data, i + 1); i += 2
                lines.append(f'{sp}      ({dx:+d},{dy:+d})')
                if dx == 0 and dy == 0: break
        elif b == 0x0A:
            if i + 2 > len(data):
                lines.append(f'{sp}0A  !! TRUNCATED'); break
            r = _rd(data, i); oc = _rd(data, i + 1); i += 2
            lines.append(f'{sp}0A {r:02X} {oc:02X}  ARC(r={r},oct={oc:#04x})')
        elif b == 0x0B:
            i = min(i + 4, len(data))
            lines.append(f'{sp}0B  FracARC(skip4)')
        elif b == 0x0C:
            lines.append(f'{sp}0C  ??? (skip-if-horiz OR exec-if-horiz)')
        elif b == 0x0D:
            lines.append(f'{sp}0D  ??? (skip-if-vert  OR exec-if-vert)')
        elif b == 0x0E:
            lines.append(f'{sp}0E  SKIP-NEXT-IF-HORIZ')
        elif b == 0x0F:
            lines.append(f'{sp}0F  SKIP-NEXT-IF-VERT(NOP in horiz)')
        elif b >= 0x10:
            L = (b >> 4) & 0xF; N = b & 0xF
            lines.append(f'{sp}{b:02X}  VEC(len={L}, dir={DIRS[N]})')
        else:
            lines.append(f'{sp}{b:02X}  ???')
    return lines

# ─────────────────────────────────────────────────────────
# render_shape
# ─────────────────────────────────────────────────────────
def render(sno, sl, rl, sc=1.0, depth=0, visited=None,
           mode_0C='nop', mode_0D='nop'):
    if depth > 8: return []
    if visited is None: visited = set()
    if sno in visited: return []
    visited = visited | {sno}

    data = get_data(sno, sl, rl)
    if not data: return []

    pts = []; stack = []; x = y = 0.0; draw_on = False; pen_up = True

    def move(nx, ny):
        nonlocal x, y, pen_up
        x, y = nx, ny; pen_up = True

    def draw(nx, ny):
        nonlocal x, y, pen_up
        if pen_up:
            pts.append((x, y, True)); pen_up = False
        pts.append((nx, ny, False)); x, y = nx, ny

    def step(nx, ny):
        if draw_on: draw(nx, ny)
        else: move(nx, ny)

    i = 0
    while i < len(data):
        b = data[i]; i += 1

        if b == 0x00:
            break
        elif b == 0x01:
            draw_on = True
        elif b == 0x02:
            draw_on = False; pen_up = True
        elif b == 0x03:
            n = _rd(data, i); i += 1
            if n: sc /= n
        elif b == 0x04:
            n = _rd(data, i); i += 1; sc *= n
        elif b == 0x05:
            stack.append((x, y, sc, draw_on, pen_up))
        elif b == 0x06:
            if stack: x, y, sc, draw_on, pen_up = stack.pop()
        elif b == 0x07:
            lo = _rd(data, i); hi = _rd(data, i + 1); i += 2
            subno = lo | (hi << 8)
            if subno == 0: continue
            # ── BUG FIX: 子形狀在自己的 (0,0) 空間渲染，
            #    終點 (lx,ly) 就是相對位移，加到當前 (x,y) 即可。
            #    不能直接把 sub 的絕對座標偏移後 append，
            #    因為子形狀本身已從 0,0 出發。
            sub = render(subno, sl, rl, sc, depth + 1, visited, mode_0C, mode_0D)
            for sx, sy, spu in sub:
                pts.append((x + sx, y + sy, spu))
            if sub:
                lx, ly, _ = sub[-1]
                x += lx; y += ly   # 正確：lx/ly 是子形狀自己空間的終點 = 位移
            pen_up = True
        elif b == 0x08:
            # ── BUG FIX: 加邊界檢查
            if i + 2 > len(data): break
            dx = _srd(data, i); i += 1
            dy = _srd(data, i); i += 1
            step(x + dx * sc, y + dy * sc)
        elif b == 0x09:
            while i + 2 <= len(data):
                dx = _srd(data, i); dy = _srd(data, i + 1); i += 2
                if dx == 0 and dy == 0: break
                step(x + dx * sc, y + dy * sc)
        elif b == 0x0A:
            if i + 2 > len(data): break
            r = _rd(data, i) * sc; i += 1
            ob = _rd(data, i); i += 1
            a0o = (ob >> 4) & 7
            no  = ob & 0x0F
            if no == 0: no = 8
            ccw = not bool(ob & 0x80)
            a0   = a0o * math.pi / 4
            span = no * math.pi / 4 * (1 if ccw else -1)
            cx_ = x - r * math.cos(a0)
            cy_ = y - r * math.sin(a0)
            # 先移到弧線起點（保持 draw 狀態）
            for k in range(1, no * 4 + 1):
                ang = a0 + k / (no * 4) * span
                step(cx_ + r * math.cos(ang), cy_ + r * math.sin(ang))
        elif b == 0x0B:
            i = min(i + 4, len(data))
        elif b == 0x0C:
            if mode_0C == 'skip_horiz':
                i = skip_cmd(data, i)
        elif b == 0x0D:
            if mode_0D == 'skip_horiz':
                i = skip_cmd(data, i)
        elif b == 0x0E:
            i = skip_cmd(data, i)   # 水平模式下跳過下一指令
        elif b == 0x0F:
            pass                     # 水平模式下 NOP
        elif b >= 0x10:
            L = (b >> 4) & 0xF; N = b & 0xF
            ddx, ddy = DIR16[N]
            step(x + ddx * L * sc, y + ddy * L * sc)
        # else: 未知操碼，忽略
    return pts

# ─────────────────────────────────────────────────────────
# 統計摘要
# ─────────────────────────────────────────────────────────
def pts_summary(pts):
    if not pts: return 'EMPTY'
    dp = [p for p in pts if not p[2]]
    if not dp: return f'pts={len(pts)} draw=0 (moves only)'
    xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
    return (f'pts={len(pts)} draw={len(dp)}  '
            f'x=[{min(xs):.1f}..{max(xs):.1f}]  '
            f'y=[{min(ys):.1f}..{max(ys):.1f}]')

# ─────────────────────────────────────────────────────────
# 主程式
# ─────────────────────────────────────────────────────────
def main():
    if len(sys.argv) < 3:
        print('用法: python shx_decode_diag.py extfont.shx extfont2.shx')
        sys.exit(1)

    s1, r1 = load_index(sys.argv[1])
    s2, r2 = load_index(sys.argv[2])
    sl = [s1, s2]; rl = [r1, r2]

    # ── A. 關鍵形狀完整解碼 ──────────────────────────────
    print('=' * 70)
    print('[A] 關鍵形狀完整操作碼解碼（含 defbytes）')
    print('=' * 70)
    targets = [
        ('亜', 0x889F), ('唖', 0x88A0), ('娃', 0x88A1),
        ('阿', 0x88A2), ('一', 0x88EA), ('口', 0x8CFB),
        ('人', 0x906C), ('山', 0x8E52), ('田', 0x9363),
    ]
    for ch, sno in targets:
        data = get_data(sno, sl, rl)
        found = False
        for lab, s in [('ext1', s1), ('ext2', s2)]:
            if sno in s:
                off, db = s[sno]
                print(f'\n  {ch}({sno:#06x}) [{lab}]  defbytes={db}  '
                      f'raw={data.hex(" ")}')
                for line in decode_ops(data):
                    print(line)
                found = True; break
        if not found:
            print(f'\n  {ch}({sno:#06x})  NOT FOUND')

    # ── B. 子形狀解碼 ─────────────────────────────────────
    print('\n' + '=' * 70)
    print('[B] 子形狀解碼（0xFD00-0xFD13, 0xFE 系列）')
    print('=' * 70)
    fd_shapes = sorted(sno for s in sl for sno in s
                       if (sno >> 8) & 0xFF in (0xFD, 0xFE))[:20]
    for sno in fd_shapes:
        data = get_data(sno, sl, rl)
        print(f'\n  {sno:#06x}  defbytes={len(data)}  raw[0:16]={data[:16].hex(" ")}')
        for line in decode_ops(data):
            print(line)

    # ── C. 0x0C/0x0D 語義比較 ────────────────────────────
    print('\n' + '=' * 70)
    print('[C] 0x0C / 0x0D 語義比較（4種組合 × 8個字）')
    print('=' * 70)
    combos = [
        ('0C=nop  0D=nop  ',  'nop',        'nop'),
        ('0C=skip 0D=nop  ',  'skip_horiz', 'nop'),
        ('0C=nop  0D=skip ',  'nop',        'skip_horiz'),
        ('0C=skip 0D=skip ',  'skip_horiz', 'skip_horiz'),
    ]
    test_chars = [
        ('亜', 0x889F), ('唖', 0x88A0), ('阿', 0x88A2),
        ('一', 0x88EA), ('口', 0x8CFB), ('人', 0x906C),
        ('田', 0x9363), ('山', 0x8E52),
    ]
    for desc, mc, md in combos:
        print(f'\n  [{desc}]')
        for ch, sno in test_chars:
            pts = render(sno, sl, rl, mode_0C=mc, mode_0D=md)
            print(f'    {ch}({sno:#06x}): {pts_summary(pts)}')

    # ── D. 200 字座標統計 ─────────────────────────────────
    print('\n' + '=' * 70)
    print('[D] 200 字座標統計（測試各 0x0C/0x0D 組合）')
    print('=' * 70)
    kanji_snos = [sno for sno in sorted(s1)
                  if (sno >> 8) & 0xFF in range(0x88, 0xA0)
                  or (sno >> 8) & 0xFF in range(0xE0, 0xE6)][:200]

    best_combo = None; best_valid = -1

    for desc, mc, md in combos:
        xmins = []; xmaxs = []; ymins = []; ymaxs = []; ok = 0
        for sno in kanji_snos:
            try:
                pts = render(sno, sl, rl, mode_0C=mc, mode_0D=md)
            except Exception as e:
                print(f'    [WARN] sno={sno:#06x} raised: {e}')
                continue
            dp = [p for p in pts if not p[2]]
            if len(dp) < 2: continue
            xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
            xmins.append(min(xs)); xmaxs.append(max(xs))
            ymins.append(min(ys)); ymaxs.append(max(ys)); ok += 1

        if not xmaxs:
            print(f'  [{desc}]: no valid chars'); continue

        med = statistics.median
        width_med = med([x2 - x1 for x1, x2 in zip(xmins, xmaxs)])
        print(f'  [{desc}]: valid={ok}/{len(kanji_snos)}  '
              f'xmin_med={med(xmins):5.1f}  xmax_med={med(xmaxs):5.1f}  '
              f'ymin_med={med(ymins):5.1f}  ymax_med={med(ymaxs):5.1f}  '
              f'width_med={width_med:5.1f}')

        if ok > best_valid:
            best_valid = ok
            best_combo = (mc, md, med(xmins), med(xmaxs),
                          med(ymins), med(ymaxs), width_med)

    # ── E. 推薦正規化參數 ─────────────────────────────────
    print('\n' + '=' * 70)
    print('[E] 推薦正規化參數（valid 字數最多的組合）')
    print('=' * 70)
    if best_combo:
        best_mc, best_md, med_xmin, med_xmax, med_ymin, med_ymax, med_w = best_combo
        x_orig = -med_xmin; adv = med_xmax - med_xmin
        print(f'  使用: 0C={best_mc}  0D={best_md}')
        print(f'  xmin p50={med_xmin:.1f}  xmax p50={med_xmax:.1f}')
        print(f'  ymin p50={med_ymin:.1f}  ymax p50={med_ymax:.1f}')
        print(f'  width p50={med_w:.1f}')
        print(f'\n  若以 x_origin=-xmin_med, advance=xmax_med-xmin_med:')
        print(f'    x_origin ≈ {x_orig:.1f}')
        print(f'    advance  ≈ {adv:.1f}')
        print(f'    above    ≈ {med_ymax:.1f}  (ymax_med)')
        print(f'    below    ≈ {-med_ymin:.1f}  (|ymin_med|)')
        print(f'\n  建議 make_kandat.py 設定:')
        print(f'    X_ORIGIN = {round(x_orig)}')
        print(f'    ADVANCE  = {round(adv)}')
        print(f'    ABOVE    = {round(med_ymax)}')
        print(f'    BELOW    = {round(-med_ymin)}')
    else:
        best_mc, best_md = 'nop', 'skip_horiz'  # fallback

    # ── F. 前 20 字渲染結果 ───────────────────────────────
    print('\n' + '=' * 70)
    print('[F] 前 20 個 JIS level-1 漢字渲染結果（最佳組合）')
    print('=' * 70)
    for sjis in [0x889F + i for i in range(20)]:
        try:
            ch = bytes([sjis >> 8, sjis & 0xFF]).decode('cp932')
        except Exception:
            ch = '?'
        pts = render(sjis, sl, rl, mode_0C=best_mc, mode_0D=best_md)
        print(f'  {ch}({sjis:04X}): {pts_summary(pts)}')

if __name__ == '__main__':
    main()