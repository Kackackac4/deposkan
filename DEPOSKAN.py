#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DEPOSKAN — rozpoznawanie recznie pisanych numerow zamowien ze zdjec paczek.

Wszystko w jednym pliku: detekcja czerwieni (OpenCV), wysylka do Gemini,
zmiana nazw, serwer lokalny i interfejs. Zamkniecie okna konczy program.

Jak dziala:
  1. Zaznaczasz zdjecia w Finderze (albo wskazujesz folder w oknie).
  2. OpenCV lokalnie znajduje czerwony numer i przycina kadr — za darmo, ~0.1 s/zdjecie.
  3. Kadry ida paczkami do Gemini, ktore odsyla odczytane kody.
  4. Oryginaly dostaja nowe nazwy KOD_DSCxxxx.JPG.

Uruchamianie: dwuklik w „Uruchom DEPOSKAN.command".

Copyright (c) 2026 MAKRO-PLAST Sp. z o.o. — wszelkie prawa zastrzezone.
Oprogramowanie wlasnosciowe, do uzytku wewnetrznego firmy. Patrz plik LICENSE.

Uwaga o limitach: darmowy tier ma niski limit requestow na dobe, dlatego kadry
leca paczkami (kilka obrazow w jednym requescie), a postep zapisuje sie na dysk —
gdy limit sie skonczy, nastepnego dnia program dokonczy od miejsca przerwania.
"""
import base64, http.server, json, os, re, shutil, socket, subprocess, sys, tempfile
import threading, time, urllib.error, urllib.request

import cv2
import numpy as np

# ══════════════════════════════════════════════════════════════════════════
#  USTAWIENIA
# ══════════════════════════════════════════════════════════════════════════
WERSJA = '1.0.6'
REPO   = 'Kackackac4/deposkan'      # do sprawdzania aktualizacji na GitHubie

# Domyslne ustawienia — uzytkownik zmienia je w oknie Ustawienia, zapisuja sie na dysk.
DOMYSLNE = {
    'klucz':  '',                      # klucz API wpisuje sie w aplikacji, NIE w kodzie
    'model':  'gemini-3.5-flash-lite',
    'na_req': 25,                      # kadrow w jednym zapytaniu
    'rpm':    15,                      # zapytan na minute
}

WYSYLKA_PX = 1100      # dluzszy bok kadru wysylanego do API
PORT       = 8791

# ── gdzie trzymamy ustawienia (rozne sciezki na Windows i macOS) ─────────
if sys.platform == 'win32':
    CFG_DIR = os.path.join(os.environ.get('APPDATA') or os.path.expanduser('~'), 'Deposkan')
elif sys.platform == 'darwin':
    CFG_DIR = os.path.expanduser('~/Library/Application Support/Deposkan')
else:
    CFG_DIR = os.path.expanduser('~/.config/deposkan')
CFG_FILE  = os.path.join(CFG_DIR, 'config.json')
STAN_FILE = os.path.join(CFG_DIR, 'stan.json')   # postep — pozwala dokonczyc pozniej


def cfg_wczytaj():
    try:
        d = json.load(open(CFG_FILE, encoding='utf-8'))
    except Exception:
        d = {}
    return {**DOMYSLNE, **d}


def cfg_zapisz(zmiany):
    d = cfg_wczytaj()
    d.update(zmiany)
    os.makedirs(CFG_DIR, exist_ok=True)
    json.dump(d, open(CFG_FILE, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    return d


def klucz():
    k = cfg_wczytaj().get('klucz', '').strip()
    if not k:
        raise RuntimeError('brak klucza API — wpisz go w Ustawieniach')
    return k


WZOR_KODU = re.compile(r'^[A-Z]{1,3}\d{6,9}-\d{1,2}$')

# ══════════════════════════════════════════════════════════════════════════
#  DETEKCJA CZERWONEGO NUMERU  (logika z wytnij_numery.py)
# ══════════════════════════════════════════════════════════════════════════
DETEKCJA_PX = 3600      # dluzszy bok kopii roboczej; zdjecia z daleka maja maly numer
WYNIK_PX    = 1800      # dluzszy bok zapisywanego kadru


def maska_czerwieni(img, czulosc=1.0):
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    m_hsv = cv2.inRange(hsv, (0, 60, 40), (12, 255, 255)) | \
            cv2.inRange(hsv, (165, 60, 40), (180, 255, 255))
    b, g, r = cv2.split(img.astype(np.int16))
    d = r - np.maximum(g, b)                     # przewaga czerwieni nad reszta
    # prog adaptacyjny: zdjecia w slabym swietle maja duzo mniejszy kontrast pisaka
    t = max(20.0 * czulosc, 0.45 * float(np.percentile(d, 99.9)))
    m_rgb = ((d > t) & (r > 45)).astype(np.uint8) * 255
    return cv2.bitwise_and(m_hsv, m_rgb)


def szukaj(img, czulosc, gest_max, close_w, min_ar, min_bh, min_area, jasnosc_min=0):
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
        # numer jest pisany na jasnej etykiecie — ciemne tlo nie moze byc punktem
        # startowym, ale juz doklejane sasiednie znaki moga lezec w cieniu
        if not jasnosc_min or np.median(hsv_v[y:y+bh, x:x+bw]) >= jasnosc_min:
            seedy.append((x, y, bw, bh, a))
    if not seedy:
        return None

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


def kadruj(sciezka):
    """Zwraca kadr (BGR) wokol numeru albo None, gdy nie znaleziono czerwieni."""
    orig = cv2.imread(sciezka)
    if orig is None:
        return None
    H, W = orig.shape[:2]
    skala = min(1.0, DETEKCJA_PX / max(H, W))
    prac = cv2.resize(orig, None, fx=skala, fy=skala, interpolation=cv2.INTER_AREA) if skala < 1 else orig

    box = szukaj(prac, 1.0, 0.62, 60, 0.8, 14, 400, jasnosc_min=70)
    if box is None:      # drugi przebieg: blady pisak / slabe swiatlo / zdjecie z daleka
        box = szukaj(prac, 0.7, 0.75, 90, 1.2, 9, 250, jasnosc_min=75)
    if box is None:
        return None

    x0, y0, x1, y1 = [int(v / skala) for v in box]
    bw, bh = x1 - x0, y1 - y0
    # kadr z DUZYM zapasem: numer ma byc widoczny w kontekscie etykiety, nic obcietego
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


def kadr_do_jpg(kadr, dl_boku=WYSYLKA_PX, jakosc=88):
    """Kadr -> bajty JPG, zmniejszony do wysylki (mniej tokenow, ten sam odczyt)."""
    r = dl_boku / max(kadr.shape[:2])
    if r < 1:
        kadr = cv2.resize(kadr, None, fx=r, fy=r, interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode('.jpg', kadr, [cv2.IMWRITE_JPEG_QUALITY, jakosc])
    return buf.tobytes() if ok else None


# ══════════════════════════════════════════════════════════════════════════
#  GEMINI
# ══════════════════════════════════════════════════════════════════════════
BAZA = 'https://generativelanguage.googleapis.com/v1beta'

PROMPT = """Na zdjeciach sa paczki z drukowana etykieta adresowa, na ktorej ktos
dopisal RECZNIE CZERWONYM pisakiem numer zamowienia.

Odczytaj ten czerwony odreczny numer z kazdego obrazu.

Format numeru: 1-3 litery + cyfry + myslnik + cyfra, np. P5204707-1, TU1808836-1,
OS0219136-1. Sufiks bywa inny niz -1 (np. -2) i wtedy TAK MA BYC, nie poprawiaj go.

Wazne:
- Czesc zdjec jest obrocona o 180 stopni. Obroc w glowie i odczytaj normalnie.
- Interesuje Cie WYLACZNIE tekst pisany czerwonym pisakiem, nie drukowany adres.
- Jesli w kadrze widac WIECEJ NIZ JEDEN rozny numer (np. zdjecie calej polki albo
  kilku paczek naraz) — podaj ten numer, ktory jest najlepiej widoczny: najwiekszy,
  najostrzejszy, najblizej srodka kadru — i ustaw "wiele": true. NIE zwracaj wtedy "?".
- Jesli numeru nie ma w ogole, jest nieczytelny albo zasloniety — zwroc kod "?"
  i pewnosc "niska". Lepiej przyznac sie do niepewnosci niz zgadnac.
- Nie zmyslaj. Cyfry 3 i 9 oraz 0 i 6 bywaja podobne — jesli nie masz pewnosci,
  ustaw pewnosc "niska".

Obrazy sa ponumerowane. Dla kazdego zwroc jeden wpis z jego numerem."""

PROMPT2 = """To sa CALE zdjecia paczek. Automat nie znalazl na nich czerwonego numeru
w wycietym kadrze, wiec szukamy jeszcze raz na pelnym obrazie.

Szukaj WYLACZNIE numeru napisanego ODRECZNIE CZERWONYM pisakiem — zwykle na bialej
drukowanej etykiete adresowej naklejonej na paczke.

IGNORUJ calkowicie:
- drukowany tekst adresu, kody kreskowe, logotypy,
- karteczki samoprzylepne (zolte, rozowe) i napisy czarnym dlugopisem,
- czerwone paski na tasmie pakowej i czerwone przedmioty.

Numer moze byc maly, w rogu kadru, obrocony o 180 stopni albo pod katem — obejrzyj
cale zdjecie dokladnie.

Format: 1-3 litery + cyfry + myslnik + cyfra.

