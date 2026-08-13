#!/usr/bin/env python3
"""Vygeneruje QR kód pre dobrovoľný príspevok a vloží ho do web/index.html.

Kóduje reťazec SPAYD (česká QR platba) do QR kódu a výsledok zapíše ako
inline SVG medzi značky QR-START / QR-END v index.html. Inline preto, lebo
stránka je jeden statický súbor bez závislostí — nechceme ďalší request ani
externý generátor obrázkov.

Použitie:
    python tools/make_support_qr.py --iban CZ6508000000192000145399 \\
        --amount 25 --currency CZK --message "Cinema watcher"

Skript je čistá stdlib: vlastný enkodér QR (bajtový režim, úroveň korekcie M).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# --- Tabuľky QR ------------------------------------------------------------

# Pre každú verziu (index = verzia - 1) pri úrovni korekcie M:
# (počet dátových kódových slov, EC slov na blok, [(počet blokov, dát na blok)])
RS_BLOCKS_M = [
    (16, 10, [(1, 16)]),
    (28, 16, [(1, 28)]),
    (44, 26, [(1, 44)]),
    (64, 18, [(2, 32)]),
    (86, 24, [(2, 43)]),
    (108, 16, [(4, 27)]),
    (124, 18, [(4, 31)]),
    (154, 22, [(2, 38), (2, 39)]),
    (182, 22, [(3, 36), (2, 37)]),
    (216, 26, [(4, 43), (1, 44)]),
]

# Stredy zarovnávacích vzorov pre verzie 1–10.
ALIGN_CENTERS = [
    [],
    [6, 18],
    [6, 22],
    [6, 26],
    [6, 30],
    [6, 34],
    [6, 22, 38],
    [6, 24, 42],
    [6, 26, 46],
    [6, 28, 50],
]

# Zvyšné bity za poslednou kódovou slabikou (verzie 1–10).
REMAINDER_BITS = [0, 7, 7, 7, 7, 7, 0, 0, 0, 0]


# --- Galoisove teleso GF(256) ---------------------------------------------

GF_EXP = [0] * 512
GF_LOG = [0] * 256


def _init_gf() -> None:
    x = 1
    for i in range(255):
        GF_EXP[i] = x
        GF_LOG[x] = i
        x <<= 1
        if x & 0x100:
            x ^= 0x11D
    for i in range(255, 512):
        GF_EXP[i] = GF_EXP[i - 255]


_init_gf()


def gf_mul(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return GF_EXP[GF_LOG[a] + GF_LOG[b]]


def rs_generator(degree: int) -> list[int]:
    """Generátorový polynóm Reed-Solomon pre daný počet EC slov."""
    poly = [1]
    for i in range(degree):
        nxt = [0] * (len(poly) + 1)
        for j, coef in enumerate(poly):
            nxt[j] ^= gf_mul(coef, 1)
            nxt[j + 1] ^= gf_mul(coef, GF_EXP[i])
        poly = nxt
    return poly


def rs_encode(data: list[int], ec_len: int) -> list[int]:
    gen = rs_generator(ec_len)
    rem = [0] * ec_len
    for byte in data:
        factor = byte ^ rem[0]
        rem = rem[1:] + [0]
        for i, coef in enumerate(gen[1:]):
            rem[i] ^= gf_mul(coef, factor)
    return rem


# --- Kódovanie dát ---------------------------------------------------------


def pick_version(payload: bytes) -> int:
    """Najmenšia verzia (1–10), do ktorej sa dáta zmestia pri úrovni M."""
    for version in range(1, 11):
        count_bits = 8 if version < 10 else 16
        needed = 4 + count_bits + 8 * len(payload)
        if needed <= RS_BLOCKS_M[version - 1][0] * 8:
            return version
    raise SystemExit(
        f"Reťazec má {len(payload)} B — na to by bola potrebná verzia QR > 10, "
        "ktorú tento skript nepozná. Skráť správu (MSG)."
    )


def build_codewords(payload: bytes, version: int) -> list[int]:
    """Bajty správy -> dátové kódové slová vrátane hlavičky a výplne."""
    total_data = RS_BLOCKS_M[version - 1][0]
    count_bits = 8 if version < 10 else 16

    bits: list[int] = []

    def put(value: int, length: int) -> None:
        for i in range(length - 1, -1, -1):
            bits.append((value >> i) & 1)

    put(0b0100, 4)  # bajtový režim
    put(len(payload), count_bits)
    for byte in payload:
        put(byte, 8)

    # Ukončovač (max 4 bity) a zarovnanie na celé slovo.
    put(0, min(4, total_data * 8 - len(bits)))
    if len(bits) % 8:
        put(0, 8 - len(bits) % 8)

    codewords = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    # Striedavá výplň predpísaná normou.
    for i in range(total_data - len(codewords)):
        codewords.append(0xEC if i % 2 == 0 else 0x11)
    return codewords


def interleave(codewords: list[int], version: int) -> list[int]:
    """Rozdelí dáta na bloky, doplní EC a poprepletá ich podľa normy."""
    _, ec_len, groups = RS_BLOCKS_M[version - 1]

    data_blocks: list[list[int]] = []
    pos = 0
    for count, per_block in groups:
        for _ in range(count):
            data_blocks.append(codewords[pos:pos + per_block])
            pos += per_block

    ec_blocks = [rs_encode(block, ec_len) for block in data_blocks]

    out: list[int] = []
    for i in range(max(len(b) for b in data_blocks)):
        for block in data_blocks:
            if i < len(block):
                out.append(block[i])
    for i in range(ec_len):
        for block in ec_blocks:
            out.append(block[i])
    return out


# --- Kreslenie matice ------------------------------------------------------


def new_matrix(version: int):
    size = version * 4 + 17
    modules = [[0] * size for _ in range(size)]
    reserved = [[False] * size for _ in range(size)]
    return modules, reserved, size


def place_function_patterns(modules, reserved, version, size) -> None:
    def finder(row: int, col: int) -> None:
        for dr in range(-1, 8):
            for dc in range(-1, 8):
                r, c = row + dr, col + dc
                if not (0 <= r < size and 0 <= c < size):
                    continue
                edge = max(abs(dr - 3), abs(dc - 3))
                modules[r][c] = 1 if edge in (0, 1, 3) else 0
                reserved[r][c] = True

    finder(0, 0)
    finder(0, size - 7)
    finder(size - 7, 0)

    # Časovacie vzory.
    for i in range(size):
        if not reserved[6][i]:
            modules[6][i] = 1 - i % 2
            reserved[6][i] = True
        if not reserved[i][6]:
            modules[i][6] = 1 - i % 2
            reserved[i][6] = True

    # Zarovnávacie vzory (nie tam, kde už sedia hľadáčiky).
    centers = ALIGN_CENTERS[version - 1]
    for r in centers:
        for c in centers:
            if (r < 8 and c < 8) or (r < 8 and c > size - 9) or (r > size - 9 and c < 8):
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    modules[r + dr][c + dc] = 1 if max(abs(dr), abs(dc)) != 1 else 0
                    reserved[r + dr][c + dc] = True

    # Miesto pre formátovú informáciu.
    for i in range(9):
        if not reserved[8][i]:
            reserved[8][i] = True
        if not reserved[i][8]:
            reserved[i][8] = True
    for i in range(8):
        reserved[8][size - 1 - i] = True
        reserved[size - 1 - i][8] = True

    # Tmavý modul — vždy 1.
    modules[size - 8][8] = 1
    reserved[size - 8][8] = True

    # Verzia sa zapisuje až od verzie 7.
    if version >= 7:
        bits = version_bits(version)
        for i in range(18):
            bit = (bits >> i) & 1
            r, c = i // 3, size - 11 + i % 3
            modules[r][c] = bit
            reserved[r][c] = True
            modules[c][r] = bit
            reserved[c][r] = True


def version_bits(version: int) -> int:
    """BCH(18,6) pre číslo verzie."""
    rem = version
    for _ in range(12):
        rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
    return (version << 12) | rem


def format_bits(mask: int) -> int:
    """BCH(15,5) pre úroveň korekcie M (0b00) a číslo masky."""
    data = (0b00 << 3) | mask
    rem = data
    for _ in range(10):
        rem = (rem << 1) ^ ((rem >> 9) * 0x537)
    return ((data << 10) | rem) ^ 0x5412


def place_data(modules, reserved, bits, size) -> None:
    idx = 0
    col = size - 1
    upward = True
    while col > 0:
        if col == 6:  # stĺpec časovacieho vzoru sa preskakuje
            col -= 1
        rows = range(size - 1, -1, -1) if upward else range(size)
        for row in rows:
            for c in (col, col - 1):
                if reserved[row][c]:
                    continue
                modules[row][c] = bits[idx] if idx < len(bits) else 0
                idx += 1
        upward = not upward
        col -= 2


def apply_mask(modules, reserved, mask: int, size):
    out = [row[:] for row in modules]
    for r in range(size):
        for c in range(size):
            if reserved[r][c]:
                continue
            if mask == 0:
                flip = (r + c) % 2 == 0
            elif mask == 1:
                flip = r % 2 == 0
            elif mask == 2:
                flip = c % 3 == 0
            elif mask == 3:
                flip = (r + c) % 3 == 0
            elif mask == 4:
                flip = (r // 2 + c // 3) % 2 == 0
            elif mask == 5:
                flip = (r * c) % 2 + (r * c) % 3 == 0
            elif mask == 6:
                flip = ((r * c) % 2 + (r * c) % 3) % 2 == 0
            else:
                flip = ((r + c) % 2 + (r * c) % 3) % 2 == 0
            if flip:
                out[r][c] ^= 1
    return out


def place_format(modules, mask: int, size) -> None:
    bits = format_bits(mask)
    for i in range(15):
        bit = (bits >> i) & 1
        # Kópia pri ľavom hornom hľadáčiku.
        if i < 6:
            modules[8][i] = bit
        elif i == 6:
            modules[8][7] = bit
        elif i == 7:
            modules[8][8] = bit
        elif i == 8:
            modules[7][8] = bit
        else:
            modules[14 - i][8] = bit
        # Rozdelená kópia pri zvyšných dvoch.
        if i < 8:
            modules[size - 1 - i][8] = bit
        else:
            modules[8][size - 15 + i] = bit


def penalty(modules, size) -> int:
    score = 0

    # 1) Súvislé série rovnakej farby.
    for line in list(modules) + [list(col) for col in zip(*modules)]:
        run, prev = 1, line[0]
        for value in line[1:]:
            if value == prev:
                run += 1
            else:
                if run >= 5:
                    score += run - 2
                run, prev = 1, value
        if run >= 5:
            score += run - 2

    # 2) Bloky 2×2.
    for r in range(size - 1):
        for c in range(size - 1):
            v = modules[r][c]
            if v == modules[r][c + 1] == modules[r + 1][c] == modules[r + 1][c + 1]:
                score += 3

    # 3) Vzor pripomínajúci hľadáčik.
    patterns = ([1, 0, 1, 1, 1, 0, 1, 0, 0, 0, 0], [0, 0, 0, 0, 1, 0, 1, 1, 1, 0, 1])
    for line in list(modules) + [list(col) for col in zip(*modules)]:
        for i in range(size - 10):
            window = line[i:i + 11]
            if window in patterns:
                score += 40

    # 4) Nevyváženosť tmavých a svetlých modulov.
    dark = sum(sum(row) for row in modules)
    ratio = dark * 100 // (size * size)
    score += 10 * (abs(ratio - 50) // 5)
    return score


def encode(payload: bytes):
    version = pick_version(payload)
    codewords = interleave(build_codewords(payload, version), version)

    bits: list[int] = []
    for word in codewords:
        bits.extend((word >> i) & 1 for i in range(7, -1, -1))
    bits.extend([0] * REMAINDER_BITS[version - 1])

    modules, reserved, size = new_matrix(version)
    place_function_patterns(modules, reserved, version, size)
    place_data(modules, reserved, bits, size)

    best = None
    for mask in range(8):
        candidate = apply_mask(modules, reserved, mask, size)
        place_format(candidate, mask, size)
        score = penalty(candidate, size)
        if best is None or score < best[0]:
            best = (score, candidate)
    return best[1], size, version


# --- Výstup ----------------------------------------------------------------


def to_svg(modules, size: int, quiet: int = 4) -> str:
    """Matica -> SVG s jedinou cestou (menšie ako mriežka <rect>)."""
    total = size + 2 * quiet
    parts = []
    for r, row in enumerate(modules):
        c = 0
        while c < size:
            if row[c]:
                start = c
                while c < size and row[c]:
                    c += 1
                parts.append(f"M{start + quiet} {r + quiet}h{c - start}v1h-{c - start}z")
            else:
                c += 1
    path = "".join(parts)
    return (
        f'<svg class="qr" viewBox="0 0 {total} {total}" '
        f'shape-rendering="crispEdges" role="img" '
        f'aria-label="QR kód na platbu"><rect width="{total}" height="{total}" '
        f'fill="#fff"/><path fill="#000" d="{path}"/></svg>'
    )


def spayd(iban: str, amount: str | None, currency: str, message: str | None) -> str:
    iban = iban.replace(" ", "").upper()
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{11,30}", iban):
        raise SystemExit(f"'{iban}' nevyzerá ako IBAN.")
    fields = [f"ACC:{iban}"]
    if amount:
        fields.append(f"AM:{float(amount):.2f}")
    fields.append(f"CC:{currency.upper()}")
    if message:
        # SPAYD nepovoľuje '*' ani diakritiku v hodnotách.
        clean = message.replace("*", " ").encode("ascii", "ignore").decode()
        fields.append(f"MSG:{clean.upper()}")
    return "SPD*1.0*" + "*".join(fields)


MARK_START = "<!-- QR-START -->"
MARK_END = "<!-- QR-END -->"


def inject(html_path: Path, svg: str) -> None:
    html = html_path.read_text(encoding="utf-8")
    if MARK_START not in html or MARK_END not in html:
        raise SystemExit(f"V {html_path} chýbajú značky {MARK_START} / {MARK_END}.")
    head, rest = html.split(MARK_START, 1)
    _, tail = rest.split(MARK_END, 1)
    html_path.write_text(
        f"{head}{MARK_START}\n        {svg}\n        {MARK_END}{tail}",
        encoding="utf-8",
    )


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--iban", required=True, help="IBAN účtu, napr. CZ65 0800 0000 1920 0014 5399")
    ap.add_argument("--amount", default=None, help="Predvyplnená suma, napr. 25 (bez = zadá si ju platca)")
    ap.add_argument("--currency", default="CZK")
    ap.add_argument("--message", default="Cinema watcher", help="Správa pre príjemcu")
    ap.add_argument(
        "--html",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "web" / "index.html",
    )
    ap.add_argument("--print", action="store_true", help="Vypíše SVG namiesto zápisu do stránky")
    args = ap.parse_args(argv)

    payload = spayd(args.iban, args.amount, args.currency, args.message)
    modules, size, version = encode(payload.encode("utf-8"))
    svg = to_svg(modules, size)

    if args.print:
        print(svg)
    else:
        inject(args.html, svg)
        print(f"Zapísané do {args.html}")
    print(f"Obsah QR: {payload}", file=sys.stderr)
    print(f"Verzia QR: {version} ({size}×{size} modulov)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
