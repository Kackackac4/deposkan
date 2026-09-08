"""Kadruje zdjecia do okolicy recznie pisanego czerwonego numeru (np. P5204707-1).

Nie wycina samego napisu - znajduje czerwien i przycina zdjecie do jej okolicy
z duzym zapasem, zeby cala etykieta i kontekst byly widoczne. Kadr bierze
z oryginalu w pelnej rozdzielczosci; zmniejszona kopia sluzy tylko do detekcji.

Uzycie:
    python3 -m venv .venv && .venv/bin/pip install opencv-python-headless numpy
    .venv/bin/python Deposkan/wytnij_numery.py "Deposkan/02.09.2026" wycinki [limit]

Ostatni argument (limit) jest opcjonalny - przetwarza tylko N pierwszych zdjec.
"""
import sys, cv2, numpy as np
from pathlib import Path

DETEKCJA_PX = 3600        # dluzszy bok kopii roboczej; zdjecia z daleka maja maly numer
WYNIK_PX    = 1800        # dluzszy bok zapisywanego kadru

def maska_czerwieni(img, czulosc=1.0):
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    m_hsv = cv2.inRange(hsv, (0, 60, 40), (12, 255, 255)) | \
            cv2.inRange(hsv, (165, 60, 40), (180, 255, 255))
    b, g, r = cv2.split(img.astype(np.int16))
    d = r - np.maximum(g, b)                    # przewaga czerwieni nad reszta
    # prog adaptacyjny: zdjecia w slabym swietle maja duzo mniejszy kontrast pisaka
    t = max(20.0 * czulosc, 0.45 * float(np.percentile(d, 99.9)))
    m_rgb = ((d > t) & (r > 45)).astype(np.uint8) * 255
    return cv2.bitwise_and(m_hsv, m_rgb)