W tej samej partii wystapily juz nastepujace numery:
%s
Jesli widzisz numer podobny do ktoregos z powyzszych, to najpewniej ten sam — ale
przepisz DOKLADNIE to, co widzisz, nie dopasowuj na sile.

Jesli w kadrze jest kilka roznych numerow — podaj najlepiej widoczny i ustaw "wiele": true.
Jesli naprawde nie ma zadnego odrecznego czerwonego numeru — zwroc "?" i pewnosc "niska"."""


SCHEMAT = {
    'type': 'OBJECT',
    'properties': {
        'wyniki': {
            'type': 'ARRAY',
            'items': {
                'type': 'OBJECT',
                'properties': {
                    'nr':      {'type': 'INTEGER'},
                    'kod':     {'type': 'STRING'},
                    'pewnosc': {'type': 'STRING', 'enum': ['wysoka', 'niska']},
                    'wiele':   {'type': 'BOOLEAN'},
                },
                'required': ['nr', 'kod', 'pewnosc', 'wiele'],
            },
        }
    },
    'required': ['wyniki'],
}


def http_json(url, dane=None, timeout=180):
    req = urllib.request.Request(
        url,
        data=json.dumps(dane).encode() if dane is not None else None,
        headers={'Content-Type': 'application/json'},
        method='POST' if dane is not None else 'GET')
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


# Limity zapytan NIE sa dostepne przez API (Google pokazuje je tylko w panelu
# AI Studio), wiec trzymamy je tutaj. Dopisz swoj model, jesli dojdzie nowy.
LIMITY = {
    'gemini-3.5-flash':       {'rpm': 5,  'rpd': 20},
    'gemini-3-flash-preview': {'rpm': 5,  'rpd': 20},
    'gemini-2.5-flash':       {'rpm': 10, 'rpd': 250},
    'flash-lite':             {'rpm': 20, 'rpd': 20},     # dopasowanie po fragmencie nazwy
    'flash':                  {'rpm': 5,  'rpd': 20},     # ostroznie, gdy nie znamy modelu
}


def limity_modelu(model):
    """Zwraca limity + to, co API faktycznie podaje o modelu."""
    lim = None
    if model in LIMITY:
        lim = LIMITY[model]
    else:
        for frag, v in LIMITY.items():          # dopasowanie po fragmencie nazwy
            if frag in model:
                lim = v
                break
    lim = lim or {'rpm': 5, 'rpd': 20}
    out = {'rpm': lim['rpm'], 'rpd': lim['rpd'], 'zrodlo': 'tabela wbudowana'}
    try:                                        # metadane z API — tokeny, nie zapytania
        d = http_json(f'{BAZA}/models/{model}?key={klucz()}')
        out['in_tok'] = d.get('inputTokenLimit')
        out['nazwa'] = d.get('displayName')
    except Exception:
        pass
    return out


def sprawdz_aktualizacje():
    """Pyta GitHuba o najnowsze wydanie. Nie pobiera nic sam — tylko informuje."""
    try:
        req = urllib.request.Request(
            f'https://api.github.com/repos/{REPO}/releases/latest',
            headers={'Accept': 'application/vnd.github+json', 'User-Agent': 'deposkan'})
        with urllib.request.urlopen(req, timeout=12) as r:
            d = json.loads(r.read().decode())
    except Exception as e:
        return {'wersja': WERSJA, 'blad': str(e)[:120]}

    naj = (d.get('tag_name') or '').lstrip('v').strip()

    def liczby(w):
        return [int(x) for x in re.findall(r'\d+', w)] or [0]

    nowsza = liczby(naj) > liczby(WERSJA) if naj else False
    # podpowiadamy plik pasujacy do systemu
    szukaj_roz = '.exe' if sys.platform == 'win32' else '.zip'
    link = d.get('html_url', '')
    for a in d.get('assets', []):
        if a.get('name', '').lower().endswith(szukaj_roz):
            link = a.get('browser_download_url', link)
            break
    return {'wersja': WERSJA, 'najnowsza': naj, 'nowsza': nowsza,
            'link': link, 'opis': (d.get('body') or '')[:300]}


def sciezka_aplikacji():
    """Gdzie leży zainstalowana aplikacja. None, gdy chodzimy ze zrodel."""
    if not getattr(sys, 'frozen', False):
        return None
    if sys.platform == 'darwin':
        # .../DEPOSKAN.app/Contents/MacOS/DEPOSKAN  ->  .../DEPOSKAN.app
        p = os.path.abspath(sys.executable)
        for _ in range(3):
            p = os.path.dirname(p)
        return p if p.endswith('.app') else None
    return os.path.abspath(sys.executable)


def pobierz_z_kontrola(url, cel, suma_url=None, nazwa=None):
    """Pobiera plik i — jesli sa sumy kontrolne — sprawdza SHA-256."""
    with urllib.request.urlopen(urllib.request.Request(
            url, headers={'User-Agent': 'deposkan'}), timeout=300) as r, open(cel, 'wb') as f:
        calosc = int(r.headers.get('Content-Length') or 0)
        mam = 0
        while True:
            kawalek = r.read(262144)
            if not kawalek:
                break
            f.write(kawalek)
            mam += len(kawalek)
            if calosc:
                faza(f'pobieram aktualizacje… {100*mam//calosc}%')

    if not (suma_url and nazwa):
        return True
    try:
        with urllib.request.urlopen(urllib.request.Request(
                suma_url, headers={'User-Agent': 'deposkan'}), timeout=30) as r:
            sumy = r.read().decode()
    except Exception:
        return True                       # brak pliku sum — nie blokujemy aktualizacji
    import hashlib
    oczekiwana = None
    for linia in sumy.splitlines():
        czesci = linia.split()
        if len(czesci) == 2 and czesci[1].lstrip('*') == nazwa:
            oczekiwana = czesci[0]
    if not oczekiwana:
        return True
    h = hashlib.sha256()
    with open(cel, 'rb') as f:
        for blok in iter(lambda: f.read(1 << 20), b''):
            h.update(blok)
    return h.hexdigest() == oczekiwana


def zaktualizuj():
    """Pobiera nowe wydanie i podmienia zainstalowana aplikacje w miejscu."""
    cel = sciezka_aplikacji()
    if not cel:
        raise RuntimeError('aktualizacja dziala tylko w zainstalowanej aplikacji')

    a = sprawdz_aktualizacje()
    if a.get('blad'):
        raise RuntimeError(a['blad'])
    if not a.get('nowsza'):
        return {'ok': False, 'info': 'masz juz najnowsza wersje'}

    faza('pobieram aktualizacje…')
    tmp = tempfile.mkdtemp(prefix='deposkan-akt-')
    nazwa = os.path.basename(a['link'].split('?')[0])
    paczka = os.path.join(tmp, nazwa)
    sumy = f"https://github.com/{REPO}/releases/download/v{a['najnowsza']}/checksums.txt"
    if not pobierz_z_kontrola(a['link'], paczka, sumy, nazwa):
        shutil.rmtree(tmp, ignore_errors=True)
        raise RuntimeError('suma kontrolna pobranego pliku sie nie zgadza')

    faza('przygotowuje podmiane…')
    if sys.platform == 'darwin':
        skrypt = os.path.join(tmp, 'podmien.sh')
        open(skrypt, 'w').write(f"""#!/bin/bash
# czekamy, az aplikacja sie zamknie, potem podmieniamy bundle i uruchamiamy na nowo
for _ in $(seq 1 120); do
  pgrep -f "{cel}/Contents/MacOS/" >/dev/null || break
  sleep 0.5
done
ditto -x -k "{paczka}" "{tmp}/rozpakowane" || exit 1
[ -d "{tmp}/rozpakowane/DEPOSKAN.app" ] || exit 1
rm -rf "{cel}"
ditto "{tmp}/rozpakowane/DEPOSKAN.app" "{cel}"

# LaunchServices potrzebuje chwili, zeby zauwazyc podmieniony bundle
sleep 1
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "{cel}" 2>/dev/null
for _ in 1 2 3; do
  open "{cel}" && sleep 3
  pgrep -f "{cel}/Contents/MacOS/" >/dev/null && break
  sleep 2
done
rm -rf "{tmp}"
""")
        os.chmod(skrypt, 0o755)
        subprocess.Popen(['/bin/bash', skrypt], start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        skrypt = os.path.join(tmp, 'podmien.ps1')
        open(skrypt, 'w').write(f"""
