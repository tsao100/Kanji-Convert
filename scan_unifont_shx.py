SHX_FILE = "simplex.shx"
START_OFFSET = 0x4F


def parse_shx_table(shx_file):
    results = []

    with open(shx_file, "rb") as f:
        data = f.read()

    offset = START_OFFSET

    while offset + 4 <= len(data):
        # 讀 unicode + length
        code = int.from_bytes(data[offset:offset+2], "little")
        length = int.from_bytes(data[offset+2:offset+4], "little")

        data_start = offset + 4
        data_end = data_start + length

        # 防止爆掉
        if data_end > len(data):
            print(f"[WARN] 超出檔案範圍 code=0x{code:04X}")
            break

        glyph_data = data[data_start:data_end]

        results.append((code, offset, length, glyph_data))

        # Debug
        # print(f"[DEBUG] code=0x{code:04X} len={length} next=0x{data_end:08X}")

        offset = data_end

    return results


if __name__ == "__main__":
    table = parse_shx_table(SHX_FILE)

    for code, offset, length, _ in table:
        print(f"code=0x{code:04X} start=0x{offset:08X} length={length}")