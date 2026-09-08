"""Kopiuje oryginalne (nieprzyciete) zdjecia do nowego folderu pod nazwa z kodem
zamowienia, na podstawie nazw plikow w folderze z wycinkami.

Wycinki maja nazwy w postaci "KOD_DSC0002.jpg" albo (dla niepewnych)
"sprawdz_DSC0024.jpg" / "_DSC0001_DO-SPRAWDZENIA.jpg" - te drugie sa pomijane,
bo nie maja przypisanego kodu.

Dla kazdego rozpoznanego wycinka szuka oryginalu o tej samej koncowce DSCxxxx
(dowolne rozszerzenie, bez rozroznienia wielkosci liter) w folderze zrodlowym
i kopiuje go 1:1 (bez zadnego przycinania) do folderu wyjsciowego jako
"KOD_DSCxxxx.<oryginalne rozszerzenie>".

Gdy kilka wycinkow ma ten sam numer DSC (nie powinno sie zdarzyc przy tym
sposobie nazywania) albo dwa rozne kody wskazuja na ten sam oryginal,
plik jest zgłaszany i pomijany - nic nie nadpisuje sie po cichu.

Uzycie:
    python3 Deposkan/przenies_oryginaly.py \
        "Deposkan/wycinki" "Deposkan/02.09.2026" "Deposkan/oryginaly_wg_kodu"
"""
import re
import shutil
import sys
from pathlib import Path
from collections import defaultdict

WZORZEC_KOD = re.compile(r"^(?P<kod>.+)_(?P<dsc>DSC\d+)\.[^.]+$", re.IGNORECASE)
NIEPEWNE_PREFIKSY = {"sprawdz"}  # te "kody" oznaczaja zdjecia bez rozpoznanego numeru
WZORZEC_DSC = re.compile(r"(DSC\d+)", re.IGNORECASE)


def zbuduj_indeks_oryginalow(src: Path) -> dict:
    """DSCxxxx (uppercase) -> Path oryginalu."""
    idx = defaultdict(list)
    for p in src.iterdir():
        if not p.is_file():
            continue
        m = WZORZEC_DSC.search(p.stem)
        if m:
            idx[m.group(1).upper()].append(p)
    return idx


def main():
    if len(sys.argv) != 4:
        print("Uzycie: przenies_oryginaly.py <wycinki> <oryginaly_zrodlo> <folder_wyjsciowy>")
        sys.exit(1)

    wycinki_dir = Path(sys.argv[1])
    src_dir = Path(sys.argv[2])
    out_dir = Path(sys.argv[3])
    out_dir.mkdir(parents=True, exist_ok=True)

    oryginaly = zbuduj_indeks_oryginalow(src_dir)

    skopiowane = []
    pominiete_bez_kodu = []
    brak_oryginalu = []
    konflikty_dsc = []
    docelowe_nazwy = defaultdict(list)  # nazwa_wyjsciowa -> [wycinki]

    for w in sorted(wycinki_dir.iterdir()):
        if not w.is_file():
            continue
        m = WZORZEC_KOD.match(w.name)
        if not m or m.group("kod").lower() in NIEPEWNE_PREFIKSY:
            pominiete_bez_kodu.append(w.name)
            continue

        kod = m.group("kod")
        dsc = m.group("dsc").upper()

        kandydaci = oryginaly.get(dsc, [])
        if len(kandydaci) == 0:
            brak_oryginalu.append(w.name)
            continue
        if len(kandydaci) > 1:
            konflikty_dsc.append((w.name, [str(k) for k in kandydaci]))
            continue

        oryginal = kandydaci[0]
        docelowa_nazwa = f"{kod}_{dsc}{oryginal.suffix}"
        docelowe_nazwy[docelowa_nazwa].append(w.name)
        skopiowane.append((oryginal, docelowa_nazwa))

    # nie kopiuj, jesli dwa rozne wycinki chca tej samej nazwy docelowej
    konflikty_nazw = {n: zrodla for n, zrodla in docelowe_nazwy.items() if len(zrodla) > 1}

    ile_ok = 0
    for oryginal, docelowa_nazwa in skopiowane:
        if docelowa_nazwa in konflikty_nazw:
            continue
        cel = out_dir / docelowa_nazwa
        shutil.copy2(oryginal, cel)  # kopia 1:1, oryginal nie jest ruszany ani przycinany
        ile_ok += 1

    print(f"skopiowano: {ile_ok}")
    print(f"pominiete (brak kodu - do sprawdzenia): {len(pominiete_bez_kodu)}")
    if brak_oryginalu:
        print(f"BRAK oryginalu dla {len(brak_oryginalu)} wycinkow:")
        for n in brak_oryginalu:
            print("   ", n)
    if konflikty_dsc:
        print(f"KONFLIKT - kilka oryginalow z tym samym numerem DSC ({len(konflikty_dsc)}):")
        for n, ks in konflikty_dsc:
            print("   ", n, "->", ks)
    if konflikty_nazw:
        print(f"KONFLIKT - rozne wycinki wskazuja na ta sama nazwe wyjsciowa ({len(konflikty_nazw)}):")
        for n, zrodla in konflikty_nazw.items():
            print("   ", n, "<-", zrodla)


if __name__ == "__main__":
    main()