$stary = "{cel}"
for ($i=0; $i -lt 120; $i++) {{
  if (-not (Get-Process DEPOSKAN -ErrorAction SilentlyContinue)) {{ break }}
  Start-Sleep -Milliseconds 500
}}
Copy-Item -Force "{paczka}" $stary
Start-Process $stary
Remove-Item -Recurse -Force "{tmp}"
""")
        subprocess.Popen(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                          '-File', skrypt], creationflags=0x00000008)

    return {'ok': True, 'wersja': a['najnowsza']}


def lista_modeli():
    """Pobiera modele dostepne na TYM kluczu — bez zgadywania nazw."""
    d = http_json(f'{BAZA}/models?key={klucz()}&pageSize=200')
    out = []
    for m in d.get('models', []):
        if 'generateContent' in m.get('supportedGenerationMethods', []):
            nazwa = m['name'].split('/')[-1]
            if 'embedding' not in nazwa and 'imagen' not in nazwa:
                out.append(nazwa)
    return sorted(out)


def orig_do_jpg(sciezka, dl_boku=1500, jakosc=86):
    """Cale zdjecie zmniejszone do wysylki — drugie podejscie patrzy na pelny kadr."""
    im = cv2.imread(sciezka)
    if im is None:
        return None
    r = dl_boku / max(im.shape[:2])
    if r < 1:
        im = cv2.resize(im, None, fx=r, fy=r, interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode('.jpg', im, [cv2.IMWRITE_JPEG_QUALITY, jakosc])
    return buf.tobytes() if ok else None


def czytaj_paczke(jpgi, model, prompt=None):
    """jpgi: lista bajtow JPG. Zwraca liste kodow (str) tej samej dlugosci."""
    czesci = [{'text': prompt or PROMPT}]
    for i, b in enumerate(jpgi, 1):
        czesci.append({'text': f'--- obraz {i} ---'})
        czesci.append({'inline_data': {'mime_type': 'image/jpeg',
                                       'data': base64.b64encode(b).decode()}})
    ciało = {
        'contents': [{'parts': czesci}],
        'generationConfig': {
            'responseMimeType': 'application/json',
            'responseSchema': SCHEMAT,
            'temperature': 0,
        },
    }
    d = http_json(f'{BAZA}/models/{model}:generateContent?key={klucz()}', ciało)
    tekst = d['candidates'][0]['content']['parts'][0]['text']
    wyniki = json.loads(tekst).get('wyniki', [])

    kody = ['?'] * len(jpgi)
    for w in wyniki:
        i = int(w.get('nr', 0)) - 1
        if 0 <= i < len(kody):
            kod = (w.get('kod') or '?').strip().upper().replace(' ', '')
            wiele = bool(w.get('wiele'))
            if not WZOR_KODU.match(kod):
                kod = '?'                      # nie trzyma formatu — brak odczytu
            elif wiele:
                kod += '+wiele'                # kilka etykiet w kadrze, to ta glowna
            elif w.get('pewnosc') != 'wysoka':
                kod = '?'                      # sam odczyt niepewny
            kody[i] = kod
    return kody


# ══════════════════════════════════════════════════════════════════════════
#  PRZEBIEG
# ══════════════════════════════════════════════════════════════════════════
ROZSZ = {'.jpg', '.jpeg', '.png', '.webp', '.heic'}

STAN = {
    'pracuje': False, 'stop': False, 'etap': '', 'zrobione': 0, 'ile': 0,
    'log': [], 'wyniki': [], 'req': 0, 'gotowe': False, 'folder': '', 'cel': '',
    'faza': '', 'start': 0.0, 'trwa': 0,
}
BLOKADA = threading.Lock()


def faza(txt):
    """Krotki opis tego, co program robi w tej chwili — zeby nie wygladal na zawieszony."""
    with BLOKADA:
        STAN['faza'] = txt
        if STAN['start']:
            STAN['trwa'] = int(time.time() - STAN['start'])


def log(txt):
    with BLOKADA:
        STAN['log'].append(txt)
        del STAN['log'][:-400]


def zuzycie(dolicz=0):
    """Licznik zapytan na dzis — darmowy tier ma niski limit dobowy, warto widziec ile poszlo."""
    dzis = time.strftime('%Y-%m-%d')
    d = cfg_wczytaj()
    if d.get('data') != dzis:
        d['data'], d['dzis'] = dzis, 0
    if dolicz:
        d = cfg_zapisz({'data': dzis, 'dzis': d.get('dzis', 0) + dolicz})
    return {'data': d.get('data', dzis), 'dzis': d.get('dzis', 0)}


def stan_zapisz(folder, mapa):
    os.makedirs(CFG_DIR, exist_ok=True)
    json.dump({'folder': folder, 'mapa': mapa}, open(STAN_FILE, 'w', encoding='utf-8'),
              ensure_ascii=False, indent=1)


def stan_wczytaj(folder):
    try:
        d = json.load(open(STAN_FILE, encoding='utf-8'))
        return d['mapa'] if d.get('folder') == folder else {}
    except Exception:
        return {}


def unikalna(sciezka):
    """Nigdy nie nadpisujemy — przy kolizji dokladamy licznik."""
    if not os.path.exists(sciezka):
        return sciezka
    baza, roz = os.path.splitext(sciezka)
    i = 2
    while os.path.exists(f'{baza}_{i}{roz}'):
        i += 1
    return f'{baza}_{i}{roz}'


def przebieg(pliki, model, na_req, rpm):
    """Glowny watek roboczy: kadrowanie -> Gemini -> zmiana nazw."""
    try:
        folder = os.path.dirname(pliki[0])
        wczesniej = stan_wczytaj(folder)          # to, co juz odczytano wczesniej
        mapa = dict(wczesniej)
        if wczesniej:
            log(f'wznawiam — {len(wczesniej)} zdjec bylo juz odczytanych')

        do_zrobienia = [p for p in pliki if os.path.basename(p) not in mapa]
        with BLOKADA:
            STAN.update(ile=len(do_zrobienia), zrobione=0, folder=folder, etap='kadrowanie')

        # ── 1. kadrowanie lokalnie ───────────────────────────────────────
        kadry, bez_czerwieni = [], []
        for nr_p, p in enumerate(do_zrobienia, 1):
            if STAN['stop']:
                break
            faza(f'kadruje {nr_p}/{len(do_zrobienia)}: {os.path.basename(p)}')
            k = kadruj(p)
            if k is None:
                bez_czerwieni.append(p)
                mapa[os.path.basename(p)] = '?'
                log(f'bez czerwieni: {os.path.basename(p)}')
            else:
                jpg = kadr_do_jpg(k)
                if jpg:
                    kadry.append((p, jpg))
            with BLOKADA:
                STAN['zrobione'] += 1
        log(f'skadrowano {len(kadry)}, bez czerwieni {len(bez_czerwieni)}')

        # ── 2. odczyt przez Gemini, paczkami, z pauzami na limit ─────────
        paczki = [kadry[i:i+na_req] for i in range(0, len(kadry), na_req)]
        odstep = 60.0 / max(rpm, 1)
        with BLOKADA:
            STAN.update(etap='odczyt', ile=len(paczki), zrobione=0)
        log(f'{len(paczki)} zapytan do {model}, co {odstep:.0f} s')

        ostatnie, brak_limitu = 0.0, False
        for nr, paczka in enumerate(paczki, 1):
            if STAN['stop']:
                break
            czekaj = odstep - (time.time() - ostatnie)
            while czekaj > 0 and not STAN['stop']:      # pauza na limit RPM
                faza(f'pauza na limit — {int(czekaj)+1} s do paczki {nr}/{len(paczki)}')
                time.sleep(min(0.5, czekaj))
                czekaj = odstep - (time.time() - ostatnie)
            if STAN['stop']:
                break

            proba, kody = 0, None
            while proba < 4 and kody is None and not STAN['stop']:
                try:
                    ostatnie = time.time()
                    faza(f'paczka {nr}/{len(paczki)} — wyslane {len(paczka)} kadrow, '
                         f'czekam na odpowiedz…')
                    kody = czytaj_paczke([j for _, j in paczka], model)
                    faza(f'paczka {nr}/{len(paczki)} — odpowiedz po '
                         f'{time.time()-ostatnie:.0f} s')
                    zuzycie(1)                       # doliczamy do licznika dobowego
                    with BLOKADA:
                        STAN['req'] += 1
                except urllib.error.HTTPError as e:
                    tresc = e.read().decode('utf-8', 'replace')[:200]
                    if e.code in (429, 500, 503):        # limit albo chwilowy blad
                        proba += 1
                        pauza = min(60, 5 * 2 ** proba)
                        log(f'paczka {nr}: HTTP {e.code}, czekam {pauza} s (proba {proba}/3)')
                        if e.code == 429 and proba >= 3:
                            log('LIMIT WYCZERPANY — zapisuje to, co juz odczytane, '
                                'reszta czeka na nastepny raz')
                            brak_limitu = True
                            break
                        for _ in range(pauza * 2):
                            if STAN['stop']:
                                break
                            time.sleep(0.5)
                    else:
                        log(f'paczka {nr}: HTTP {e.code} {tresc} — koncze odczyt, '
                            'nazywam to, co gotowe')
                        brak_limitu = True
                        break

            if kody is None:
                break
            for (p, _), kod in zip(paczka, kody):
                mapa[os.path.basename(p)] = kod
            ile_ok = sum(1 for k in kody if k != '?')
            log(f'paczka {nr}/{len(paczki)}: {ile_ok}/{len(kody)} odczytanych — ' +
                ', '.join(kody))
            stan_zapisz(folder, mapa)
            with BLOKADA:
                STAN['zrobione'] = nr

        # ── 2b. drugie podejscie: nieudane -> CALE zdjecia, jednym zapytaniem ──
        nieudane = [p for p in pliki
                    if mapa.get(os.path.basename(p)) == '?' and os.path.exists(p)]
        if nieudane and not STAN['stop'] and not brak_limitu:
            with BLOKADA:
                STAN.update(etap='drugie podejscie', ile=1, zrobione=0)
            znane = sorted({k.replace('+wiele', '') for k in mapa.values()
                            if k and k != '?'})
            log(f'drugie podejscie: {len(nieudane)} zdjec w calosci, '
                f'kontekst {len(znane)} znanych numerow')
            try:
                # przerwa na limit RPM przed dodatkowym zapytaniem
                czekaj = odstep - (time.time() - ostatnie)
                while czekaj > 0 and not STAN['stop']:
                    faza(f'pauza na limit — {int(czekaj)+1} s do drugiego podejscia')
                    time.sleep(min(0.5, czekaj))
                    czekaj = odstep - (time.time() - ostatnie)

                for i in range(0, len(nieudane), 30):     # 30 zdjec = bezpieczny rozmiar zapytania
                    czesc = nieudane[i:i+30]
                    jpgi = [j for j in (orig_do_jpg(x) for x in czesc) if j]
                    if not jpgi:
                        continue
                    ostatnie = time.time()
                    faza(f'drugie podejscie — {len(jpgi)} pelnych zdjec, czekam…')
                    kody = czytaj_paczke(jpgi, model,
                                         PROMPT2 % ('\n'.join('- ' + z for z in znane) or '- (brak)'))
                    zuzycie(1)
                    with BLOKADA:
                        STAN['req'] += 1
                    odzysk = 0
                    for x, kod in zip(czesc, kody):
                        if kod != '?':
                            mapa[os.path.basename(x)] = kod
                            odzysk += 1
                            log(f'  odzyskane: {os.path.basename(x)} -> {kod}')
                    log(f'drugie podejscie: odzyskano {odzysk}/{len(czesc)}')
                    stan_zapisz(folder, mapa)
            except Exception as e:
                log(f'drugie podejscie nieudane: {type(e).__name__}: {e}')
            with BLOKADA:
                STAN['zrobione'] = 1

        # ── 3. zmiana nazw ───────────────────────────────────────────────
        # kopiowanie kilkuset plikow po kilka MB trwa — pokazujemy postep,
        # zeby pasek nie stal na 100% przez ostatnia jedna trzecia czasu
        with BLOKADA:
            STAN.update(etap='zapisywanie', ile=len(pliki), zrobione=0)
        faza('zapisuje pliki…')
        cel_dir = os.path.join(folder, 'Kopia z kodami')
        os.makedirs(cel_dir, exist_ok=True)
        with BLOKADA:
            STAN['cel'] = cel_dir          # to otwiera przycisk "Pokaz w Finderze"

        wyniki, ile_ok, ile_spr, ile_wiele = [], 0, 0, 0
        for nr_p, p in enumerate(pliki, 1):
            with BLOKADA:
                STAN['zrobione'] = nr_p
            nazwa = os.path.basename(p)
            if nr_p % 5 == 0 or nr_p == len(pliki):
                faza(f'zapisuje {nr_p}/{len(pliki)}: {nazwa}')
            kod = mapa.get(nazwa)
            if kod is None:
                continue
            baza, roz = os.path.splitext(nazwa)
            m = re.search(r'(DSC\d+|IMG_?\d+|\d{3,})', baza, re.I)
            trzon = m.group(1) if m else baza
            if kod == '?':
                nowa, ile_spr = f'{trzon}_sprawdz{roz}', ile_spr + 1
            else:
                nowa, ile_ok = f'{trzon}_{kod}{roz}', ile_ok + 1
                if kod.endswith('+wiele'):
                    ile_wiele += 1
            cel = os.path.join(cel_dir, nowa)
            # przy wznowieniu ten sam plik moze juz tam byc — nie duplikujemy go
            if (os.path.exists(cel) and os.path.abspath(cel) != os.path.abspath(p)
                    and os.path.getsize(cel) == os.path.getsize(p)):
                wyniki.append({'stary': nazwa, 'nowy': nowa, 'kod': kod})
                continue
            cel = unikalna(cel)
            try:
                shutil.copy2(p, cel)              # kopia 1:1, oryginal nietkniety
                wyniki.append({'stary': nazwa, 'nowy': os.path.basename(cel), 'kod': kod})
            except Exception as e:
                log(f'nie udalo sie {nazwa}: {e}')

        with BLOKADA:
            STAN['wyniki'] = wyniki
        if brak_limitu:
            zostalo = len([p for p in pliki if os.path.basename(p) not in mapa])
            log(f'PRZERWANE PRZEZ LIMIT — nazwane {ile_ok + ile_spr} plikow, '
                f'{zostalo} czeka. Uruchom ponownie na tym samym folderze, '
                f'program dokonczy od tego miejsca.')
        log(f'GOTOWE — rozpoznane {ile_ok}'
            + (f' (w tym {ile_wiele} z wieloma etykietami)' if ile_wiele else '')
            + f', do sprawdzenia {ile_spr}, zapytan {STAN["req"]}')
        log(f'pliki w: {cel_dir}')

    except Exception as e:
        log(f'BLAD: {type(e).__name__}: {e}')
    finally:
        with BLOKADA:
            STAN.update(pracuje=False, gotowe=True, etap='koniec', faza='',
                        trwa=int(time.time() - STAN['start']) if STAN['start'] else 0)


# ══════════════════════════════════════════════════════════════════════════
#  WYBOR PLIKOW
# ══════════════════════════════════════════════════════════════════════════
OKNO = None          # okno pywebview — ma wlasne, przenosne okno wyboru plikow


def osascript(skrypt):
    r = subprocess.run(['osascript', '-e', skrypt], capture_output=True, text=True)
    return r.stdout.strip()


def okno_wyboru():
    """Wybor plikow. pywebview daje to samo na Windows i na macOS."""
    if OKNO is not None:
        try:
            w = OKNO.create_file_dialog(
                10,                                    # OPEN_DIALOG
                allow_multiple=True,
                file_types=('Zdjecia (*.jpg;*.jpeg;*.png;*.webp;*.heic)', 'Wszystkie (*.*)'))
            return rozwin(list(w or []))
        except Exception:
            pass                                       # spadamy na wariant systemowy
    if sys.platform == 'darwin':
        out = osascript(
            'set l to choose file with prompt "Wskaz zdjecia" '
            'of type {"public.image"} with multiple selections allowed\n'
            'set r to ""\nrepeat with i in l\n  set r to r & POSIX path of i & linefeed\n'
            'end repeat\nreturn r')
        return rozwin([x for x in out.split('\n') if x.strip()])
    from tkinter import Tk, filedialog                 # zapas dla Windows/Linux
    t = Tk(); t.withdraw()
    w = filedialog.askopenfilenames(title='Wskaz zdjecia')
    t.destroy()
    return rozwin(list(w))


def z_findera():
    """Zaznaczenie w Finderze — tylko macOS; gdzie indziej zwyczajne okno wyboru."""
    if sys.platform != 'darwin':
        return okno_wyboru()
    out = osascript(
        'tell application "Finder" to set l to selection as alias list\n'
        'set r to ""\nrepeat with i in l\n  set r to r & POSIX path of i & linefeed\n'
        'end repeat\nreturn r')
    return rozwin([x for x in out.split('\n') if x.strip()])


def pokaz_folder(f):
    if sys.platform == 'win32':
        os.startfile(f)                                # noqa
    elif sys.platform == 'darwin':
        subprocess.run(['open', f])
    else:
        subprocess.run(['xdg-open', f])


def rozwin(sciezki):
    """Foldery zamienia na liste zdjec w srodku."""
    out = []
    for s in sciezki:
        s = s.rstrip('/')
        if os.path.isdir(s):
            for n in sorted(os.listdir(s)):
                if os.path.splitext(n)[1].lower() in ROZSZ:
                    out.append(os.path.join(s, n))
        elif os.path.splitext(s)[1].lower() in ROZSZ:
            out.append(s)
    return sorted(set(out))


# ══════════════════════════════════════════════════════════════════════════
#  SERWER
# ══════════════════════════════════════════════════════════════════════════
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def send(self, obj, code=200, ctype='application/json'):
        body = obj if isinstance(obj, bytes) else json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', ctype + '; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def do_GET(self):
        if self.path in ('/', '/index.html'):
            return self.send(HTML.encode(), 200, 'text/html')
        self.send({'error': 'nie ma'}, 404)

    def do_POST(self):
        n = int(self.headers.get('Content-Length') or 0)
        try:
            r = json.loads(self.rfile.read(n) or b'{}')
        except Exception:
            r = {}
        try:
            self.send(self.route(self.path, r))
        except urllib.error.HTTPError as e:
            self.send({'error': f'HTTP {e.code}: ' + e.read().decode('utf-8', 'replace')[:300]}, 200)
        except Exception as e:
            self.send({'error': f'{type(e).__name__}: {e}'}, 200)

    def route(self, path, r):
        if path == '/api/finder':
            p = z_findera()
            return {'pliki': p, 'folder': os.path.dirname(p[0]) if p else ''}
        if path == '/api/wybierz':
            p = okno_wyboru()
            return {'pliki': p, 'folder': os.path.dirname(p[0]) if p else ''}
        if path == '/api/ustawienia':
            U = cfg_wczytaj()
            if r.get('zapisz'):
                U = cfg_zapisz({k: r[k] for k in ('klucz','model','na_req','rpm')
                                if k in r})
            U = dict(U)
            U['ma_klucz'] = bool(U.get('klucz', '').strip())
            U['klucz'] = U.get('klucz', '')
            U['wersja'] = WERSJA
            return U
        if path == '/api/aktualizacja':
            return sprawdz_aktualizacje()
        if path == '/api/zaktualizuj':
            w = zaktualizuj()
            if w.get('ok'):
                threading.Timer(1.2, lambda: os._exit(0)).start()   # helper czeka na zamkniecie
            return w
        if path == '/api/reset':
            if STAN['pracuje']:
                return {'error': 'najpierw przerwij prace'}
            with BLOKADA:
                STAN.update(log=[], wyniki=[], req=0, zrobione=0, ile=0,
                            etap='gotowy', gotowe=False, folder='', cel='')
            try:
                os.remove(STAN_FILE)        # kasujemy tez zapisany postep
            except OSError:
                pass
            return {'ok': True}
        if path == '/api/limity':
            return limity_modelu(r.get('model') or cfg_wczytaj()['model'])
        if path == '/api/zuzycie':
            return zuzycie()
        if path == '/api/modele':
            return {'modele': lista_modeli()}
        if path == '/api/start':
            if STAN['pracuje']:
                return {'error': 'juz pracuje'}
            pliki = r.get('pliki') or []
            if not pliki:
                return {'error': 'brak plikow'}
            U = cfg_wczytaj()
            if not U.get('klucz', '').strip():
                return {'error': 'brak klucza API — wpisz go w Ustawieniach'}
            with BLOKADA:
                STAN.update(pracuje=True, stop=False, gotowe=False, log=[], wyniki=[],
                            req=0, zrobione=0, ile=len(pliki), etap='start',
                            faza='', start=time.time(), trwa=0)
            threading.Thread(target=przebieg, daemon=True, args=(
                pliki, r.get('model') or U['model'], int(r.get('na_req') or U['na_req']),
                float(r.get('rpm') or U['rpm']))).start()
            return {'ok': True}
        if path == '/api/stan':
            with BLOKADA:
                if STAN['pracuje'] and STAN['start']:
                    STAN['trwa'] = int(time.time() - STAN['start'])
                return dict(STAN)
        if path == '/api/stop':
            STAN['stop'] = True
            return {'ok': True}
        if path == '/api/pokaz':
            # folder z wynikami; gdy nie bylo kopiowania — folder zrodlowy
            f = STAN.get('cel') or STAN.get('folder')
            if f:
                pokaz_folder(f)
            return {'ok': True}
        raise ValueError('nieznany endpoint ' + path)


# ══════════════════════════════════════════════════════════════════════════
#  INTERFEJS
# ══════════════════════════════════════════════════════════════════════════
HTML = r"""<!doctype html><html lang="pl"><head><meta charset="utf-8">
<title>DEPOSKAN</title><style>
:root{
  --bg1:#cfe4ff; --bg2:#dceaff; --bg3:#e8f4ff; --page:#f4f8fd;
  --card:rgba(255,255,255,.66); --stroke:rgba(255,255,255,.85);
  --txt:#17222e; --dim:#68788a; --line:rgba(30,60,95,.11);
  --acc:#f07316; --ok:#3f9d4a; --err:#d8452e; --warn:#c77400;
  --field:rgba(255,255,255,.7); --track:rgba(30,60,95,.09);
}
@media (prefers-color-scheme:dark){:root{
  --bg1:#1a3050; --bg2:#25405f; --bg3:#122539; --page:#0c1219;
  --card:rgba(32,44,58,.6); --stroke:rgba(160,200,240,.14);
  --txt:#eaf1f8; --dim:#93a6ba; --line:rgba(160,200,240,.13);
  --acc:#ff9330; --ok:#4fd06a; --err:#ff7a5e; --warn:#ffc247;
  --field:rgba(0,0,0,.22); --track:rgba(160,200,240,.13);
}}
*{box-sizing:border-box}
html,body{min-height:100%}
body{margin:0; background:var(--page); color:var(--txt);
  font-family:"SF Pro Text",-apple-system,BlinkMacSystemFont,sans-serif;
  letter-spacing:-.012em; -webkit-font-smoothing:antialiased;
  display:flex; align-items:stretch; justify-content:center; padding:20px; height:100vh;
  overflow:hidden}
