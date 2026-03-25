import math

def entropy(block):
    freq = [0]*256
    for b in block:
        freq[b] += 1

    ent = 0
    size = len(block)
    for f in freq:
        if f == 0:
            continue
        p = f / size
        ent -= p * math.log2(p)
    return ent


def scan_entropy(data):
    print("[SCAN entropy...]")

    window = 256
    results = []

    for i in range(0, len(data) - window, 128):
        block = data[i:i+window]
        e = entropy(block)
        results.append((i, e))

    # 顯示 entropy 高的區域
    results.sort(key=lambda x: -x[1])

    print("\n=== TOP entropy regions ===")
    for r in results[:10]:
        print(f"offset={r[0]} entropy={r[1]:.3f}")

    return results


def main():
    with open("extfont.shx", "rb") as f:
        data = f.read()

    results = scan_entropy(data)

    # dump 第一個高 entropy 區
    off = results[0][0]

    print(f"\n=== DUMP @ {off} ===")
    chunk = data[off:off+128]
    print(" ".join(f"{b:02X}" for b in chunk))


if __name__ == "__main__":
    main()