def szukaj(img, skala, czulosc, gest_max, close_w, min_ar, min_bh, min_area, jasnosc_min=0):
    """Zwraca bbox czerwonego napisu w ukladzie kopii roboczej albo None."""
    surowa = maska_czerwieni(img, czulosc)
    surowa = cv2.morphologyEx(surowa, cv2.MORPH_OPEN,
                              cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3)))
    sklej = cv2.morphologyEx(surowa, cv2.MORPH_CLOSE,
                             cv2.getStructuringElement(cv2.MORPH_RECT, (close_w, max(3, close_w // 3))))

    hsv_v = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)[:, :, 2]
    max_area = 0.03 * img.shape[0] * img.shape[1]   # napis nigdy nie zajmuje 3% kadru

    n, _, stats, _ = cv2.connectedComponentsWithStats(sklej, 8)
    kand, seedy = [], []
    for i in range(1, n):
        x, y, bw, bh, a = stats[i]
        if a < min_area or a > max_area or bh < min_bh or not (min_ar < bw / max(bh, 1) < 30):
            continue
        # gestosc surowej czerwieni w bboxie: pismo to cienkie kreski, lita plama
        # (rozowa karteczka, tasma, czerwony przedmiot) wypelnia prostokat prawie caly
        gest = float((surowa[y:y+bh, x:x+bw] > 0).sum()) / (bw * bh)
        if not (0.02 < gest < gest_max):
            continue
        kand.append((x, y, bw, bh, a))
        # numer jest pisany na jasnej etykiecie - ciemne tlo (blat, paleta) nie moze byc
        # punktem startowym, ale juz doklejane sasiednie znaki moga lezec w cieniu
        if not jasnosc_min or np.median(hsv_v[y:y+bh, x:x+bw]) >= jasnosc_min:
            seedy.append((x, y, bw, bh, a))
    if not seedy:
        return None

    # seed = najwiekszy jasny blok, potem iteracyjny rozrost na sasiadow w tej samej linii
    seed = max(seedy, key=lambda k: k[4])
    hs = seed[3]
    x0, y0, x1, y1 = seed[0], seed[1], seed[0]+seed[2], seed[1]+seed[3]
    zmiana = True
    while zmiana:
        zmiana = False
        for kx0, ky0, kw, kh, _ in kand:
            kx1, ky1 = kx0 + kw, ky0 + kh
            if kx0 > x1 + 2.2*hs or kx1 < x0 - 2.2*hs or ky0 > y1 + 0.7*hs or ky1 < y0 - 0.7*hs:
                continue
            nowy = (min(x0, kx0), min(y0, ky0), max(x1, kx1), max(y1, ky1))
            if nowy != (x0, y0, x1, y1):
                x0, y0, x1, y1 = nowy
                zmiana = True
    return x0, y0, x1, y1

def kadruj(path: Path):
    orig = cv2.imread(str(path))
    if orig is None:
        return None
    H, W = orig.shape[:2]
    skala = min(1.0, DETEKCJA_PX / max(H, W))
    prac = cv2.resize(orig, None, fx=skala, fy=skala, interpolation=cv2.INTER_AREA) if skala < 1 else orig

    # przebieg podstawowy, potem czulszy dla zdjec z daleka / w slabym swietle
    box = szukaj(prac, skala, 1.0, 0.62, 60, 0.8, 14, 400, jasnosc_min=70)
    if box is None:
        box = szukaj(prac, skala, 0.7, 0.75, 90, 1.2, 9, 250, jasnosc_min=75)
    if box is None:
        return None

    # przelicz na oryginal i kadruj z DUZYM zapasem - numer ma byc widoczny w kontekscie
    x0, y0, x1, y1 = [int(v / skala) for v in box]
    bw, bh = x1 - x0, y1 - y0
    mx = int(max(0.9 * bw, 4.0 * bh, 0.05 * W))
    my = int(max(3.0 * bh, 0.30 * bw, 0.05 * H))
    wyc = orig[max(0, y0-my):min(H, y1+my), max(0, x0-mx):min(W, x1+mx)]
    if wyc.size == 0:
        return None

    dl = max(wyc.shape[:2])
    if dl != WYNIK_PX:
        r = WYNIK_PX / dl
        wyc = cv2.resize(wyc, None, fx=r, fy=r,
                         interpolation=cv2.INTER_AREA if r < 1 else cv2.INTER_CUBIC)
    return wyc

def main():
    SRC, OUT = Path(sys.argv[1]), Path(sys.argv[2])
    LIMIT = int(sys.argv[3]) if len(sys.argv) > 3 else 0
    OUT.mkdir(parents=True, exist_ok=True)
    pliki = [p for p in sorted(SRC.iterdir()) if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}]
    if LIMIT:
        pliki = pliki[:LIMIT]
    brak = []
    for p in pliki:
        wy = kadruj(p)
        if wy is None:
            # nic nie wykryto - zapisz cale zdjecie z dopiskiem w nazwie, do recznego przejrzenia
            brak.append(p.name)
            cale = cv2.imread(str(p))
            if cale is not None:
                r = WYNIK_PX / max(cale.shape[:2])
                if r < 1:
                    cale = cv2.resize(cale, None, fx=r, fy=r, interpolation=cv2.INTER_AREA)
                cv2.imwrite(str(OUT / f"{p.stem}_DO-SPRAWDZENIA.jpg"), cale, [cv2.IMWRITE_JPEG_QUALITY, 90])
            continue
        cv2.imwrite(str(OUT / f"{p.stem}.jpg"), wy, [cv2.IMWRITE_JPEG_QUALITY, 92])
    print(f"przetworzono: {len(pliki)}, skadrowano: {len(pliki)-len(brak)}, do sprawdzenia: {len(brak)}")
    for b in brak:
        print("  ", b)
    if brak:
        print("-> zapisane jako *_DO-SPRAWDZENIA.jpg (cale zdjecie)")

if __name__ == "__main__":
    main()