.blob{position:fixed; border-radius:50%; filter:blur(100px); opacity:.55; pointer-events:none; z-index:0}
.b1{width:46vw;height:46vw;min-width:340px;min-height:340px;background:var(--bg1);top:-14vw;left:-10vw}
.b2{width:40vw;height:40vw;min-width:300px;min-height:300px;background:var(--bg2);bottom:-16vw;right:-8vw}
.b3{width:34vw;height:34vw;min-width:260px;min-height:260px;background:var(--bg3);top:38%;right:22%}
.card{position:relative; z-index:1; width:100%; max-width:1360px; display:flex;
  flex-direction:column; overflow:hidden;
  background:var(--card); border:1px solid var(--stroke); border-radius:28px;
  backdrop-filter:blur(40px) saturate(180%); -webkit-backdrop-filter:blur(40px) saturate(180%);
  box-shadow:0 24px 60px rgba(0,0,0,.16), inset 0 1px 0 rgba(255,255,255,.35); padding:24px 26px}
/* dwie kolumny: po lewej wybor i sterowanie, po prawej postep, wyniki i log */
  .kolumny{display:grid; grid-template-columns:440px minmax(0,1fr); gap:24px;
  flex:1 1 auto; min-height:0; margin-top:16px}
.lewa,.prawa{min-height:0; display:flex; flex-direction:column}
.lewa{overflow:hidden; padding-right:4px}
.lewa .pliki{flex:1 1 auto; max-height:none; min-height:60px}
.dol{flex:none; margin-top:auto; padding-top:12px}
.dol button{margin-top:8px}
.dol .err:empty{display:none}
.podpis{display:flex; align-items:center; gap:9px; margin-top:14px; padding-top:11px;
  border-top:1px solid var(--line); font-size:10.5px; line-height:1.35; color:var(--dim)}
.podpis .mak{width:38px; height:auto; flex:none; color:#CB2228}
.podpis sup{font-size:.7em; vertical-align:super}
.prawa{gap:0}
.stopka{flex:none; margin-top:10px}
.stopka button{margin-top:0}
.sekcja{font-size:11.5px; font-weight:590; text-transform:uppercase; letter-spacing:.04em;
  color:var(--dim); margin:0 0 7px}
@media (max-width:900px){.kolumny{grid-template-columns:1fr; overflow-y:auto}}
h1{font-size:29px; font-weight:590; margin:0; letter-spacing:-.03em;
  display:flex; align-items:center; gap:11px}
.odsw{margin-left:auto; width:46px; height:46px; flex:none; border-radius:50%;
  border:1px solid var(--line); background:var(--field); color:var(--dim);
  font-size:23px; line-height:1; cursor:pointer; padding:0; margin-top:0;
  box-shadow:none; backdrop-filter:blur(10px); transition:.2s;
  display:flex; align-items:center; justify-content:center}
.odsw::before{display:none}
.odsw:hover:not(:disabled){color:var(--acc); border-color:var(--acc);
  background:rgba(240,115,22,.10)}
.odsw.obr:hover:not(:disabled){transform:rotate(90deg)}
/* znak zebatki rysuje sie mniejszy niz strzalka — wyrownujemy optycznie */
.odsw.zeb{font-size:30px; padding-bottom:2px}
h1 img{width:42px; height:42px; border-radius:9px; flex:none;
  box-shadow:0 3px 10px rgba(180,90,20,.28)}
h1 .g{background:linear-gradient(105deg,#f07316,#e0521a 48%,#b83a12);
  -webkit-background-clip:text; background-clip:text; color:transparent}
.sub{display:none}
.drop{border:1.5px dashed var(--line); border-radius:18px; padding:22px 18px; text-align:center;
  cursor:pointer; transition:.18s; background:var(--field); position:relative; overflow:hidden}
.drop:hover{border-color:var(--acc); background:rgba(240,115,22,.10)}
.drop .big{font-size:18.5px; font-weight:590; line-height:1.35; overflow-wrap:anywhere}
button{position:relative; width:100%; margin-top:14px; padding:15px; border-radius:18px;
  border:1px solid rgba(255,255,255,.38); color:#fff; font:inherit; font-size:17px; font-weight:590;
  cursor:pointer; overflow:hidden; isolation:isolate;
  background:linear-gradient(180deg,rgba(255,255,255,.34),rgba(255,255,255,.08) 46%,rgba(255,255,255,0) 62%),
             linear-gradient(118deg,rgba(255,138,32,.97),rgba(240,100,20,.93) 55%,rgba(214,64,32,.88));
  backdrop-filter:blur(16px) saturate(200%);
  box-shadow:0 10px 28px rgba(230,110,25,.34), inset 0 1px 0 rgba(255,255,255,.6);
  transition:transform .16s cubic-bezier(.2,.8,.2,1), box-shadow .22s, filter .22s}
button::before{content:''; position:absolute; inset:0; border-radius:inherit; pointer-events:none;
  background:linear-gradient(100deg,transparent 30%,rgba(255,255,255,.42) 48%,transparent 66%);
  transform:translateX(-130%); transition:transform .7s cubic-bezier(.2,.8,.2,1)}
button:hover:not(:disabled)::before{transform:translateX(130%)}
button:hover:not(:disabled){filter:brightness(1.06) saturate(1.06)}
button:active:not(:disabled){filter:brightness(.94)}
button:disabled{opacity:.38; cursor:default}
button.stop{background:linear-gradient(180deg,rgba(255,255,255,.32),rgba(255,255,255,.06) 46%,rgba(255,255,255,0) 62%),
  linear-gradient(118deg,rgba(255,69,58,.95),rgba(255,55,95,.9));
  box-shadow:0 10px 28px rgba(255,69,58,.32), inset 0 1px 0 rgba(255,255,255,.55)}
button.ghost{background:var(--field); color:var(--txt); border:1px solid var(--line);
  margin-top:8px; font-size:15.5px; padding:12px; box-shadow:none; backdrop-filter:blur(10px)}
button.ghost::before{display:none}
.pliki{overflow-y:auto; margin-top:10px; display:none}
.pliki.on{display:block}
.plik{display:flex; align-items:center; gap:8px; padding:5px 9px; border-radius:9px;
  background:var(--field); border:1px solid var(--line); margin-bottom:4px; font-size:13px}
.plik .nz{flex:1 1 auto; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap}
.plik .x{flex:none; cursor:pointer; color:var(--dim); font-size:16px; line-height:1;
  padding:0 3px; border-radius:5px}
.plik .x:hover{color:var(--err); background:rgba(216,69,46,.13)}
.naglowek{display:flex; align-items:center; justify-content:space-between; margin-top:10px;
  font-size:12.5px; color:var(--dim)}
.naglowek a{color:var(--acc); cursor:pointer; text-decoration:none}
.info{font-size:13px; color:var(--dim); margin-top:8px; line-height:1.5}
.info.ostrz{color:var(--warn)}
.opcje{display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:10px; margin-top:14px}
.pole{background:var(--field); border:1px solid var(--line); border-radius:13px; padding:9px 11px}
.pole label{display:block; font-size:11.5px; font-weight:590; text-transform:uppercase;
  letter-spacing:.03em; color:var(--dim); margin-bottom:4px}
.pole input,.pole select{width:100%; border:0; background:transparent; color:var(--txt);
  font:inherit; font-size:15.5px; outline:none}
.chk{display:flex; align-items:center; gap:8px; margin-top:12px; font-size:14.5px; color:var(--dim)}
.chk input{width:15px; height:15px; accent-color:var(--acc)}
.bar{height:30px; border-radius:15px; background:var(--track); overflow:hidden;
  border:1px solid var(--stroke); box-shadow:inset 0 1px 4px rgba(0,0,0,.13)}
.bar .fill{height:100%; width:0; border-radius:15px; position:relative; overflow:hidden;
  background:linear-gradient(180deg,rgba(255,255,255,.48),rgba(255,255,255,.06) 55%,rgba(255,255,255,0)),
             linear-gradient(105deg,#54dd8e,#26c268 58%,#12a457);
  box-shadow:0 2px 12px rgba(34,190,100,.42), inset 0 1px 0 rgba(255,255,255,.75),
             inset 0 0 0 .5px rgba(255,255,255,.25);
  backdrop-filter:blur(8px) saturate(180%);
  transition:width .4s cubic-bezier(.2,.8,.2,1)}
.bar .fill::after{content:''; position:absolute; inset:0;
  background:linear-gradient(100deg,transparent 18%,rgba(255,255,255,.55) 50%,transparent 82%);
  transform:translateX(-100%); animation:plyn 1.5s linear infinite}
.bar.done .fill::after{animation:none; opacity:0}
@keyframes plyn{to{transform:translateX(100%)}}
.czas{font-variant-numeric:tabular-nums; color:var(--txt); font-weight:590; margin-right:12px}
.faza{font-size:12.5px; color:var(--dim); margin-top:5px; min-height:17px;
  overflow:hidden; text-overflow:ellipsis; white-space:nowrap}
.stat{display:flex; justify-content:space-between; font-size:13.5px; color:var(--dim); margin-top:7px;
  font-variant-numeric:tabular-nums}
.log{flex:0 1 180px; min-height:70px; font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
  font-size:12px; color:var(--dim); line-height:1.65; overflow-y:auto; white-space:pre-wrap;
  word-break:break-word; border-top:1px solid var(--line); padding-top:9px}
.wyn{flex:1 1 auto; min-height:64px; overflow-y:auto; padding-right:4px}
.modal{position:fixed; inset:0; z-index:20; display:none; align-items:center;
  justify-content:center; background:rgba(10,25,45,.42); backdrop-filter:blur(7px); padding:24px}
.modal.on{display:flex}
.modalKarta{width:100%; max-width:680px; max-height:88vh; overflow-y:auto;
  background:var(--card); border:1px solid var(--stroke); border-radius:24px; padding:24px;
  backdrop-filter:blur(40px) saturate(180%);
  box-shadow:0 28px 70px rgba(0,0,0,.3), inset 0 1px 0 rgba(255,255,255,.4)}
.aktTxt{font-size:14.5px; line-height:1.55; color:var(--dim); margin-bottom:18px}
.aktTxt b{color:var(--txt)}
button.zielony{background:
  linear-gradient(180deg,rgba(255,255,255,.36),rgba(255,255,255,.08) 46%,rgba(255,255,255,0) 62%),
  linear-gradient(112deg,#54dd8e,#26c268 58%,#12a457);
  box-shadow:0 10px 28px rgba(34,190,100,.4), inset 0 1px 0 rgba(255,255,255,.7)}
.modalTyt{font-size:21px; font-weight:590; letter-spacing:-.02em; margin-bottom:14px;
  display:flex; align-items:baseline; gap:9px}
.wers{font-size:12px; color:var(--dim); font-weight:400}
.powiad{display:none; font-size:13.5px; line-height:1.5; padding:11px 13px; border-radius:13px;
  background:rgba(240,115,22,.13); border:1px solid rgba(240,115,22,.35);
  color:var(--txt); margin-bottom:14px}
.powiad.on{display:block}
.modalKarta .pole{margin-top:11px}
.modalKarta .opcje{margin-top:11px; grid-template-columns:1fr 1fr}
.modalKarta .pelna{grid-column:1 / -1}
.pole select{text-overflow:ellipsis}
.pusto{color:var(--dim); font-size:13.5px; padding:10px 2px}
.row{display:flex; align-items:center; gap:8px; padding:7px 10px; border-radius:11px;
  background:var(--field); border:1px solid var(--line); margin-bottom:5px; font-size:13.5px}
.row .kod{font-weight:590; font-variant-numeric:tabular-nums}
.row .str{color:var(--dim); font-size:12.5px; margin-left:auto}
.row.spr{border-color:rgba(199,116,0,.5); background:rgba(255,170,40,.13)}
.row.wiele{border-color:rgba(240,115,22,.42)}
.badge.wiele{background:rgba(240,115,22,.2); color:var(--acc)}
.badge{font-size:10.5px; font-weight:590; letter-spacing:.03em; text-transform:uppercase;
  padding:3px 7px; border-radius:7px; background:var(--track); color:var(--dim)}
.badge.ok{background:rgba(63,157,74,.18); color:var(--ok)}
.badge.warn{background:rgba(255,170,40,.22); color:var(--warn)}
.err{color:var(--err); font-size:14px; margin-top:10px}
</style></head><body>
<div class="blob b1"></div><div class="blob b2"></div><div class="blob b3"></div>
<div class="card">
  <h1><img src="data:image/png;base64,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" alt=""><span>DEPO<span class="g">SKAN</span></span>
    <button class="odsw zeb" onclick="ustOtworz()" title="Ustawienia">&#9881;</button>
    <button class="odsw obr" style="margin-left:10px" onclick="odswiez()"
            title="Odswiez">&#8635;</button></h1>

  <div class="kolumny">
    <!-- ── LEWA: wybor zdjec i sterowanie ─────────────────────────────── -->
    <div class="lewa">
      <div class="drop" id="drop" onclick="dodaj()">
        <div class="big" id="dropTxt">Wybierz zdjecia</div>
      </div>

      <div class="naglowek" id="naglowek" style="display:none">
        <span id="ilePlikow"></span><a onclick="wyczysc(event)">wyczysc liste</a>
      </div>
      <div class="pliki" id="pliki"></div>

      <div class="dol">
        <div class="err" id="err"></div>
        <button id="go" onclick="start()" disabled>Skanuj</button>
        <button id="stop" class="stop" onclick="stop()" style="display:none">Przerwij</button>

        <div class="podpis">
          <svg class="mak" viewBox="0 0 170 51" role="img" aria-label="MAK">
            <path fill="currentColor" fill-rule="evenodd" d="M0.0,0.0 L0.0,50.92 L15.67,50.92 L15.92,49.58 L15.83,13.58 L16.42,12.92 L39.92,13.0 L42.25,13.25 L43.58,14.08 L45.0,15.67 L45.25,18.08 L45.25,48.33 L45.08,49.5 L45.42,50.92 L61.67,50.92 L61.92,49.75 L61.83,10.33 L60.67,5.75 L59.33,3.75 L57.58,2.08 L55.5,1.0 L53.42,0.75 L52.92,0.5 L52.67,0.0 Z M82.25,0.0 L82.33,1.08 L83.08,2.33 L83.17,3.42 L84.0,4.67 L84.17,5.83 L84.92,7.08 L85.17,8.42 L85.83,9.5 L86.08,10.75 L86.75,11.67 L87.0,13.25 L87.75,14.58 L87.83,15.42 L89.08,18.08 L89.08,18.58 L88.58,19.0 L75.67,18.92 L75.33,19.17 L74.25,21.75 L74.25,22.25 L73.42,24.67 L73.42,25.25 L72.75,26.75 L72.58,28.17 L71.83,29.67 L71.67,31.17 L71.0,32.42 L70.83,33.75 L70.17,35.0 L69.83,36.92 L69.08,38.33 L69.0,39.67 L68.25,40.92 L68.0,42.75 L67.5,43.67 L67.17,45.25 L66.5,46.75 L66.25,48.25 L65.58,49.58 L65.83,50.92 L117.92,50.92 L117.75,48.83 L117.0,47.33 L116.67,45.83 L116.08,45.0 L115.83,43.5 L115.08,42.25 L114.83,40.67 L114.0,39.33 L114.0,38.5 L113.33,37.33 L113.0,35.75 L112.33,34.42 L112.25,33.58 L111.75,32.58 L110.58,28.67 L109.92,27.58 L109.67,26.08 L108.92,24.75 L108.83,23.58 L108.0,22.33 L107.75,20.67 L107.17,19.83 L106.83,18.33 L106.17,17.17 L106.0,16.08 L105.25,14.5 L105.17,13.58 L104.42,12.17 L103.42,8.58 L102.83,7.5 L102.67,6.42 L101.92,5.0 L101.75,3.58 L100.92,2.42 L100.92,1.5 L100.33,0.0 Z M139.42,0.0 L139.33,0.58 L136.83,3.5 L133.25,7.17 L129.92,11.25 L126.0,15.33 L121.0,21.17 L120.75,21.75 L120.75,32.5 L121.25,33.42 L124.0,36.25 L128.25,41.17 L137.08,50.42 L137.17,50.92 L159.58,50.92 L159.5,50.25 L159.0,49.33 L157.58,48.08 L148.83,38.83 L135.67,25.58 L135.92,24.75 L138.92,21.67 L143.0,16.75 L146.17,13.5 L150.0,8.92 L157.08,1.25 L157.67,0.33 L157.67,0.0 Z M22.33,19.08 L21.92,19.58 L21.92,49.83 L22.17,50.92 L38.75,50.92 L39.0,49.42 L39.0,20.58 L38.83,19.42 L38.33,19.0 Z M90.17,24.0 L90.75,24.08 L91.08,24.5 L91.25,25.83 L92.83,29.0 L93.17,30.33 L93.92,31.42 L94.0,32.25 L94.83,33.58 L95.0,34.67 L95.67,35.75 L96.58,38.25 L97.25,39.25 L97.25,39.67 L96.75,40.08 L85.92,40.08 L85.33,39.67 L85.33,39.25 L85.83,38.42 L85.92,37.33 L86.67,36.0 L86.92,34.0 L87.67,32.5 L87.67,31.58 L89.08,27.33 L89.5,24.92 Z M163.83,0.0 L163.83,0.58 L163.0,1.08 L162.0,2.08 L161.75,2.92 L161.17,3.67 L161.17,4.58 L161.83,5.67 L162.0,6.58 L163.33,7.92 L164.42,8.08 L165.5,8.83 L166.17,8.83 L167.17,8.08 L168.25,7.75 L169.33,6.33 L169.92,6.25 L169.92,2.0 L169.42,1.92 L168.67,0.92 L168.17,0.75 L167.83,0.42 L167.83,0.0 Z M164.33,0.83 L166.75,0.83 L167.25,1.0 L167.75,1.75 L168.92,2.75 L169.08,3.17 L169.08,5.5 L168.92,5.92 L166.42,7.75 L165.17,7.58 L163.67,6.67 L162.0,4.58 L162.08,3.83 L162.67,3.25 L162.92,2.25 L163.75,1.67 L163.92,1.17 Z M164.5,2.25 L163.92,3.33 L164.08,5.75 L164.58,5.83 L165.5,4.92 L166.42,5.75 L166.83,5.67 L167.0,5.25 L166.75,4.25 L167.33,3.58 L167.17,3.08 L166.58,2.33 L166.17,2.17 Z"/>
          </svg>
          <span>Stworzono w Makro-Plast<sup>&reg;</sup> przez Kacper Makarewicz
            &nbsp;|&nbsp; ver. <span id="wersjaStopka">—</span></span>
        </div>
      </div>
    </div>

    <!-- ── PRAWA: postep, wyniki, log ─────────────────────────────────── -->
    <div class="prawa">
      <div class="bar" id="bar"><div class="fill" id="fill"></div></div>
      <div class="stat"><span id="etap">gotowy</span>
        <span><span id="czas" class="czas"></span><span id="licz"></span></span></div>
      <div class="faza" id="faza"></div>
      <div class="stat"><span id="zuz" style="font-size:12.5px"></span><span></span></div>

      <div class="sekcja" style="margin-top:14px">Wyniki</div>
      <div class="wyn" id="wyn"><div class="pusto">—</div></div>

      <div class="sekcja" style="margin-top:12px">Log</div>
      <div class="log" id="log"></div>
      <div class="stopka">
        <button class="ghost" id="pokaz" onclick="pokaz()" disabled>Pokaz wyniki w Finderze</button>
      </div>
    </div>
  </div>

<div class="modal" id="modalAkt">
  <div class="modalKarta" style="max-width:430px; text-align:center">
    <div class="modalTyt" style="justify-content:center">Nowa wersja</div>
    <div class="aktTxt" id="aktTxt"></div>
    <button class="zielony" id="aktPobierz">Zaktualizuj i uruchom ponownie</button>
    <button class="ghost" id="aktPozniej"
            onclick="$('modalAkt').classList.remove('on')">Pozniej</button>
  </div>
</div>

<div class="modal" id="modal">
  <div class="modalKarta">
    <div class="modalTyt">Ustawienia<span class="wers" id="wers"></span></div>
    <div class="powiad" id="powiad"></div>

    <div class="pole"><label>Klucz API Google AI Studio</label>
      <input id="uKlucz" type="password" spellcheck="false" placeholder="wklej klucz"></div>

    <div class="opcje">
      <div class="pole pelna"><label>Model</label>
        <select id="uModel">
          <option value="gemini-3.5-flash-lite">gemini-3.5-flash-lite</option>
          <option value="gemini-3.5-flash">gemini-3.5-flash</option>
          <option value="gemini-3.1-flash-lite">gemini-3.1-flash-lite</option>
          <option value="gemini-flash-lite-latest">gemini-flash-lite-latest</option>
        </select></div>
      <div class="pole"><label>Kadrow w zapytaniu</label>
        <input id="uNaReq" type="number" min="1" max="30"></div>
      <div class="pole"><label>Zapytan na minute</label>
        <input id="uRpm" type="number" min="1" max="60"></div>
    </div>

    <button class="ghost" onclick="akt()">Sprawdz aktualizacje</button>
    <div class="info" id="uInfo"></div>

    <button onclick="ustZapisz()">Zapisz</button>
    <button class="ghost" onclick="ustZamknij()">Zamknij</button>
  </div>
</div>
</div>
<script>
let PLIKI = [], TIK = null, UST = {};
const $ = id => document.getElementById(id);
const post = (p, d) => fetch(p, {method:'POST', headers:{'Content-Type':'application/json'},
  body: JSON.stringify(d||{})}).then(r => r.json());

// Kolejne klikniecia DOKLADAJA pliki do listy, nie kasuja jej.
function dodaj(){
  post('/api/wybierz').then(d => {
    if (d.error) { $('err').textContent = d.error; return; }
    $('err').textContent = '';
    const przed = PLIKI.length;
    (d.pliki || []).forEach(p => { if (!PLIKI.includes(p)) PLIKI.push(p); });
    if (PLIKI.length === przed && (d.pliki||[]).length)
      $('err').textContent = 'te pliki juz sa na liscie';
    rysuj();
  });
}
function usun(i, e){ e.stopPropagation(); PLIKI.splice(i, 1); rysuj(); }
function wyczysc(e){ e.stopPropagation(); PLIKI = []; rysuj(); }

function rysuj(){
  const box = $('pliki'), nag = $('naglowek');
  box.innerHTML = '';
  if (!PLIKI.length){
    box.classList.remove('on'); nag.style.display = 'none';
    $('dropTxt').textContent = 'Wybierz zdjecia';
    $('go').disabled = true; return;
  }
  box.classList.add('on'); nag.style.display = 'flex';
  $('dropTxt').textContent = PLIKI.length + ' ' +
    (PLIKI.length === 1 ? 'zdjecie' : (PLIKI.length < 5 ? 'zdjecia' : 'zdjec'));
  $('ilePlikow').textContent = PLIKI.length + ' na liscie';
  PLIKI.forEach((p, i) => {
    const d = document.createElement('div');
    d.className = 'plik';
    d.innerHTML = '<span class="nz"></span><span class="x">×</span>';
    d.querySelector('.nz').textContent = p.split('/').pop();
    d.querySelector('.x').onclick = e => usun(i, e);
    box.appendChild(d);
  });
  $('go').disabled = false;
}
const pokaz = () => post('/api/pokaz', {});

// Czysci liste, logi i wyniki — trzy ustawienia (model, kadry, zapytania) zostaja.
function odswiez(){
  post('/api/reset').then(d => {
    if (d.error) { $('err').textContent = d.error; return; }
    if (TIK) { clearInterval(TIK); TIK = null; }
    PLIKI = [];
    $('err').textContent = '';
    $('log').textContent = '';
    $('wyn').innerHTML = '<div class="pusto">—</div>';
    $('etap').textContent = 'gotowy';
    $('licz').textContent = ''; $('faza').textContent = ''; $('czas').textContent = '';
    $('fill').style.width = '0%';
    $('bar').classList.remove('done');
    $('pokaz').disabled = true;
    $('go').style.display = 'block';
    $('stop').style.display = 'none';
    rysuj();
  });
}

// Po zmianie modelu pobieramy jego limity i USTAWIAMY inputy — zabezpieczenie
// przed przekroczeniem. RPM/RPD nie ma w API, wiec ida z tabeli w kodzie;
// z API bierzemy tylko limit tokenow wejscia.
// ── ustawienia ──────────────────────────────────────────────────────────
function ustWczytaj(pokazJesliBrak){
  return post('/api/ustawienia').then(U => {
    UST = U;
    $('uKlucz').value = U.klucz || '';
    $('uNaReq').value = U.na_req;
    $('uRpm').value   = U.rpm;
    $('wers').textContent = 'v' + U.wersja;
    $('wersjaStopka').textContent = U.wersja;
    const sel = $('uModel');
    if (![...sel.options].some(o => o.value === U.model)){
      const o = document.createElement('option');
      o.value = o.textContent = U.model; sel.appendChild(o);
    }
    sel.value = U.model;
    if (U.ma_klucz) modele();           // odswiez liste modeli z konta
    if (pokazJesliBrak && !U.ma_klucz){
      $('powiad').textContent = 'Zeby zaczac, wklej klucz API z Google AI Studio ' +
        '(aistudio.google.com/apikey). Reszta ustawien jest juz gotowa.';
      $('powiad').classList.add('on');
      ustOtworz();
    }
    return U;
  });
}
function ustOtworz(){ $('modal').classList.add('on'); }
function ustZamknij(){ $('modal').classList.remove('on'); }
function ustZapisz(){
  post('/api/ustawienia', {zapisz: 1, klucz: $('uKlucz').value.trim(),
    model: $('uModel').value, na_req: +$('uNaReq').value,
    rpm: +$('uRpm').value}).then(U => {
    UST = U;
    if (U.ma_klucz){ modele(); $('powiad').classList.remove('on'); ustZamknij(); }
    else { $('powiad').textContent = 'Klucz jest pusty — bez niego nic nie odczytam.';
           $('powiad').classList.add('on'); }
  });
}
// przy starcie sprawdzamy po cichu; modal pokazujemy tylko gdy faktycznie jest nowsza
function aktStart(){
  post('/api/aktualizacja').then(a => {
    if (a.blad || !a.nowsza) return;
    $('aktTxt').innerHTML = 'Dostepna jest wersja <b>' + a.najnowsza +
      '</b>.<br>Masz zainstalowana ' + a.wersja + '.';
    $('aktPobierz').onclick = () => zaktualizuj(a);
    $('modalAkt').classList.add('on');
  });
}
// Pobiera nowe wydanie, podmienia aplikacje w miejscu i uruchamia ja ponownie.
function zaktualizuj(a){
  const b = $('aktPobierz');
  b.disabled = true; $('aktPozniej').disabled = true;
  b.textContent = 'Pobieram…';
  $('aktTxt').innerHTML = 'Trwa pobieranie wersji <b>' + a.najnowsza +
    '</b>. Aplikacja zamknie sie i otworzy ponownie sama.';
  post('/api/zaktualizuj').then(w => {
    if (w.error || !w.ok){
      b.disabled = false; $('aktPozniej').disabled = false;
      b.textContent = 'Zaktualizuj i uruchom ponownie';
      $('aktTxt').textContent = w.error || w.info || 'nie udalo sie zaktualizowac';
      return;
    }
    b.textContent = 'Podmieniam…';
    $('aktTxt').innerHTML = 'Za chwile aplikacja uruchomi sie w wersji <b>' +
      w.wersja + '</b>.';
  }).catch(() => {   // serwer znika w trakcie podmiany — to normalne
    b.textContent = 'Podmieniam…';
  });
}

function akt(){
  $('uInfo').textContent = 'sprawdzam…';
  post('/api/aktualizacja').then(a => {
    if (a.blad){ $('uInfo').textContent = 'nie sprawdzilem: ' + a.blad; return; }
    if (a.nowsza){
      $('uInfo').textContent = '';
      $('aktTxt').innerHTML = 'Dostepna jest wersja <b>' + a.najnowsza +
        '</b>.<br>Masz zainstalowana ' + a.wersja + '.';
      $('aktPobierz').onclick = () => zaktualizuj(a);
      ustZamknij();
      $('modalAkt').classList.add('on');
    } else $('uInfo').textContent = 'masz najnowsza wersje (' + a.wersja + ')';
  });
}

// Aktualna lista modeli z konta — pobierana po cichu przy starcie.
// To zapytanie o metadane, nie o generowanie, wiec nie zjada limitu odczytow.
function modele(){
  return post('/api/modele').then(d => {
    if (d.error || !(d.modele||[]).length) return;      // bez klucza po prostu zostaje lista wbudowana
    const sel = $('uModel'), byl = UST.model || sel.value;
    sel.innerHTML = '';
    d.modele.forEach(m => { const o = document.createElement('option');
      o.value = o.textContent = m; sel.appendChild(o); });
    const jest = w => [...sel.options].some(o => o.value === w);
    sel.value = jest(byl) ? byl
              : jest('gemini-3.5-flash-lite') ? 'gemini-3.5-flash-lite'
              : ([...sel.options].find(o => /flash-lite/.test(o.value))
                 || sel.options[0] || {value: ''}).value;
    UST.model = sel.value;
  }).catch(() => {});
}

function start(){
  $('err').textContent = ''; $('wyn').innerHTML = '';
  $('pokaz').disabled = true;
  $('bar').classList.remove('done');
  post('/api/start', {pliki: PLIKI, model: UST.model, na_req: +UST.na_req,
    rpm: +UST.rpm}).then(d => {
    if (d.error) { $('err').textContent = d.error; return; }
    $('go').style.display = 'none'; $('stop').style.display = 'block';
    TIK = setInterval(tik, 700);
  });
}
const stop = () => post('/api/stop');

function tik(){
  post('/api/stan').then(s => {
    $('etap').textContent = s.etap + (s.req ? '  ·  zap. ' + s.req : '');
    $('faza').textContent = s.faza || '';
    const t = s.trwa || 0;
    $('czas').textContent = t ? String(Math.floor(t/60)).padStart(2,'0') + ':' +
                                String(t%60).padStart(2,'0') : '';
    $('licz').textContent = s.ile ? s.zrobione + ' / ' + s.ile : '';
    $('fill').style.width = (s.ile ? 100 * s.zrobione / s.ile : 0) + '%';
    const l = $('log'); const dol = l.scrollTop + l.clientHeight >= l.scrollHeight - 20;
    l.textContent = (s.log||[]).join('\n');
    if (dol) l.scrollTop = l.scrollHeight;
    if (s.gotowe){
      clearInterval(TIK); TIK = null;
      $('go').style.display = 'block'; $('stop').style.display = 'none';
      $('bar').classList.add('done'); $('fill').style.width = '100%';
      $('pokaz').disabled = false;
      post('/api/zuzycie').then(d => { if (d.dzis != null) licznik(d.dzis, d.data); });
      const w = $('wyn'); w.innerHTML = '';
      if (!(s.wyniki||[]).length) w.innerHTML = '<div class="pusto">brak wynikow</div>';
      (s.wyniki||[]).forEach(r => {
        const d = document.createElement('div');
        const wiele = r.kod.endsWith('+wiele');
        d.className = 'row' + (r.kod === '?' ? ' spr' : (wiele ? ' wiele' : ''));
        const bad = r.kod === '?' ? 'warn">sprawdz' : (wiele ? 'wiele">wiele etykiet' : 'ok">ok');
        d.innerHTML = '<span class="badge ' + bad + '</span><span class="kod">' +
          (r.kod === '?' ? '—' : r.kod.replace('+wiele','')) +
          '</span><span class="str">' + r.nowy + '</span>';
        w.appendChild(d);
      });
    }
  });
}
// UWAGA: listy modeli NIE pobieramy automatycznie — to byloby zapytanie do API
// przy kazdym otwarciu okna. Pobiera sie tylko po kliknieciu przycisku.
post('/api/zuzycie').then(d => { if (d.dzis != null) licznik(d.dzis, d.data); });
$('uModel').onchange = () => UST.model  = $('uModel').value;
$('uNaReq').oninput  = () => UST.na_req = +$('uNaReq').value;
$('uRpm').oninput    = () => UST.rpm    = +$('uRpm').value;
ustWczytaj(true);          // pierwszy start otworzy Ustawienia
aktStart();                // ciche sprawdzenie aktualizacji

function licznik(n, data){
  $('zuz').textContent = 'dzis: ' + n;
}
</script></body></html>"""


# ══════════════════════════════════════════════════════════════════════════
#  START
# ══════════════════════════════════════════════════════════════════════════
def wolny_port():
    for p in range(PORT, PORT + 20):
        try:
            s = http.server.ThreadingHTTPServer(('127.0.0.1', p), H)
        except OSError:
            try:                                  # zajety przez nasza wczesniejsza instancje?
                with socket.create_connection(('127.0.0.1', p), timeout=.4):
                    return p
            except OSError:
                continue
        threading.Thread(target=s.serve_forever, daemon=True).start()
        return p
    raise RuntimeError('brak wolnego portu')


def main():
    port = wolny_port()
    try:
        import webview
        global OKNO
        OKNO = webview.create_window('DEPOSKAN', f'http://127.0.0.1:{port}/',
                                     width=1300, height=880, min_size=(980, 640))
        webview.start()
    except ImportError:                            # bez pywebview — otwieramy przegladarke
        import webbrowser
        webbrowser.open(f'http://127.0.0.1:{port}/')
        print(f'DEPOSKAN: http://127.0.0.1:{port}/   (Ctrl+C konczy)')
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass


if __name__ == '__main__':
    main()
