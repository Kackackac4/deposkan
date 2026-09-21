#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MakroSkan (dawniej DEPOSKAN) — rozpoznawanie zdjec dla dwoch dzialow, przelaczane
na gorze okna:

  DEPO     — recznie pisane CZERWONE numery zamowien na paczkach.
             1. OpenCV lokalnie znajduje czerwony numer i przycina kadr (~0.1 s/zdjecie).
             2. Kadry ida paczkami do Gemini, ktore odsyla odczytane kody.
             3. Kopie oryginalow dostaja nazwy DSCxxxx_KOD.JPG w "Kopia z kodami".

  ALUPROF  — WYDRUKOWANE oznaczenie profilu (lista profili ponizej / w Ustawieniach).
             1. Wskazujesz folder; program zbiera zdjecia rowniez z podfolderow
                i z archiwow (.zip, .rar, .7z, .tar...).
             2. Zdjecia sa kompresowane (mniejsze, ale napis zostaje czytelny) —
                bez kadrowania, bo napis moze byc w dowolnym miejscu.
             3. Gemini wskazuje produkt z listy; kopie oryginalow z nazwa produktu
                trafiaja do "<folder> MakroSkan" razem z raportem.

Wszystko w jednym pliku: detekcja, wysylka do Gemini, zmiana nazw, serwer lokalny
i interfejs. Zamkniecie okna konczy program.

Copyright (c) 2026 MAKRO-PLAST Sp. z o.o. — wszelkie prawa zastrzezone.
Oprogramowanie wlasnosciowe, do uzytku wewnetrznego firmy. Patrz plik LICENSE.

Uwaga o limitach: darmowy tier ma niski limit requestow na dobe, dlatego zdjecia
leca paczkami (kilka obrazow w jednym requescie), a postep zapisuje sie na dysk —
gdy limit sie skonczy, nastepnego dnia program dokonczy od miejsca przerwania.
"""
import base64, http.server, json, os, re, shutil, socket, subprocess, sys, tempfile
import threading, time, urllib.error, urllib.request, zipfile

import cv2
import numpy as np

# ══════════════════════════════════════════════════════════════════════════
#  USTAWIENIA
# ══════════════════════════════════════════════════════════════════════════
WERSJA = '1.2.0'
NAZWA  = 'MakroSkan'
REPO   = 'Kackackac4/deposkan'      # do sprawdzania aktualizacji na GitHubie
# Pliki wydania (DEPOSKAN.exe, DEPOSKAN-macOS.zip) i katalog ustawien zostaja pod stara
# nazwa — zainstalowane kopie aktualizuja sie po tych nazwach, a klucz API lezy
# w starym katalogu. Zmienia sie tylko to, co widac w oknie.

# Domyslne ustawienia — uzytkownik zmienia je w oknie Ustawienia, zapisuja sie na dysk.
DOMYSLNE = {
    'klucz':  '',                      # klucz API wpisuje sie w aplikacji, NIE w kodzie
    'model':  'gemini-3.5-flash-lite',
    'na_req': 25,                      # kadrow w jednym zapytaniu
    'rpm':    15,                      # zapytan na minute
    'tryb':   'depo',                  # 'depo' albo 'alu' — ostatnio wybrany przelacznik
    'na_req_alu': 10,                  # calych zdjec ALUPROF w jednym zapytaniu
    'profile': '',                     # wlasna lista profili ALUPROF; pusta = PROFILE_ALUPROF
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
STAN_ALU_FILE = os.path.join(CFG_DIR, 'stan_alu.json')


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
        orig = wczytaj_obraz(sciezka)
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


AKT = {'trwa': False, 'etap': '', 'procent': 0, 'mb': 0.0, 'mb_calosc': 0.0,
       'blad': '', 'gotowe': False, 'wersja': '', 'log': ''}


def akt_stan(**co):
    with BLOKADA:
        AKT.update(co)


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
        akt_stan(etap='pobieram', mb_calosc=round(calosc / 1048576, 1))
        while True:
            kawalek = r.read(262144)
            if not kawalek:
                break
            f.write(kawalek)
            mam += len(kawalek)
            akt_stan(mb=round(mam / 1048576, 1),
                     procent=(100 * mam // calosc) if calosc else 0)

    if not (suma_url and nazwa):
        return True
    try:
        with urllib.request.urlopen(urllib.request.Request(
                suma_url, headers={'User-Agent': 'deposkan'}), timeout=30) as r:
            sumy = r.read().decode()
    except Exception:
        return True                       # brak pliku sum — nie blokujemy aktualizacji
    import hashlib
    akt_stan(etap='sprawdzam sumę kontrolną', procent=100)
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


SKRYPT_WIN = r"""
$ErrorActionPreference = "Stop"
$log   = "@@LOG@@"
$stary = "@@STARY@@"
$nowy  = "@@NOWY@@"
$stare_pid = @@PID@@

function Zapisz($t) { "$(Get-Date -Format 'HH:mm:ss')  $t" | Out-File -FilePath $log -Append -Encoding utf8 }
Zapisz "start; podmieniam $stary"

# 1. czekamy, az aplikacja naprawde zniknie — najpierw po PID, potem po nazwie,
#    bo PyInstaller w trybie onefile uruchamia proces potomny o tej samej nazwie
try { Wait-Process -Id $stare_pid -Timeout 60 -ErrorAction SilentlyContinue } catch {}
for ($i = 0; $i -lt 60; $i++) {
    $zyje = Get-Process -Name DEPOSKAN -ErrorAction SilentlyContinue
    if (-not $zyje) { break }
    Start-Sleep -Milliseconds 500
}
Start-Sleep -Milliseconds 800
Zapisz "aplikacja zamknieta"

# 2. dzialajacego pliku .exe nie da sie nadpisac, ale MOZNA go przemianowac —
#    dlatego najpierw odsuwamy stary na bok, dopiero potem kopiujemy nowy
$odsuniety = "$stary.stary"
if (Test-Path $odsuniety) { Remove-Item -LiteralPath $odsuniety -Force -ErrorAction SilentlyContinue }

$udalo = $false
for ($i = 0; $i -lt 40; $i++) {
    try {
        if (Test-Path $stary) { Move-Item -LiteralPath $stary -Destination $odsuniety -Force }
        Copy-Item -LiteralPath $nowy -Destination $stary -Force
        $udalo = $true
        break
    } catch {
        Zapisz "proba $($i+1): $($_.Exception.Message)"
        # gdy kopiowanie padlo po przemianowaniu, wracamy do stanu wyjsciowego
        if ((Test-Path $odsuniety) -and -not (Test-Path $stary)) {
            Move-Item -LiteralPath $odsuniety -Destination $stary -Force -ErrorAction SilentlyContinue
        }
        Start-Sleep -Seconds 1
    }
}

if (-not $udalo) {
    Zapisz "NIE UDALO SIE podmienic pliku"
    if ((Test-Path $odsuniety) -and -not (Test-Path $stary)) {
        Move-Item -LiteralPath $odsuniety -Destination $stary -Force -ErrorAction SilentlyContinue
    }
    if (Test-Path $stary) { Start-Process -FilePath $stary }
    exit 1
}

Zapisz "podmieniono, uruchamiam"
Start-Process -FilePath $stary
Start-Sleep -Seconds 3
Remove-Item -LiteralPath $odsuniety -Force -ErrorAction SilentlyContinue
Zapisz "gotowe"
"""


def zaktualizuj_w_tle():
    """Cala aktualizacja w osobnym watku — interfejs odpytuje o postep."""
    try:
        w = zaktualizuj()
        if w.get('ok'):
            akt_stan(etap='uruchamiam ponownie', gotowe=True, wersja=w['wersja'], trwa=False)
            # na Windows proces potomny PyInstallera potrzebuje chwili wiecej
            threading.Timer(2.5 if sys.platform == 'win32' else 1.5,
                            lambda: os._exit(0)).start()
        else:
            akt_stan(etap='', blad=w.get('info', 'nie udało się'), trwa=False)
    except Exception as e:
        akt_stan(etap='', blad=f'{type(e).__name__}: {e}', trwa=False)


def zaktualizuj():
    """Pobiera nowe wydanie i podmienia zainstalowana aplikacje w miejscu."""
    cel = sciezka_aplikacji()
    if not cel:
        raise RuntimeError('aktualizacja działa tylko w zainstalowanej aplikacji')

    a = sprawdz_aktualizacje()
    if a.get('blad'):
        raise RuntimeError(a['blad'])
    if not a.get('nowsza'):
        return {'ok': False, 'info': 'masz już najnowszą wersję'}

    tmp = tempfile.mkdtemp(prefix='deposkan-akt-')
    nazwa = os.path.basename(a['link'].split('?')[0])
    paczka = os.path.join(tmp, nazwa)
    sumy = f"https://github.com/{REPO}/releases/download/v{a['najnowsza']}/checksums.txt"
    if not pobierz_z_kontrola(a['link'], paczka, sumy, nazwa):
        shutil.rmtree(tmp, ignore_errors=True)
        raise RuntimeError('suma kontrolna pobranego pliku się nie zgadza')

    akt_stan(etap='podmieniam aplikację', procent=100)
    if sys.platform == 'darwin':
        skrypt = os.path.join(tmp, 'podmien.sh')
        open(skrypt, 'w').write(f"""#!/bin/bash
# czekamy, az aplikacja sie zamknie, potem podmieniamy bundle i uruchamiamy na nowo
for _ in $(seq 1 120); do
  pgrep -f "{cel}/Contents/MacOS/" >/dev/null || break
  sleep 0.5
done
ditto -x -k "{paczka}" "{tmp}/rozpakowane" || exit 1
# bierzemy pierwszy bundle z paczki, jakkolwiek sie nazywa — dzieki temu przyszla
# zmiana nazwy pliku wydania nie zepsuje aktualizacji juz zainstalowanych kopii
APP=$(ls -d "{tmp}/rozpakowane/"*.app 2>/dev/null | head -n 1)
[ -n "$APP" ] || exit 1
rm -rf "{cel}"
ditto "$APP" "{cel}"

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
        # skrypt kladziemy POZA katalogiem tymczasowym pobrania — poprzednia wersja
        # kasowala katalog, w ktorym sama sie wykonywala
        skrypt = os.path.join(tempfile.gettempdir(), 'deposkan-podmien.ps1')
        log = os.path.join(tempfile.gettempdir(), 'deposkan-aktualizacja.log')
        tresc = (SKRYPT_WIN.replace('@@LOG@@', log)
                           .replace('@@STARY@@', cel)
                           .replace('@@NOWY@@', paczka)
                           .replace('@@PID@@', str(os.getpid())))
        with open(skrypt, 'w', encoding='utf-8') as f:
            f.write(tresc)
        akt_stan(log=log)
        subprocess.Popen(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                          '-WindowStyle', 'Hidden', '-File', skrypt],
                         creationflags=0x00000008 | 0x00000200)   # odlaczony, wlasna grupa

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
        im = wczytaj_obraz(sciezka)
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
    'faza': '', 'start': 0.0, 'trwa': 0, 'tryb': 'depo', 'produkty': [],
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
            log(f'wznawiam — {len(wczesniej)} zdjęć było już odczytanych')

        do_zrobienia = [p for p in pliki if os.path.basename(p) not in mapa]
        with BLOKADA:
            STAN.update(ile=len(do_zrobienia), zrobione=0, folder=folder, etap='kadrowanie')

        # ── 1. kadrowanie lokalnie ───────────────────────────────────────
        kadry, bez_czerwieni = [], []
        for nr_p, p in enumerate(do_zrobienia, 1):
            if STAN['stop']:
                break
            faza(f'kadruję {nr_p}/{len(do_zrobienia)}: {os.path.basename(p)}')
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
        log(f'{len(paczki)} zapytań do {model}, co {odstep:.0f} s')

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
                    faza(f'paczka {nr}/{len(paczki)} — wysłane {len(paczka)} kadrów, '
                         f'czekam na odpowiedź…')
                    kody = czytaj_paczke([j for _, j in paczka], model)
                    faza(f'paczka {nr}/{len(paczki)} — odpowiedź po '
                         f'{time.time()-ostatnie:.0f} s')
                    zuzycie(1)                       # doliczamy do licznika dobowego
                    with BLOKADA:
                        STAN['req'] += 1
                except urllib.error.HTTPError as e:
                    tresc = e.read().decode('utf-8', 'replace')[:200]
                    if e.code in (429, 500, 503):        # limit albo chwilowy blad
                        proba += 1
                        pauza = min(60, 5 * 2 ** proba)
                        log(f'paczka {nr}: HTTP {e.code}, czekam {pauza} s (próba {proba}/3)')
                        if e.code == 429 and proba >= 3:
                            log('LIMIT WYCZERPANY — zapisuję to, co już odczytane, '
                                'reszta czeka na następny raz')
                            brak_limitu = True
                            break
                        for _ in range(pauza * 2):
                            if STAN['stop']:
                                break
                            time.sleep(0.5)
                    else:
                        log(f'paczka {nr}: HTTP {e.code} {tresc} — kończę odczyt, '
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
            log(f'drugie podejście: {len(nieudane)} zdjęć w całości, '
                f'kontekst {len(znane)} znanych numerów')
            try:
                # przerwa na limit RPM przed dodatkowym zapytaniem
                czekaj = odstep - (time.time() - ostatnie)
                while czekaj > 0 and not STAN['stop']:
                    faza(f'pauza na limit — {int(czekaj)+1} s do drugiego podejścia')
                    time.sleep(min(0.5, czekaj))
                    czekaj = odstep - (time.time() - ostatnie)

                for i in range(0, len(nieudane), 30):     # 30 zdjec = bezpieczny rozmiar zapytania
                    czesc = nieudane[i:i+30]
                    jpgi = [j for j in (orig_do_jpg(x) for x in czesc) if j]
                    if not jpgi:
                        continue
                    ostatnie = time.time()
                    faza(f'drugie podejście — {len(jpgi)} pełnych zdjęć, czekam…')
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
                    log(f'drugie podejście: odzyskano {odzysk}/{len(czesc)}')
                    stan_zapisz(folder, mapa)
            except Exception as e:
                log(f'drugie podejście nieudane: {type(e).__name__}: {e}')
            with BLOKADA:
                STAN['zrobione'] = 1

        # ── 3. zmiana nazw ───────────────────────────────────────────────
        # kopiowanie kilkuset plikow po kilka MB trwa — pokazujemy postep,
        # zeby pasek nie stal na 100% przez ostatnia jedna trzecia czasu
        with BLOKADA:
            STAN.update(etap='zapisywanie', ile=len(pliki), zrobione=0)
        faza('zapisuję pliki…')
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
                faza(f'zapisuję {nr_p}/{len(pliki)}: {nazwa}')
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
                log(f'nie udało się {nazwa}: {e}')

        with BLOKADA:
            STAN['wyniki'] = wyniki
        if brak_limitu:
            zostalo = len([p for p in pliki if os.path.basename(p) not in mapa])
            log(f'PRZERWANE PRZEZ LIMIT — nazwane {ile_ok + ile_spr} plików, '
                f'{zostalo} czeka. Uruchom ponownie na tym samym folderze, '
                f'program dokończy od tego miejsca.')
        log(f'GOTOWE — rozpoznane {ile_ok}'
            + (f' (w tym {ile_wiele} z wieloma etykietami)' if ile_wiele else '')
            + f', do sprawdzenia {ile_spr}, zapytań {STAN["req"]}')
        log(f'pliki w: {cel_dir}')

    except Exception as e:
        log(f'BŁĄD: {type(e).__name__}: {e}')
    finally:
        with BLOKADA:
            STAN.update(pracuje=False, gotowe=True, etap='koniec', faza='',
                        trwa=int(time.time() - STAN['start']) if STAN['start'] else 0)


# ══════════════════════════════════════════════════════════════════════════
#  ALUPROF — wydrukowane oznaczenia profili na calych zdjeciach
# ══════════════════════════════════════════════════════════════════════════
# Domyslna lista; w Ustawieniach mozna ja podmienic (jedna pozycja w linii).
# Nazwy zapisujemy DOKLADNIE tak, jak sa w systemie — z kropkami i dopiskami.
PROFILE_ALUPROF = """USZCZ/GU/LDG_5311_U8/LD/20
U6/MR/20
USZCZ/GU/PP89_5014/C_U3/PR
USZCZ/GU/LDG/DU 7,15m
USZCZ/GU/LDG/DU 6,15m
UPPF/20
USZCZ/GU/LDG52OPT_5192
USZCZ/GU/LDG/D 5,7 m
PUY208.0501.6
PUY208.0502.6
PUY107.4404.6
PUY107.4405.6
PUY107.4406.6
PUY107.2257.6
PUY107.4407.6
PUY107.2239.6
PUY107.4408.6.
PUY208.0180.6
U1/PR
P/KMO/20
P/KMO/20 pak
APPDRA 79/02
APPDRA 79/02 pak
APPDRA 79/08
APPDRA 79/08 pak
APPRA 45/02
APPRA 45/02 pak
APPRA 45/08
APPRA 45/08 pak
PP 60/12/BU/02
PP 60/12/BU/08
PP 60/12/BU/08 F.
PP 60/17/BU/02
PP 60/17/BU/08
PP 60/17/BU/08 F.
PPD 60/12/02
PPD 60/12/02 K.
PPD 60/12/08
PPD 60/12/08 K.
PPD 60/12/08 KF.
PPD 60/17/BU/02
PPD 60/17/BU/08
PPD 60/17/BU/08 F.
PPDMW 60/12/BU/02
PPDMW 60/12/BU/08
PPDMW 60/12/BU/08 F.
PPDMW 60/17/BU/02
PPDMW 60/17/BU/08
PPM 60/12/BU/02
PPM 60/12/BU/08
PPM 60/12/BU/08 F.
PPM 60/17/02
PPM 60/17/08
PPMRA/12/BU/02
PPMRA/12/BU/08
PPMRA/17/BU/02
PPMRA/17/BU/08
PPRA/12/BU/02
PPRA/12/BU/08
PPRA/17/BU/02
PPRA/17/BU/08
PSB 170/02 pak
PSB 170/02.
PSB 170/08 F.
PSB 170/08 pak
PSB 170/08.
PSB 210/02
PSB 210/02 pak
PSB 210/08
PSB 210/08 F.
PSB 210/08 pak
PSBO 170/02
PSBO 170/02 pak
PSBO 170/08
PSBO 170/08 pak
PSBO 210/02
PSBO 210/02 pak
PSBO 210/08
PSBO 210/08 pak
PSBO 240/02
PSBO 240/02 pak
PSBO 240/08
PSBO 240/08 pak
PSD RA/02
PSD/02
PSD/02 pak
PSD/08
PSD/08 F
PSD/08 pak
PSDW RA/02
PSG 230/02
PSG 230/02 pak.
PSG 230/08
PSG 230/08 pak
PSRD 100/02
PSRD 100/02 pak
PSRD 100/08
PSRD 100/08 F
PSRD 100/08 pak
WUF/20
PPM 60/17/BU/08 F.
USZCZ/GU/PP66_5012/B_U2/PR
USZCZ/GU/LDG/D 7,2 m
USZCZ/UGO/LDG/D/20 7,0 m
USZCZ/UGO/LDG/D/20 6,0 m
ZHPMZN/20
ZHLDMZN/20
PUY107.2239.6000
NZIP/B/20 pak.
PMMPH/20
PSDW_F
PD 351.01.00A
PPD 60/12/02 K. PRZEMIAŁ
PPD 60/12/08 K. Przemiał
PPD 60/12/08 KF. Przemiał"""

ROZSZ_ALU = ROZSZ | {'.heif', '.tif', '.tiff', '.bmp'}
ARCHIWA   = ('.zip', '.rar', '.7z', '.tar', '.tgz', '.tar.gz', '.tar.bz2', '.tar.xz')
DOPISEK   = ' MakroSkan'   # folder wynikowy: "<nazwa folderu> MakroSkan"

ALU_PX, ALU_JAKOSC   = 1600, 82   # kompresja do pierwszego odczytu: ~150-300 KB/zdjecie
ALU_PX2, ALU_JAKOSC2 = 2400, 86   # drugie podejscie dla nierozpoznanych — wiecej szczegolu
ALU_NA_REQ2          = 4


def lista_profili():
    wlasna = (cfg_wczytaj().get('profile') or '').strip()
    linie = (wlasna or PROFILE_ALUPROF).splitlines()
    return list(dict.fromkeys(x.strip() for x in linie if x.strip()))


def norm_profilu(s):
    """Klucz porownania: bez spacji, kropek, myslnikow i wielkosci liter."""
    return re.sub(r'[\s.\-_]+', '', (s or '').upper())


def wczytaj_obraz(p):
    """cv2.imread nie radzi sobie z polskimi znakami w sciezce na Windows — czytamy bajty."""
    try:
        im = cv2.imdecode(np.fromfile(p, dtype=np.uint8), cv2.IMREAD_COLOR)
    except Exception:
        im = None
    if im is None and sys.platform == 'darwin' and \
            os.path.splitext(p)[1].lower() in ('.heic', '.heif'):
        # HEIC z iPhone'a: OpenCV go nie czyta, macOS ma wbudowany konwerter
        tmp = tempfile.mktemp(suffix='.jpg', prefix='makroskan-')
        try:
            subprocess.run(['sips', '-s', 'format', 'jpeg', p, '--out', tmp],
                           capture_output=True, timeout=60)
            im = cv2.imdecode(np.fromfile(tmp, dtype=np.uint8), cv2.IMREAD_COLOR)
        except Exception:
            im = None
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
    return im


def obraz_do_jpg(im, dl_boku, jakosc):
    r = dl_boku / max(im.shape[:2])
    if r < 1:
        im = cv2.resize(im, None, fx=r, fy=r, interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode('.jpg', im, [cv2.IMWRITE_JPEG_QUALITY, jakosc])
    return buf.tobytes() if ok else None


def rozpakuj(archiwum, cel):
    """ZIP rozpakowuje Python; reszte systemowy tar (bsdtar na macOS i Windows 10+)."""
    os.makedirs(cel, exist_ok=True)
    if archiwum.lower().endswith('.zip'):
        with zipfile.ZipFile(archiwum) as z:
            z.extractall(cel)             # extractall sam odcina sciezki typu ../
        return
    r = subprocess.run(['tar', '-xf', archiwum, '-C', cel], capture_output=True, text=True,
                       timeout=600, creationflags=0x08000000 if sys.platform == 'win32' else 0)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or 'tar nie rozpakował').strip()[:160])


def ukryty(n):
    return n.startswith('.') or n == '__MACOSX' or n == 'Thumbs.db'


def policz_folder(folder):
    """Szybkie liczenie do podgladu po wyborze folderu — bez rozpakowywania."""
    zdj = arch = 0
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if not ukryty(d) and not d.endswith(DOPISEK)]
        for n in files:
            if ukryty(n):
                continue
            if os.path.splitext(n)[1].lower() in ROZSZ_ALU:
                zdj += 1
            elif n.lower().endswith(ARCHIWA):
                arch += 1
    return zdj, arch


def zbierz_zdjecia(folder, tmp):
    """Zwraca liste (klucz, sciezka). Klucz to sciezka wzgledna — dla zdjec z archiwum
    'paczka.zip/podfolder/x.jpg' — stala miedzy uruchomieniami, wiec wznawianie dziala."""
    wpisy, bledy, licznik = [], [], [0]

    def przejdz(katalog, prefiks, glebokosc):
        for root, dirs, files in os.walk(katalog):
            dirs[:] = sorted(d for d in dirs if not ukryty(d) and not d.endswith(DOPISEK))
            for n in sorted(files):
                if ukryty(n):
                    continue
                p = os.path.join(root, n)
                rel = os.path.join(prefiks, os.path.relpath(p, katalog)).replace('\\', '/')
                if os.path.splitext(n)[1].lower() in ROZSZ_ALU:
                    wpisy.append((rel, p))
                elif n.lower().endswith(ARCHIWA):
                    if glebokosc >= 3:
                        bledy.append(f'pomijam archiwum w archiwum w archiwum: {rel}')
                        continue
                    licznik[0] += 1
                    cel = os.path.join(tmp, f'a{licznik[0]}')
                    faza(f'rozpakowuję {rel}')
                    try:
                        rozpakuj(p, cel)
                        log(f'rozpakowano: {rel}')
                        przejdz(cel, rel, glebokosc + 1)
                    except Exception as e:
                        bledy.append(f'nie rozpakowałem {rel}: {e}')

    przejdz(folder, '', 0)
    return wpisy, bledy


PROMPT_ALU = """@@WSTEP@@

Na zdjeciach sa profile ALUPROF (aluminiowe / PCV), ich paczki albo etykiety. Na kazdym
gdzies jest WYDRUKOWANY napis z oznaczeniem profilu — na samym profilu, na folii
ochronnej, na etykiecie paczki albo na naklejce.

Dla kazdego obrazu znajdz to oznaczenie i wskaz, ktora to pozycja z listy naszych profili.

Lista profili (pole "produkt" musi byc DOKLADNIE jedna z tych pozycji albo "?"):
@@LISTA@@

Zasady:
- Obejrzyj cale zdjecie — napis bywa maly, w rogu, obrocony, do gory nogami albo pod katem.
- "napis": przepisz doslownie wydrukowane oznaczenie profilu, nawet jesli nie ma go
  na liscie. Gdy nic nie widac — "?".
- "produkt": pozycja z listy odpowiadajaca napisowi. Pozycje rozniace sie dopiskiem
  ("pak", "F.", "K.", "KF.", "Przemial", kropka na koncu) to ROZNE produkty — wybierz te,
  ktorej dopisek faktycznie widac. Gdy nie da sie rozstrzygnac miedzy wariantami, wybierz
  najbardziej prawdopodobny i ustaw pewnosc "niska".
- Uwazaj na podobne znaki: 0/O/D, 1/I, 5/S, 8/B, 02/08, 12/17. Jesli nie masz pewnosci
  co do znaku — pewnosc "niska".
- Jesli na zdjeciu sa ROZNE profile: w "produkt" najlepiej widoczny, pozostale (z listy)
  w "inne". Gdy profil jest jeden — "inne" puste.
- Nie zgaduj. Brak czytelnego oznaczenia -> produkt "?", pewnosc "niska".

Obrazy sa ponumerowane. Dla kazdego zwroc jeden wpis z jego numerem."""

WSTEP_ALU1 = 'Odczytaj oznaczenia profili ze zdjec.'
WSTEP_ALU2 = ('To sa zdjecia w WYZSZEJ rozdzielczosci — przy pierwszym podejsciu nie udalo sie '
              'pewnie odczytac oznaczenia. Obejrzyj je bardzo dokladnie, fragment po fragmencie.')


def schemat_alu(profile):
    return {
        'type': 'OBJECT',
        'properties': {'wyniki': {'type': 'ARRAY', 'items': {
            'type': 'OBJECT',
            'properties': {
                'nr':      {'type': 'INTEGER'},
                'produkt': {'type': 'STRING', 'enum': profile + ['?']},
                'napis':   {'type': 'STRING'},
                'pewnosc': {'type': 'STRING', 'enum': ['wysoka', 'niska']},
                'inne':    {'type': 'ARRAY', 'items': {'type': 'STRING'}},
            },
            'required': ['nr', 'produkt', 'napis', 'pewnosc', 'inne'],
        }}},
        'required': ['wyniki'],
    }


def gemini(prompt, jpgi, model, schemat):
    """Jedno zapytanie z obrazami; zwraca liste 'wyniki' z odpowiedzi JSON."""
    czesci = [{'text': prompt}]
    for i, b in enumerate(jpgi, 1):
        czesci.append({'text': f'--- obraz {i} ---'})
        czesci.append({'inline_data': {'mime_type': 'image/jpeg',
                                       'data': base64.b64encode(b).decode()}})
    d = http_json(f'{BAZA}/models/{model}:generateContent?key={klucz()}', {
        'contents': [{'parts': czesci}],
        'generationConfig': {'responseMimeType': 'application/json',
                             'responseSchema': schemat, 'temperature': 0},
    })
    tekst = ''.join(c.get('text', '') for c in d['candidates'][0]['content']['parts']
                    if not c.get('thought'))
    return json.loads(tekst).get('wyniki', [])


def czytaj_alu(jpgi, model, profile, wstep):
    """Zwraca liste slownikow {produkt, napis, pewnosc, inne} w kolejnosci obrazow."""
    prompt = (PROMPT_ALU.replace('@@WSTEP@@', wstep)
                        .replace('@@LISTA@@', '\n'.join('- ' + p for p in profile)))
    wyniki = gemini(prompt, jpgi, model, schemat_alu(profile))
    po_normie = {}
    for p in profile:
        po_normie.setdefault(norm_profilu(p), p)

    out = [{'produkt': '', 'napis': '', 'status': 'brak', 'inne': []} for _ in jpgi]
    for w in wyniki:
        i = int(w.get('nr', 0)) - 1
        if not 0 <= i < len(out):
            continue
        napis = (w.get('napis') or '').strip()
        napis = '' if napis == '?' else napis
        # enum w schemacie pilnuje listy, ale starsze modele potrafia go zignorowac —
        # dlatego i tak sprawdzamy po normie, a w drugiej kolejnosci po samym napisie
        prod = po_normie.get(norm_profilu(w.get('produkt'))) or \
               (po_normie.get(norm_profilu(napis)) if napis else None)
        inne = [po_normie.get(norm_profilu(x)) for x in (w.get('inne') or [])]
        inne = [x for x in dict.fromkeys(inne) if x and x != prod][:3]
        if prod:
            status = 'ok' if w.get('pewnosc') == 'wysoka' else 'niepewne'
        elif napis:
            status = 'spoza'              # cos wydrukowane, ale nie z naszej listy
        else:
            status = 'brak'
        out[i] = {'produkt': prod or '', 'napis': napis, 'status': status, 'inne': inne}
    return out


class Tempo:
    """Pilnuje odstepu miedzy zapytaniami (limit na minute)."""
    def __init__(self, rpm):
        self.odstep, self.ostatnie = 60.0 / max(rpm, 1), 0.0

    def czekaj(self, opis):
        while not STAN['stop']:
            c = self.odstep - (time.time() - self.ostatnie)
            if c <= 0:
                return
            faza(f'pauza na limit — {int(c)+1} s do {opis}')
            time.sleep(min(0.5, c))


def wyslij(tempo, opis, fn):
    """Zapytanie z pauza na limit i ponawianiem. Zwraca (wynik albo None, limit_wyczerpany)."""
    proba = 0
    while proba < 4 and not STAN['stop']:
        tempo.czekaj(opis)
        if STAN['stop']:
            break
        try:
            tempo.ostatnie = time.time()
            faza(f'{opis} — wysłane, czekam na odpowiedź…')
            w = fn()
            zuzycie(1)
            with BLOKADA:
                STAN['req'] += 1
            return w, False
        except urllib.error.HTTPError as e:
            tresc = e.read().decode('utf-8', 'replace')[:200]
            if e.code not in (429, 500, 503):
                log(f'{opis}: HTTP {e.code} {tresc} — kończę odczyt, nazywam to, co gotowe')
                return None, True
            proba += 1
            if e.code == 429 and proba >= 3:
                log('LIMIT WYCZERPANY — zapisuję to, co już odczytane, reszta czeka na następny raz')
                return None, True
            pauza = min(60, 5 * 2 ** proba)
            log(f'{opis}: HTTP {e.code}, czekam {pauza} s (próba {proba}/3)')
        except Exception as e:           # siec, pusta odpowiedz, zly JSON — probujemy jeszcze raz
            proba += 1
            pauza = 5 * proba
            log(f'{opis}: {type(e).__name__}: {str(e)[:120]} — ponawiam za {pauza} s')
        for _ in range(pauza * 2):
            if STAN['stop']:
                break
            time.sleep(0.5)
    return None, False


def stan_alu_zapisz(folder, mapa):
    os.makedirs(CFG_DIR, exist_ok=True)
    json.dump({'folder': folder, 'mapa': mapa}, open(STAN_ALU_FILE, 'w', encoding='utf-8'),
              ensure_ascii=False, indent=1)


def stan_alu_wczytaj(folder):
    try:
        d = json.load(open(STAN_ALU_FILE, encoding='utf-8'))
        return d['mapa'] if d.get('folder') == folder else {}
    except Exception:
        return {}


def nazwa_pliku(s):
    """Nazwa produktu -> bezpieczna nazwa pliku (ukosnik jest separatorem katalogow)."""
    s = s.replace('/', '-').replace('\\', '-')
    s = re.sub(r'[<>:"|?*\x00-\x1f]', '', s)
    s = re.sub(r'\s+', ' ', s).strip(' .')      # Windows ucina kropki i spacje na koncu
    return s[:150] or 'bez nazwy'


def miejsce_na(cel_dir, trzon, roz, zrodlo, zajete):
    """Pierwsza wolna nazwa 'trzon.jpg', 'trzon (2).jpg'... Jesli pod ktoras z nich lezy
    juz ten sam plik (ponowne uruchomienie), zwraca ja z flaga 'juz jest'.
    'zajete' to nazwy nadane w tym przebiegu — tych nie bierzemy drugi raz."""
    rozmiar = os.path.getsize(zrodlo)
    i = 0
    while True:
        i += 1
        n = f'{trzon}{roz}' if i == 1 else f'{trzon} ({i}){roz}'
        cel = os.path.join(cel_dir, n)
        if cel in zajete:
            continue
        if not os.path.exists(cel):
            return cel, False
        if os.path.getsize(cel) == rozmiar:
            return cel, True


def nazwa_wyniku(w, stary):
    baza, roz = os.path.splitext(os.path.basename(stary))
    st = w.get('status')
    if st in ('ok', 'niepewne'):
        trzon = ' + '.join(nazwa_pliku(x) for x in [w['produkt']] + w.get('inne', []))
        return trzon + (' (niepewne)' if st == 'niepewne' else ''), roz
    if st == 'spoza':
        return f'{nazwa_pliku(w["napis"])} (spoza listy)', roz
    return f'NIEROZPOZNANE {baza}', roz


def raport_alu(folder, cel_dir, wyniki, produkty):
    """Raport tekstowy i CSV (dla Excela: srednik + UTF-8 z BOM) w folderze wynikowym."""
    ile = len(wyniki)
    licz = {k: sum(1 for w in wyniki if w['status'] == k)
            for k in ('ok', 'niepewne', 'spoza', 'brak', 'blad')}
    etyk = {'ok': 'ROZPOZNANE', 'niepewne': 'NIEPEWNE', 'spoza': 'SPOZA LISTY',
            'brak': 'NIEROZPOZNANE', 'blad': 'BŁĄD PLIKU'}
    L = [f'{NAZWA} — raport ALUPROF',
         f'Folder:  {folder}',
         f'Data:    {time.strftime("%Y-%m-%d %H:%M")}',
         '',
         f'Zdjęć: {ile}  ·  rozpoznane: {licz["ok"]}  ·  niepewne: {licz["niepewne"]}  ·  '
         f'spoza listy: {licz["spoza"]}  ·  nierozpoznane: {licz["brak"] + licz["blad"]}',
         '', 'PRODUKTY', '']
    szer = max([len(p['produkt']) for p in produkty] + [10])
    for p in produkty:
        L.append(f'  {p["produkt"].ljust(szer)}  x {p["ile"]}'
                 + (f'   (w tym niepewne: {p["niepewne"]})' if p['niepewne'] else ''))
    if not produkty:
        L.append('  (brak)')
    L += ['', 'ZDJĘCIA', '']
    for w in wyniki:
        L.append(f'  [{etyk[w["status"]]}]  {w["stary"]}  ->  {w["nowy"]}'
                 + (f'   (napis: {w["napis"]})' if w.get('napis') else ''))
    with open(os.path.join(cel_dir, f'Raport {NAZWA}.txt'), 'w', encoding='utf-8') as f:
        f.write('\n'.join(L) + '\n')

    def pole(x):
        x = str(x)
        return '"' + x.replace('"', '""') + '"' if any(c in x for c in ';"\n') else x
    with open(os.path.join(cel_dir, f'Raport {NAZWA}.csv'), 'w', encoding='utf-8-sig') as f:
        f.write('Produkt;Ilość;W tym niepewne\n')
        for p in produkty:
            f.write(f'{pole(p["produkt"])};{p["ile"]};{p["niepewne"]}\n')
        f.write('\nZdjęcie;Nowa nazwa;Status;Produkt;Wydrukowany napis\n')
        for w in wyniki:
            f.write(';'.join(pole(x) for x in (w['stary'], w['nowy'], etyk[w['status']],
                                               w.get('kod', ''), w.get('napis', ''))) + '\n')


def przebieg_alu(folder, model, na_req, rpm):
    """Watek roboczy ALUPROF: zbieranie -> kompresja -> Gemini -> kopie z nazwami -> raport."""
    tmp = tempfile.mkdtemp(prefix='makroskan-')
    try:
        folder = os.path.abspath(folder)
        profile = lista_profili()
        with BLOKADA:
            STAN.update(folder=folder, etap='szukam zdjęć', ile=0, zrobione=0)

        # ── 1. zbieranie, razem z rozpakowaniem archiwow ─────────────────
        wpisy, bledy = zbierz_zdjecia(folder, os.path.join(tmp, 'archiwa'))
        for b in bledy:
            log(b)
        log(f'znaleziono {len(wpisy)} zdjęć, lista profili: {len(profile)} pozycji')
        if not wpisy:
            log('w folderze nie ma zdjęć')
            return

        mapa = {k: v for k, v in stan_alu_wczytaj(folder).items()
                if any(k == x for x, _ in wpisy)}
        if mapa:
            log(f'wznawiam — {len(mapa)} zdjęć było już odczytanych')
        do_zrobienia = [(k, p) for k, p in wpisy if k not in mapa]

        # ── 2. kompresja: lzejsze pliki do wysylki, napis ma zostac czytelny ─
        with BLOKADA:
            STAN.update(etap='kompresja', ile=len(do_zrobienia), zrobione=0)
        male, przed, po = [], 0, 0
        os.makedirs(os.path.join(tmp, 'male'), exist_ok=True)
        for i, (k, p) in enumerate(do_zrobienia, 1):
            if STAN['stop']:
                break
            faza(f'kompresuję {i}/{len(do_zrobienia)}: {k}')
            im = wczytaj_obraz(p)
            jpg = obraz_do_jpg(im, ALU_PX, ALU_JAKOSC) if im is not None else None
            if not jpg:
                mapa[k] = {'produkt': '', 'napis': '', 'status': 'blad', 'inne': []}
                log(f'nie da się otworzyć: {k}')
            else:
                sc = os.path.join(tmp, 'male', f'{i}.jpg')
                with open(sc, 'wb') as f:
                    f.write(jpg)
                przed += os.path.getsize(p)
                po += len(jpg)
                male.append((k, p, sc))
            with BLOKADA:
                STAN['zrobione'] = i
        if male:
            log(f'skompresowano {len(male)} zdjęć: {przed/1048576:.0f} MB → {po/1048576:.1f} MB')

        # ── 3. odczyt przez Gemini ───────────────────────────────────────
        tempo, brak_limitu = Tempo(rpm), False
        paczki = [male[i:i+na_req] for i in range(0, len(male), na_req)]
        with BLOKADA:
            STAN.update(etap='odczyt', ile=len(paczki), zrobione=0)
        if paczki:
            log(f'{len(paczki)} zapytań do {model}, po {na_req} zdjęć')
        for nr, paczka in enumerate(paczki, 1):
            if STAN['stop']:
                break
            jpgi = [open(sc, 'rb').read() for _, _, sc in paczka]
            wyn, brak_limitu = wyslij(tempo, f'paczka {nr}/{len(paczki)}',
                                      lambda: czytaj_alu(jpgi, model, profile, WSTEP_ALU1))
            if wyn is None:
                break
            for (k, _, _), w in zip(paczka, wyn):
                mapa[k] = w
            log(f'paczka {nr}/{len(paczki)}: ' + ', '.join(
                (w['produkt'] or w['napis'] or '—') + ('?' if w['status'] != 'ok' else '')
                for w in wyn))
            stan_alu_zapisz(folder, mapa)
            with BLOKADA:
                STAN['zrobione'] = nr

        # ── 3b. drugie podejscie: nierozpoznane i niepewne w wiekszej rozdzielczosci ─
        slabe = [(k, p) for k, p in wpisy
                 if mapa.get(k, {}).get('status') in ('brak', 'niepewne', 'spoza')]
        if slabe and not STAN['stop'] and not brak_limitu:
            czesci = [slabe[i:i+ALU_NA_REQ2] for i in range(0, len(slabe), ALU_NA_REQ2)]
            with BLOKADA:
                STAN.update(etap='drugie podejscie', ile=len(czesci), zrobione=0)
            log(f'drugie podejście: {len(slabe)} zdjęć w wyższej rozdzielczości')
            for nr, czesc in enumerate(czesci, 1):
                if STAN['stop']:
                    break
                jpgi = []
                for _, p in czesc:
                    im = wczytaj_obraz(p)
                    jpgi.append(obraz_do_jpg(im, ALU_PX2, ALU_JAKOSC2) if im is not None else None)
                if not all(jpgi):
                    continue
                wyn, brak_limitu = wyslij(tempo, f'drugie podejście {nr}/{len(czesci)}',
                                          lambda: czytaj_alu(jpgi, model, profile, WSTEP_ALU2))
                if wyn is None:
                    break
                ranga = {'ok': 3, 'niepewne': 2, 'spoza': 1, 'brak': 0}
                for (k, _), w in zip(czesc, wyn):
                    if ranga.get(w['status'], 0) > ranga.get(mapa[k]['status'], 0):
                        log(f'  poprawione: {k} -> {w["produkt"] or w["napis"]}')
                        mapa[k] = w
                stan_alu_zapisz(folder, mapa)
                with BLOKADA:
                    STAN['zrobione'] = nr

        # ── 4. kopie oryginalow z nazwami produktow ──────────────────────
        cel_dir = os.path.join(folder, os.path.basename(folder.rstrip('/\\')) + DOPISEK)
        os.makedirs(cel_dir, exist_ok=True)
        with BLOKADA:
            STAN.update(etap='zapisywanie', ile=len(wpisy), zrobione=0, cel=cel_dir)
        wyniki, zajete = [], set()
        for nr, (k, p) in enumerate(wpisy, 1):
            with BLOKADA:
                STAN['zrobione'] = nr
            w = mapa.get(k)
            if w is None:                   # nieodczytane (limit/przerwanie) — nastepnym razem
                continue
            if nr % 5 == 0 or nr == len(wpisy):
                faza(f'zapisuję {nr}/{len(wpisy)}')
            trzon, roz = nazwa_wyniku(w, k)
            cel, juz = miejsce_na(cel_dir, trzon, roz, p, zajete)
            zajete.add(cel)
            try:
                if not juz:
                    shutil.copy2(p, cel)    # kopia 1:1, oryginal nietkniety
                wyniki.append({'stary': k, 'nowy': os.path.basename(cel), 'status': w['status'],
                               'kod': ' + '.join([w['produkt']] + w.get('inne', []))
                                      if w['produkt'] else '',
                               'napis': w.get('napis', '')})
            except Exception as e:
                log(f'nie udało się {k}: {e}')

        # ── 5. zestawienie produktow i raport ────────────────────────────
        zest = {}
        for w in (mapa[k] for k, _ in wpisy if k in mapa):
            if w['status'] not in ('ok', 'niepewne'):
                continue
            for prod in [w['produkt']] + w.get('inne', []):
                z = zest.setdefault(prod, {'produkt': prod, 'ile': 0, 'niepewne': 0})
                z['ile'] += 1
                z['niepewne'] += w['status'] == 'niepewne'
        kolej = {p: i for i, p in enumerate(profile)}
        produkty = sorted(zest.values(), key=lambda z: (-z['ile'], kolej.get(z['produkt'], 1e9)))
        try:
            raport_alu(folder, cel_dir, wyniki, produkty)
        except Exception as e:
            log(f'raport nie zapisany: {e}')

        with BLOKADA:
            STAN.update(wyniki=wyniki, produkty=produkty)
        ok = sum(1 for w in wyniki if w['status'] == 'ok')
        if brak_limitu or STAN['stop']:
            zostalo = sum(1 for k, _ in wpisy if k not in mapa)
            if zostalo:
                log(f'PRZERWANE — {zostalo} zdjęć czeka. Uruchom ponownie na tym samym '
                    'folderze, program dokończy od tego miejsca.')
        log(f'GOTOWE — rozpoznane {ok}/{len(wpisy)}, '
            f'różnych produktów {len(produkty)}, zapytań {STAN["req"]}')
        log(f'pliki i raport w: {cel_dir}')

    except Exception as e:
        log(f'BŁĄD: {type(e).__name__}: {e}')
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
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


def wybierz_folder():
    """Wybor jednego folderu (tryb ALUPROF)."""
    if OKNO is not None:
        try:
            import webview
            typ = getattr(getattr(webview, 'FileDialog', None), 'FOLDER', 20)
            w = OKNO.create_file_dialog(typ)
            return (list(w or []) or [''])[0]
        except Exception:
            pass
    if sys.platform == 'darwin':
        return osascript('POSIX path of (choose folder with prompt "Wskaż folder ze zdjęciami")')
    from tkinter import Tk, filedialog
    t = Tk(); t.withdraw()
    w = filedialog.askdirectory(title='Wskaż folder ze zdjęciami')
    t.destroy()
    return w or ''


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
        if path == '/api/wybierz_folder':
            f = wybierz_folder().rstrip('/\\')
            if not f or not os.path.isdir(f):
                return {'folder': ''}
            zdj, arch = policz_folder(f)
            return {'folder': f, 'nazwa': os.path.basename(f), 'zdjecia': zdj, 'archiwa': arch}
        if path == '/api/tryb':
            if STAN['pracuje']:
                return {'error': 'najpierw przerwij pracę'}
            t = 'alu' if r.get('tryb') == 'alu' else 'depo'
            cfg_zapisz({'tryb': t})
            return {'tryb': t}
        if path == '/api/ustawienia':
            U = cfg_wczytaj()
            if r.get('zapisz'):
                zm = {k: r[k] for k in ('klucz', 'model', 'na_req', 'rpm', 'na_req_alu')
                      if k in r}
                if 'profile' in r:
                    # lista identyczna z wbudowana zapisuje sie jako pusta — wtedy
                    # przyszle poprawki listy w programie dojda same
                    nowa = [x.strip() for x in str(r['profile']).splitlines() if x.strip()]
                    domyslna = [x.strip() for x in PROFILE_ALUPROF.splitlines() if x.strip()]
                    zm['profile'] = '' if nowa == domyslna else '\n'.join(nowa)
                U = cfg_zapisz(zm)
            U = dict(U)
            U['profile_tekst'] = '\n'.join(lista_profili())
            U['profile_ile'] = len(lista_profili())
            U['profile_wlasne'] = bool((U.get('profile') or '').strip())
            U['ma_klucz'] = bool(U.get('klucz', '').strip())
            U['klucz'] = U.get('klucz', '')
            U['wersja'] = WERSJA
            return U
        if path == '/api/aktualizacja':
            return sprawdz_aktualizacje()
        if path == '/api/zaktualizuj':
            if AKT['trwa']:
                return {'ok': True, 'juz': True}
            akt_stan(trwa=True, etap='sprawdzam wydanie', procent=0, mb=0,
                     mb_calosc=0, blad='', gotowe=False, wersja='')
            threading.Thread(target=zaktualizuj_w_tle, daemon=True).start()
            return {'ok': True}
        if path == '/api/stan_akt':
            with BLOKADA:
                return dict(AKT)
        if path == '/api/reset':
            if STAN['pracuje']:
                return {'error': 'najpierw przerwij pracę'}
            with BLOKADA:
                STAN.update(log=[], wyniki=[], produkty=[], req=0, zrobione=0, ile=0,
                            etap='gotowy', gotowe=False, folder='', cel='')
            for f in (STAN_FILE, STAN_ALU_FILE):   # kasujemy tez zapisany postep
                try:
                    os.remove(f)
                except OSError:
                    pass
            return {'ok': True}
        if path == '/api/limity':
            return limity_modelu(r.get('model') or cfg_wczytaj()['model'])
        if path == '/api/zuzycie':
            return zuzycie()
        if path == '/api/modele':
            return {'modele': lista_modeli()}
        if path == '/api/start' and r.get('tryb') == 'alu':
            if STAN['pracuje']:
                return {'error': 'już pracuje'}
            folder = r.get('folder') or ''
            if not os.path.isdir(folder):
                return {'error': 'wskaż folder'}
            U = cfg_wczytaj()
            if not U.get('klucz', '').strip():
                return {'error': 'brak klucza API — wpisz go w Ustawieniach'}
            with BLOKADA:
                STAN.update(pracuje=True, stop=False, gotowe=False, log=[], wyniki=[],
                            produkty=[], req=0, zrobione=0, ile=0, etap='start', cel='',
                            faza='', start=time.time(), trwa=0, tryb='alu')
            threading.Thread(target=przebieg_alu, daemon=True, args=(
                folder, r.get('model') or U['model'],
                max(1, min(30, int(r.get('na_req_alu') or U['na_req_alu']))),
                float(r.get('rpm') or U['rpm']))).start()
            return {'ok': True}
        if path == '/api/start':
            if STAN['pracuje']:
                return {'error': 'już pracuje'}
            pliki = r.get('pliki') or []
            if not pliki:
                return {'error': 'brak plików'}
            U = cfg_wczytaj()
            if not U.get('klucz', '').strip():
                return {'error': 'brak klucza API — wpisz go w Ustawieniach'}
            with BLOKADA:
                STAN.update(pracuje=True, stop=False, gotowe=False, log=[], wyniki=[],
                            produkty=[], req=0, zrobione=0, ile=len(pliki), etap='start',
                            cel='', faza='', start=time.time(), trwa=0, tryb='depo')
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
<title>MakroSkan</title><style>
:root{
  --bg1:#cfe4ff; --bg2:#dceaff; --bg3:#e8f4ff; --page:#f4f8fd;
  --card:rgba(255,255,255,.66); --stroke:rgba(255,255,255,.85);
  --txt:#17222e; --dim:#68788a; --line:rgba(30,60,95,.11);
  --acc:#f07316; --ok:#3f9d4a; --err:#d8452e; --warn:#c77400;
  --field:rgba(255,255,255,.7); --track:rgba(30,60,95,.09);
  /* kolor wiodacy: DEPO = pomarancz, ALUPROF (body.alu) = blekit */
  --accrgb:240,115,22; --g1:255,138,32; --g2:240,100,20; --g3:214,64,32; --glow:230,110,25;
  --t1:#f07316; --t2:#e0521a; --t3:#b83a12;
}
body.alu{--acc:#1478dc; --accrgb:20,120,220; --g1:64,170,255; --g2:26,128,236; --g3:18,88,200;
  --glow:25,120,230; --t1:#2a9bff; --t2:#1478dc; --t3:#0b4fa8}
@media (prefers-color-scheme:dark){:root{
  --bg1:#1a3050; --bg2:#25405f; --bg3:#122539; --page:#0c1219;
  --card:rgba(32,44,58,.6); --stroke:rgba(160,200,240,.14);
  --txt:#eaf1f8; --dim:#93a6ba; --line:rgba(160,200,240,.13);
  --acc:#ff9330; --ok:#4fd06a; --err:#ff7a5e; --warn:#ffc247;
  --field:rgba(0,0,0,.22); --track:rgba(160,200,240,.13);
  --t1:#ffa24a; --t2:#ff8330; --t3:#e0621c;
}
body.alu{--acc:#4db2ff; --accrgb:77,178,255; --t1:#7cc6ff; --t2:#4db2ff; --t3:#2a8fe8}}
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
  flex-direction:column;
  background:var(--card); border:1px solid var(--stroke); border-radius:28px;
  backdrop-filter:blur(40px) saturate(180%); -webkit-backdrop-filter:blur(40px) saturate(180%);
  box-shadow:0 24px 60px rgba(0,0,0,.16), inset 0 1px 0 rgba(255,255,255,.35); padding:24px 26px}
/* dwie kolumny: po lewej wybor i sterowanie, po prawej postep, wyniki i log */
  .kolumny{display:grid; grid-template-columns:440px minmax(0,1fr); gap:24px;
  flex:1 1 auto; min-height:0; margin-top:16px}
.lewa,.prawa{min-height:0; display:flex; flex-direction:column; overflow:visible}
.lewa{overflow:visible}
.lewa .pliki{flex:1 1 auto; max-height:none; min-height:60px; overflow-y:auto;
  margin-right:-5px; padding-right:5px}
.dol{flex:none; margin-top:auto; padding-top:12px}
.dol button{margin-top:8px}
.dol .err:empty{display:none}
.podpis{flex:none; display:flex; align-items:center; justify-content:center; gap:10px;
  margin-top:16px; padding-top:12px; border-top:1px solid var(--line);
  font-size:11.5px; line-height:1.35; color:var(--dim)}
.podpis .mak{width:38px; height:auto; flex:none; color:#CB2228}
.podpis .autor{height:15px; width:auto; flex:none; color:var(--txt); opacity:.85;
  margin:0 1px; transform:translateY(1px)}
.podpis sup{font-size:.7em; vertical-align:super}
.prawa{gap:0}
.stopka{flex:none; margin-top:10px}
.stopka button{margin-top:0; padding:15px; font-size:17px}
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
  background:rgba(var(--accrgb),.10)}
.odsw.obr:hover:not(:disabled){transform:rotate(90deg)}
/* znak zebatki rysuje sie mniejszy niz strzalka — wyrownujemy optycznie */
.odsw.zeb{font-size:30px; padding-bottom:2px}
h1{position:relative}
h1 img{width:42px; height:42px; border-radius:10px; flex:none;
  box-shadow:0 3px 12px rgba(120,100,160,.32)}
h1 .g{background:linear-gradient(105deg,var(--t1),var(--t2) 48%,var(--t3));
  -webkit-background-clip:text; background-clip:text; color:transparent}
/* przelacznik DEPO / ALUPROF — na srodku naglowka */
.tryby{position:absolute; left:50%; top:50%; transform:translate(-50%,-50%);
  display:flex; gap:4px; padding:4px; border-radius:16px; background:var(--field);
  border:1px solid var(--line); backdrop-filter:blur(10px)}
.tryb{min-width:132px; padding:10px 20px; border-radius:12px; text-align:center;
  font-size:15.5px; font-weight:650; letter-spacing:.04em; color:var(--dim);
  cursor:pointer; user-select:none; transition:color .2s, background .25s, box-shadow .25s}
.tryb:hover{color:var(--txt)}
.tryb.on{color:#fff; box-shadow:0 6px 18px rgba(var(--glow),.35), inset 0 1px 0 rgba(255,255,255,.55)}
.tryb[data-t="depo"].on{background:linear-gradient(180deg,rgba(255,255,255,.3),rgba(255,255,255,0) 60%),
  linear-gradient(118deg,#ff8a20,#f06414 55%,#d64020)}
.tryb[data-t="alu"].on{background:linear-gradient(180deg,rgba(255,255,255,.3),rgba(255,255,255,0) 60%),
  linear-gradient(118deg,#40aaff,#1a80ec 55%,#1258c8)}
.tryby.zablok .tryb{cursor:default; opacity:.6}
@media (max-width:1100px){.tryb{min-width:96px; padding:9px 12px}}
.sub{display:none}
.drop{border:1.5px dashed var(--line); border-radius:18px; padding:22px 18px; text-align:center;
  cursor:pointer; transition:.18s; background:var(--field); position:relative; overflow:hidden}
.drop:hover{border-color:var(--acc); background:rgba(var(--accrgb),.10)}
.drop .big{font-size:18.5px; font-weight:590; line-height:1.35; overflow-wrap:anywhere}
button{position:relative; width:100%; margin-top:14px; padding:15px; border-radius:18px;
  border:1px solid rgba(255,255,255,.38); color:#fff; font:inherit; font-size:17px; font-weight:590;
  cursor:pointer; overflow:hidden; isolation:isolate;
  background:linear-gradient(180deg,rgba(255,255,255,.34),rgba(255,255,255,.08) 46%,rgba(255,255,255,0) 62%),
             linear-gradient(118deg,rgba(var(--g1),.97),rgba(var(--g2),.93) 55%,rgba(var(--g3),.88));
  backdrop-filter:blur(16px) saturate(200%);
  box-shadow:0 10px 28px rgba(var(--glow),.34), inset 0 1px 0 rgba(255,255,255,.6);
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
.aktTxt{font-size:14.5px; line-height:1.55; color:var(--dim); margin-bottom:18px;
  white-space:pre-wrap; word-break:break-word}
.aktInfo{display:flex; justify-content:space-between; font-size:12px; color:var(--dim);
  margin-top:7px; margin-bottom:16px; font-variant-numeric:tabular-nums}
.aktTxt b{color:var(--txt)}
button.zielony{background:
  linear-gradient(180deg,rgba(255,255,255,.36),rgba(255,255,255,.08) 46%,rgba(255,255,255,0) 62%),
  linear-gradient(112deg,#54dd8e,#26c268 58%,#12a457);
  box-shadow:0 10px 28px rgba(34,190,100,.4), inset 0 1px 0 rgba(255,255,255,.7)}
.modalTyt{font-size:21px; font-weight:590; letter-spacing:-.02em; margin-bottom:14px;
  display:flex; align-items:baseline; gap:9px}
.wers{font-size:12px; color:var(--dim); font-weight:400}
.powiad{display:none; font-size:13.5px; line-height:1.5; padding:11px 13px; border-radius:13px;
  background:rgba(var(--accrgb),.13); border:1px solid rgba(var(--accrgb),.35);
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
.row.wiele{border-color:rgba(var(--accrgb),.42)}
.row .ile{margin-left:auto; font-weight:650; font-variant-numeric:tabular-nums; color:var(--acc)}
.row.prod .kod{overflow:hidden; text-overflow:ellipsis; white-space:nowrap}
.row.bad{border-color:rgba(216,69,46,.45); background:rgba(216,69,46,.10)}
.badge.bad{background:rgba(216,69,46,.18); color:var(--err)}
.podsum{display:flex; flex-wrap:wrap; gap:6px; margin:2px 0 10px}
.podtyt{font-size:11px; font-weight:590; text-transform:uppercase; letter-spacing:.04em;
  color:var(--dim); margin:12px 0 6px}
.pole textarea{width:100%; min-height:150px; border:0; background:transparent; color:var(--txt);
  font:12.5px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace; outline:none; resize:vertical}
.pole label a{float:right; text-transform:none; letter-spacing:0; font-weight:400;
  color:var(--acc); cursor:pointer}
.tylkoAlu{display:none}
body.alu .tylkoAlu{display:block}
body.alu .tylkoDepo{display:none}
.badge.wiele{background:rgba(var(--accrgb),.2); color:var(--acc)}
.badge{font-size:10.5px; font-weight:590; letter-spacing:.03em; text-transform:uppercase;
  padding:3px 7px; border-radius:7px; background:var(--track); color:var(--dim)}
.badge.ok{background:rgba(63,157,74,.18); color:var(--ok)}
.badge.warn{background:rgba(255,170,40,.22); color:var(--warn)}
.err{color:var(--err); font-size:14px; margin-top:10px}
</style></head><body>
<div class="blob b1"></div><div class="blob b2"></div><div class="blob b3"></div>
<div class="card">
  <h1><img src="data:image/png;base64,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" alt=""><span>Makro<span class="g">Skan</span></span>
    <div class="tryby" id="tryby">
      <div class="tryb on" data-t="depo" onclick="ustawTryb('depo', true)">DEPO</div>
      <div class="tryb" data-t="alu" onclick="ustawTryb('alu', true)">ALUPROF</div>
    </div>
    <button class="odsw zeb" style="margin-left:auto" onclick="ustOtworz()" title="Ustawienia">&#9881;</button>
    <button class="odsw obr" style="margin-left:10px" onclick="odswiez()"
            title="Odśwież">&#8635;</button></h1>

  <div class="kolumny">
    <!-- ── LEWA: wybor zdjec i sterowanie ─────────────────────────────── -->
    <div class="lewa">
      <div class="drop" id="drop" onclick="dodaj()">
        <div class="big" id="dropTxt">Wybierz zdjęcia</div>
      </div>

      <div class="info tylkoAlu" id="aluInfo">Program przeszuka folder razem z podfolderami
        i archiwami (.zip, .rar, .7z), skompresuje zdjęcia i rozpozna na nich
        wydrukowane oznaczenia profili. Kopie z nazwami produktów i raport trafią
        do nowego folderu „<i>nazwa folderu</i> MakroSkan”.</div>

      <div class="naglowek" id="naglowek" style="display:none">
        <span id="ilePlikow"></span><a onclick="wyczysc(event)">wyczyść listę</a>
      </div>
      <div class="pliki" id="pliki"></div>

      <div class="dol">
        <div class="err" id="err"></div>
        <button id="go" onclick="start()" disabled>Skanuj</button>
        <button id="stop" class="stop" onclick="stop()" style="display:none">Przerwij</button>
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
        <button class="ghost" id="pokaz" onclick="pokaz()" disabled>Pokaż wyniki w Finderze</button>
      </div>
    </div>
  </div>

  <div class="podpis">
    <svg class="mak" viewBox="0 0 170 51" role="img" aria-label="MAK">
      <path fill="currentColor" fill-rule="evenodd" d="M0.0,0.0 L0.0,50.92 L15.67,50.92 L15.92,49.58 L15.83,13.58 L16.42,12.92 L39.92,13.0 L42.25,13.25 L43.58,14.08 L45.0,15.67 L45.25,18.08 L45.25,48.33 L45.08,49.5 L45.42,50.92 L61.67,50.92 L61.92,49.75 L61.83,10.33 L60.67,5.75 L59.33,3.75 L57.58,2.08 L55.5,1.0 L53.42,0.75 L52.92,0.5 L52.67,0.0 Z M82.25,0.0 L82.33,1.08 L83.08,2.33 L83.17,3.42 L84.0,4.67 L84.17,5.83 L84.92,7.08 L85.17,8.42 L85.83,9.5 L86.08,10.75 L86.75,11.67 L87.0,13.25 L87.75,14.58 L87.83,15.42 L89.08,18.08 L89.08,18.58 L88.58,19.0 L75.67,18.92 L75.33,19.17 L74.25,21.75 L74.25,22.25 L73.42,24.67 L73.42,25.25 L72.75,26.75 L72.58,28.17 L71.83,29.67 L71.67,31.17 L71.0,32.42 L70.83,33.75 L70.17,35.0 L69.83,36.92 L69.08,38.33 L69.0,39.67 L68.25,40.92 L68.0,42.75 L67.5,43.67 L67.17,45.25 L66.5,46.75 L66.25,48.25 L65.58,49.58 L65.83,50.92 L117.92,50.92 L117.75,48.83 L117.0,47.33 L116.67,45.83 L116.08,45.0 L115.83,43.5 L115.08,42.25 L114.83,40.67 L114.0,39.33 L114.0,38.5 L113.33,37.33 L113.0,35.75 L112.33,34.42 L112.25,33.58 L111.75,32.58 L110.58,28.67 L109.92,27.58 L109.67,26.08 L108.92,24.75 L108.83,23.58 L108.0,22.33 L107.75,20.67 L107.17,19.83 L106.83,18.33 L106.17,17.17 L106.0,16.08 L105.25,14.5 L105.17,13.58 L104.42,12.17 L103.42,8.58 L102.83,7.5 L102.67,6.42 L101.92,5.0 L101.75,3.58 L100.92,2.42 L100.92,1.5 L100.33,0.0 Z M139.42,0.0 L139.33,0.58 L136.83,3.5 L133.25,7.17 L129.92,11.25 L126.0,15.33 L121.0,21.17 L120.75,21.75 L120.75,32.5 L121.25,33.42 L124.0,36.25 L128.25,41.17 L137.08,50.42 L137.17,50.92 L159.58,50.92 L159.5,50.25 L159.0,49.33 L157.58,48.08 L148.83,38.83 L135.67,25.58 L135.92,24.75 L138.92,21.67 L143.0,16.75 L146.17,13.5 L150.0,8.92 L157.08,1.25 L157.67,0.33 L157.67,0.0 Z M22.33,19.08 L21.92,19.58 L21.92,49.83 L22.17,50.92 L38.75,50.92 L39.0,49.42 L39.0,20.58 L38.83,19.42 L38.33,19.0 Z M90.17,24.0 L90.75,24.08 L91.08,24.5 L91.25,25.83 L92.83,29.0 L93.17,30.33 L93.92,31.42 L94.0,32.25 L94.83,33.58 L95.0,34.67 L95.67,35.75 L96.58,38.25 L97.25,39.25 L97.25,39.67 L96.75,40.08 L85.92,40.08 L85.33,39.67 L85.33,39.25 L85.83,38.42 L85.92,37.33 L86.67,36.0 L86.92,34.0 L87.67,32.5 L87.67,31.58 L89.08,27.33 L89.5,24.92 Z M163.83,0.0 L163.83,0.58 L163.0,1.08 L162.0,2.08 L161.75,2.92 L161.17,3.67 L161.17,4.58 L161.83,5.67 L162.0,6.58 L163.33,7.92 L164.42,8.08 L165.5,8.83 L166.17,8.83 L167.17,8.08 L168.25,7.75 L169.33,6.33 L169.92,6.25 L169.92,2.0 L169.42,1.92 L168.67,0.92 L168.17,0.75 L167.83,0.42 L167.83,0.0 Z M164.33,0.83 L166.75,0.83 L167.25,1.0 L167.75,1.75 L168.92,2.75 L169.08,3.17 L169.08,5.5 L168.92,5.92 L166.42,7.75 L165.17,7.58 L163.67,6.67 L162.0,4.58 L162.08,3.83 L162.67,3.25 L162.92,2.25 L163.75,1.67 L163.92,1.17 Z M164.5,2.25 L163.92,3.33 L164.08,5.75 L164.58,5.83 L165.5,4.92 L166.42,5.75 L166.83,5.67 L167.0,5.25 L166.75,4.25 L167.33,3.58 L167.17,3.08 L166.58,2.33 L166.17,2.17 Z"/>
    </svg>
    <span>Stworzono w Makro-Plast<sup>&reg;</sup> przez</span>
    <svg class="autor" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 5719.33 916" role="img" aria-label="makarewicz"><g transform="translate(-65 770) scale(1 -1)" fill="currentColor"><path d="M65 0V526H167L177 456H184Q201 480 223 498.5Q245 517 273.5 527.5Q302 538 336 538Q382 538 418 519Q454 500 474 456H481Q498 480 521 498.5Q544 517 574 527.5Q604 538 639 538Q686 538 722 521Q758 504 779 465Q800 426 800 361V0H678V336Q678 363 672 381.5Q666 400 655.5 410.5Q645 421 629.5 426Q614 431 596 431Q567 431 544 415Q521 399 507.5 371Q494 343 494 306V0H372V336Q372 363 366 381.5Q360 400 349.5 410.5Q339 421 323.5 426Q308 431 290 431Q261 431 237.5 415Q214 399 200.5 371Q187 343 187 306V0Z"/><path transform="translate(841 0)" d="M200 -12Q178 -12 149.5 -6.5Q121 -1 94.5 14Q68 29 51 58.5Q34 88 34 136Q34 190 58 225.5Q82 261 125.5 281.5Q169 302 229.5 310.5Q290 319 362 319V362Q362 385 355 403Q348 421 329.5 431.5Q311 442 274 442Q237 442 216 433Q195 424 187 411Q179 398 179 384V370H61Q60 375 60 380Q60 385 60 392Q60 437 87 470Q114 503 162 520.5Q210 538 273 538Q345 538 391.5 518Q438 498 461 461Q484 424 484 371V123Q484 104 495 96Q506 88 519 88H551V4Q541 0 522 -5.5Q503 -11 475 -11Q449 -11 428.5 -2.5Q408 6 394 22Q380 38 374 60H368Q351 39 327.5 22.5Q304 6 272.5 -3Q241 -12 200 -12ZM237 88Q267 88 290.5 97Q314 106 329.5 122Q345 138 353.5 161Q362 184 362 211V235Q307 235 260.5 228Q214 221 186.5 202Q159 183 159 148Q159 130 167.5 116.5Q176 103 193.5 95.5Q211 88 237 88Z"/><path transform="translate(1377 0)" d="M65 0V723H187V302L376 526H519L343 322L529 0H389L265 233L187 158V0Z"/><path transform="translate(1900 0)" d="M200 -12Q178 -12 149.5 -6.5Q121 -1 94.5 14Q68 29 51 58.5Q34 88 34 136Q34 190 58 225.5Q82 261 125.5 281.5Q169 302 229.5 310.5Q290 319 362 319V362Q362 385 355 403Q348 421 329.5 431.5Q311 442 274 442Q237 442 216 433Q195 424 187 411Q179 398 179 384V370H61Q60 375 60 380Q60 385 60 392Q60 437 87 470Q114 503 162 520.5Q210 538 273 538Q345 538 391.5 518Q438 498 461 461Q484 424 484 371V123Q484 104 495 96Q506 88 519 88H551V4Q541 0 522 -5.5Q503 -11 475 -11Q449 -11 428.5 -2.5Q408 6 394 22Q380 38 374 60H368Q351 39 327.5 22.5Q304 6 272.5 -3Q241 -12 200 -12ZM237 88Q267 88 290.5 97Q314 106 329.5 122Q345 138 353.5 161Q362 184 362 211V235Q307 235 260.5 228Q214 221 186.5 202Q159 183 159 148Q159 130 167.5 116.5Q176 103 193.5 95.5Q211 88 237 88Z"/><path transform="translate(2436 0)" d="M65 0V526H167L177 443H184Q194 468 208.5 489.5Q223 511 246 524.5Q269 538 302 538Q318 538 331.5 535Q345 532 352 529V414H315Q284 414 260 405.5Q236 397 219.5 379Q203 361 195 334Q187 307 187 271V0Z"/><path transform="translate(2778 0)" d="M291 -12Q207 -12 151 17.5Q95 47 67 108Q39 169 39 263Q39 358 67 418.5Q95 479 151 508.5Q207 538 291 538Q367 538 418.5 509.5Q470 481 496 422Q522 363 522 269V233H164Q166 184 179 150.5Q192 117 219.5 100.5Q247 84 292 84Q315 84 335 90Q355 96 370 108.5Q385 121 393.5 140Q402 159 402 184H522Q522 134 504.5 97Q487 60 455.5 36Q424 12 382 0Q340 -12 291 -12ZM166 319H395Q395 352 387.5 375Q380 398 366.5 413Q353 428 334 434.5Q315 441 291 441Q252 441 225.5 428Q199 415 185 388Q171 361 166 319Z"/><path transform="translate(3319 0)" d="M160 0 3 526H129L202 240Q208 218 212 196.5Q216 175 219 160Q222 145 223 142H229Q233 160 237 181Q241 202 244.5 219.5Q248 237 249 243L317 526H448L519 242Q523 228 526.5 209.5Q530 191 534 173Q538 155 540 142H546Q548 154 551.5 171Q555 188 559 206Q563 224 566 239L638 526H755L599 0H470L410 251Q406 270 400.5 294Q395 318 390.5 342Q386 366 383 383H377Q377 376 374 358Q371 340 365.5 313Q360 286 351 251L289 0Z"/><path transform="translate(4057 0)" d="M65 607V723H187V607ZM65 0V526H187V0Z"/><path transform="translate(4289 0)" d="M284 -12Q202 -12 147.5 17.5Q93 47 66 108Q39 169 39 263Q39 358 66.5 418.5Q94 479 148.5 508.5Q203 538 284 538Q337 538 378 525Q419 512 448.5 485.5Q478 459 493 420Q508 381 508 329H384Q384 366 373 390Q362 414 339.5 426.5Q317 439 282 439Q241 439 215 420Q189 401 176.5 363.5Q164 326 164 269V256Q164 200 176.5 162Q189 124 216 105.5Q243 87 287 87Q321 87 343.5 99.5Q366 112 378 137Q390 162 390 197H508Q508 148 493 109Q478 70 449 43Q420 16 378.5 2Q337 -12 284 -12Z"/><path d="M4840 0 4840 57 5106 426 4857 426 4857 526 5313.33 526 5284 470 5017 100 5205.38 100 5153 0Z"/><path d="M5144.53 -146 5624.33 770 5784.33 770 5304.53 -146Z"/></g></svg>
    <span>&nbsp;|&nbsp; ver. <span id="wersjaStopka">—</span></span>
  </div>

<div class="modal" id="modalAkt">
  <div class="modalKarta" style="max-width:430px; text-align:center">
    <div class="modalTyt" style="justify-content:center">Nowa wersja</div>
    <div class="aktTxt" id="aktTxt"></div>
    <div id="aktPasek" style="display:none">
      <div class="bar"><div class="fill" id="aktFill"></div></div>
      <div class="aktInfo"><span id="aktEtap"></span><span id="aktMb"></span></div>
    </div>
    <button class="zielony" id="aktPobierz">Zaktualizuj i uruchom ponownie</button>
    <button class="ghost" id="aktPozniej"
            onclick="$('modalAkt').classList.remove('on')">Później</button>
  </div>
</div>

<div class="modal" id="modal">
  <div class="modalKarta">
    <div class="modalTyt">Ustawienia<span class="wers" id="wers"></span></div>
    <div class="powiad" id="powiad"></div>

    <div class="pole"><label>Klucz API Google AI Studio</label>
      <input id="uKlucz" type="password" spellcheck="false" placeholder="wklej klucz API"></div>

    <div class="opcje">
      <div class="pole pelna"><label>Model</label>
        <select id="uModel">
          <option value="gemini-3.5-flash-lite">gemini-3.5-flash-lite</option>
          <option value="gemini-3.5-flash">gemini-3.5-flash</option>
          <option value="gemini-3.1-flash-lite">gemini-3.1-flash-lite</option>
          <option value="gemini-flash-lite-latest">gemini-flash-lite-latest</option>
        </select></div>
      <div class="pole"><label>Kadrów DEPO w zapytaniu</label>
        <input id="uNaReq" type="number" min="1" max="30"></div>
      <div class="pole"><label>Zapytań na minutę</label>
        <input id="uRpm" type="number" min="1" max="60"></div>
      <div class="pole"><label>Zdjęć ALUPROF w zapytaniu</label>
        <input id="uNaReqAlu" type="number" min="1" max="30"></div>
    </div>

    <div class="pole"><label>Lista profili ALUPROF <span id="uProfIle"></span>
        <a onclick="profDomyslne()">przywróć wbudowaną</a></label>
      <textarea id="uProfile" spellcheck="false" placeholder="jedna pozycja w linii"></textarea></div>

    <button class="ghost" onclick="akt()">Sprawdź aktualizacje</button>
    <div class="info" id="uInfo"></div>

    <button onclick="ustZapisz()">Zapisz</button>
    <button class="ghost" onclick="ustZamknij()">Zamknij</button>
  </div>
</div>
</div>
<script>
let PLIKI = [], TIK = null, UST = {}, TRYB = 'depo', FOLDER = null;
const $ = id => document.getElementById(id);
const post = (p, d) => fetch(p, {method:'POST', headers:{'Content-Type':'application/json'},
  body: JSON.stringify(d||{})}).then(r => r.json());

// Przelacznik DEPO / ALUPROF. Zmienia kolor wiodacy, lewy panel i sposob wyboru.
function ustawTryb(t, zapisz){
  if (TIK) return;                               // w trakcie pracy nie przelaczamy
  const zmiana = t !== TRYB;
  TRYB = t;
  document.body.classList.toggle('alu', t === 'alu');
  document.querySelectorAll('.tryb').forEach(e => e.classList.toggle('on', e.dataset.t === t));
  if (zapisz) post('/api/tryb', {tryb: t});
  if (zmiana){
    $('err').textContent = '';
    $('wyn').innerHTML = '<div class="pusto">—</div>';
    $('pokaz').disabled = true;
  }
  rysuj();
}

// ALUPROF: jeden folder, program sam znajdzie w nim wszystkie zdjecia
function wybierzFolder(){
  post('/api/wybierz_folder').then(d => {
    if (d.error) { $('err').textContent = d.error; return; }
    if (!d.folder) return;                       // anulowano okno
    $('err').textContent = '';
    FOLDER = d;
    if (!d.zdjecia && !d.archiwa) $('err').textContent = 'w tym folderze nie ma zdjęć ani archiwów';
    rysuj();
  });
}

// Kolejne klikniecia DOKLADAJA pliki do listy, nie kasuja jej.
function dodaj(){
  if (TRYB === 'alu') return wybierzFolder();
  post('/api/wybierz').then(d => {
    if (d.error) { $('err').textContent = d.error; return; }
    $('err').textContent = '';
    const przed = PLIKI.length;
    (d.pliki || []).forEach(p => { if (!PLIKI.includes(p)) PLIKI.push(p); });
    if (PLIKI.length === przed && (d.pliki||[]).length)
      $('err').textContent = 'te pliki już są na liście';
    rysuj();
  });
}
function usun(i, e){ e.stopPropagation(); PLIKI.splice(i, 1); rysuj(); }
function wyczysc(e){ e.stopPropagation(); PLIKI = []; rysuj(); }

const odm = (n, a, b, c) => n + ' ' + (n === 1 ? a : (n%10 >= 2 && n%10 <= 4 &&
  (n%100 < 10 || n%100 >= 20) ? b : c));

function rysuj(){
  const box = $('pliki'), nag = $('naglowek');
  box.innerHTML = '';
  if (TRYB === 'alu'){
    box.classList.remove('on'); nag.style.display = 'none';
    if (!FOLDER){
      $('dropTxt').textContent = 'Wybierz folder';
      $('go').disabled = true; return;
    }
    $('dropTxt').textContent = FOLDER.nazwa;
    $('aluInfo').innerHTML = '';
    const t = document.createElement('div');
    t.textContent = odm(FOLDER.zdjecia, 'zdjęcie', 'zdjęcia', 'zdjęć') +
      (FOLDER.archiwa ? ' + ' + odm(FOLDER.archiwa, 'archiwum', 'archiwa', 'archiwów') +
       ' do rozpakowania' : '');
    t.style.cssText = 'color:var(--txt); font-weight:590';
    const f = document.createElement('div');
    f.textContent = FOLDER.folder; f.style.cssText = 'overflow-wrap:anywhere';
    const w = document.createElement('div');
    w.textContent = 'wyniki: ' + FOLDER.nazwa + ' MakroSkan';
    $('aluInfo').append(t, f, w);
    $('go').disabled = !(FOLDER.zdjecia || FOLDER.archiwa);
    return;
  }
  if (!PLIKI.length){
    box.classList.remove('on'); nag.style.display = 'none';
    $('dropTxt').textContent = 'Wybierz zdjęcia';
    $('go').disabled = true; return;
  }
  box.classList.add('on'); nag.style.display = 'flex';
  $('dropTxt').textContent = PLIKI.length + ' ' +
    (PLIKI.length === 1 ? 'zdjęcie' : (PLIKI.length < 5 ? 'zdjęcia' : 'zdjęć'));
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
    PLIKI = []; FOLDER = null;
    $('aluInfo').innerHTML = ALU_INFO;
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
    $('uNaReqAlu').value = U.na_req_alu;
    $('uProfile').value  = U.profile_tekst || '';
    $('uProfIle').textContent = '· ' + U.profile_ile + (U.profile_wlasne ? ' · własna' : '');
    if (!TIK) ustawTryb(U.tryb === 'alu' ? 'alu' : 'depo', false);
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
      $('powiad').textContent = 'Żeby zacząć, wklej klucz API z Google AI Studio ' +
        '(aistudio.google.com/apikey). Reszta ustawień jest już gotowa.';
      $('powiad').classList.add('on');
      ustOtworz();
    }
    return U;
  });
}
function ustOtworz(){ $('modal').classList.add('on'); }
// czyszczymy pole — przy zapisie pusta lista oznacza wbudowana
function profDomyslne(){ $('uProfile').value = ''; ustZapisz(); }
function ustZamknij(){ $('modal').classList.remove('on'); }
function ustZapisz(){
  post('/api/ustawienia', {zapisz: 1, klucz: $('uKlucz').value.trim(),
    model: $('uModel').value, na_req: +$('uNaReq').value,
    rpm: +$('uRpm').value, na_req_alu: +$('uNaReqAlu').value || 10,
    profile: $('uProfile').value}).then(U => {
    UST = U;
    $('uProfile').value = U.profile_tekst || '';
    $('uProfIle').textContent = '· ' + U.profile_ile + (U.profile_wlasne ? ' · własna' : '');
    if (U.ma_klucz){ modele(); $('powiad').classList.remove('on'); ustZamknij(); }
    else { $('powiad').textContent = 'Klucz jest pusty — bez niego nic nie odczytam.';
           $('powiad').classList.add('on'); }
  });
}
// przy starcie sprawdzamy po cichu; modal pokazujemy tylko gdy faktycznie jest nowsza
function aktStart(){
  post('/api/aktualizacja').then(a => {
    if (a.blad || !a.nowsza) return;
    $('aktTxt').innerHTML = 'Dostępna jest wersja <b>' + a.najnowsza +
      '</b>.<br>Masz zainstalowaną ' + a.wersja + '.';
    $('aktPobierz').onclick = () => zaktualizuj(a);
    $('modalAkt').classList.add('on');
  });
}
// Pobiera nowe wydanie, podmienia aplikacje w miejscu i uruchamia ja ponownie.
function zaktualizuj(a){
  const b = $('aktPobierz');
  b.disabled = true; $('aktPozniej').disabled = true;
  b.textContent = 'Aktualizuję…';
  $('aktTxt').innerHTML = 'Pobieram wersję <b>' + a.najnowsza +
    '</b>. Aplikacja zamknie się i otworzy ponownie sama.';
  $('aktPasek').style.display = 'block';
  post('/api/zaktualizuj').then(w => {
    if (w.error){ aktBlad(w.error); return; }
    sledzAkt();
  }).catch(() => sledzAkt());
}

function aktBlad(txt){
  $('aktPobierz').disabled = false; $('aktPozniej').disabled = false;
  $('aktPobierz').textContent = 'Spróbuj ponownie';
  $('aktPasek').style.display = 'none';
  $('aktTxt').textContent = txt;
}

// Odpytujemy serwer o postep. Gdy przestaje odpowiadac, a byl juz etap podmiany,
// to znaczy ze aplikacja wlasnie sie zamyka — czekamy, az wstanie nowa.
function sledzAkt(){
  let podmieniano = false;
  const tik = setInterval(() => {
    post('/api/stan_akt').then(A => {
      if (A.blad){
        clearInterval(tik);
        aktBlad(A.blad + (A.log ? '\n\nSzczegóły: ' + A.log : ''));
        return;
      }
      if (A.etap === 'podmieniam aplikację' || A.gotowe) podmieniano = true;
      $('aktFill').style.width = (A.procent || 0) + '%';
      $('aktEtap').textContent = A.etap || '';
      $('aktMb').textContent = A.mb_calosc
        ? A.mb.toFixed(1) + ' / ' + A.mb_calosc.toFixed(1) + ' MB' : '';
      if (A.gotowe){
        clearInterval(tik);
        $('aktEtap').textContent = 'uruchamiam ponownie…';
        $('aktTxt').innerHTML = 'Gotowe. Aplikacja startuje w wersji <b>' +
          A.wersja + '</b>.';
      }
    }).catch(() => {
      if (podmieniano){    // serwer zniknal w trakcie podmiany — to normalne
        clearInterval(tik);
        $('aktEtap').textContent = 'uruchamiam ponownie…';
        $('aktFill').style.width = '100%';
      }
    });
  }, 400);
}

function akt(){
  $('uInfo').textContent = 'sprawdzam…';
  post('/api/aktualizacja').then(a => {
    if (a.blad){ $('uInfo').textContent = 'nie sprawdziłem: ' + a.blad; return; }
    if (a.nowsza){
      $('uInfo').textContent = '';
      $('aktTxt').innerHTML = 'Dostępna jest wersja <b>' + a.najnowsza +
        '</b>.<br>Masz zainstalowaną ' + a.wersja + '.';
      $('aktPobierz').onclick = () => zaktualizuj(a);
      ustZamknij();
      $('modalAkt').classList.add('on');
    } else $('uInfo').textContent = 'masz najnowszą wersję (' + a.wersja + ')';
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
  const dane = TRYB === 'alu'
    ? {tryb: 'alu', folder: FOLDER && FOLDER.folder, model: UST.model,
       na_req_alu: +UST.na_req_alu, rpm: +UST.rpm}
    : {pliki: PLIKI, model: UST.model, na_req: +UST.na_req, rpm: +UST.rpm};
  post('/api/start', dane).then(d => {
    if (d.error) { $('err').textContent = d.error; return; }
    $('go').style.display = 'none'; $('stop').style.display = 'block';
    $('tryby').classList.add('zablok');
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
      $('tryby').classList.remove('zablok');
      $('go').style.display = 'block'; $('stop').style.display = 'none';
      $('bar').classList.add('done'); $('fill').style.width = '100%';
      $('pokaz').disabled = false;
      post('/api/zuzycie').then(d => { if (d.dzis != null) licznik(d.dzis, d.data); });
      const w = $('wyn'); w.innerHTML = '';
      if (!(s.wyniki||[]).length) w.innerHTML = '<div class="pusto">brak wyników</div>';
      if (s.tryb === 'alu') return wynikiAlu(s);
      (s.wyniki||[]).forEach(r => {
        const d = document.createElement('div');
        const wiele = r.kod.endsWith('+wiele');
        d.className = 'row' + (r.kod === '?' ? ' spr' : (wiele ? ' wiele' : ''));
        const bad = r.kod === '?' ? 'warn">sprawdź' : (wiele ? 'wiele">wiele etykiet' : 'ok">ok');
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

const ALU_INFO = $('aluInfo').innerHTML;

// Wiersz wynikow: plakietka + tekst + dopisek z prawej. Tekst wstawiamy jako tekst,
// nie HTML — nazwy plikow i napisy z AI moga zawierac cokolwiek.
function wiersz(klasa, bad, badTxt, kod, prawa, prawaKl){
  const d = document.createElement('div');
  d.className = 'row ' + klasa;
  if (bad){ const b = document.createElement('span'); b.className = 'badge ' + bad;
            b.textContent = badTxt; d.appendChild(b); }
  const k = document.createElement('span'); k.className = 'kod'; k.textContent = kod;
  const r = document.createElement('span'); r.className = prawaKl || 'str'; r.textContent = prawa;
  d.append(k, r);
  return d;
}

// ALUPROF: podsumowanie, lista produktow z iloscia, potem kazde zdjecie ze statusem
function wynikiAlu(s){
  const w = $('wyn'), W = s.wyniki || [];
  if (!W.length) return;
  const ile = k => W.filter(r => r.status === k).length;
  const pod = document.createElement('div'); pod.className = 'podsum';
  [['ok', 'rozpoznane ' + ile('ok') + ' / ' + W.length],
   ['warn', 'niepewne ' + ile('niepewne'), ile('niepewne')],
   ['warn', 'spoza listy ' + ile('spoza'), ile('spoza')],
   ['bad', 'nierozpoznane ' + (ile('brak') + ile('blad')), ile('brak') + ile('blad')]]
    .forEach(([k, t, n]) => { if (n === 0) return;
      const b = document.createElement('span'); b.className = 'badge ' + k;
      b.textContent = t; pod.appendChild(b); });
  w.appendChild(pod);

  const t1 = document.createElement('div'); t1.className = 'podtyt';
  t1.textContent = 'Produkty'; w.appendChild(t1);
  if (!(s.produkty||[]).length) w.insertAdjacentHTML('beforeend', '<div class="pusto">żaden produkt nie został rozpoznany</div>');
  (s.produkty||[]).forEach(p => w.appendChild(wiersz('prod', '', '', p.produkt +
    (p.niepewne ? '  (niepewne: ' + p.niepewne + ')' : ''), '× ' + p.ile, 'ile')));

  const t2 = document.createElement('div'); t2.className = 'podtyt';
  t2.textContent = 'Zdjęcia'; w.appendChild(t2);
  const opis = {ok: ['ok', 'ok', ''], niepewne: ['warn', 'niepewne', 'spr'],
                spoza: ['warn', 'spoza listy', 'spr'], brak: ['bad', 'brak', 'bad'],
                blad: ['bad', 'błąd pliku', 'bad']};
  W.forEach(r => {
    const [b, bt, kl] = opis[r.status] || opis.brak;
    const kod = r.kod || (r.napis ? '„' + r.napis + '”' : '—');
    const d = wiersz(kl, b, bt, kod, r.stary + '  →  ' + r.nowy);
    d.title = r.stary + '\n→ ' + r.nowy + (r.napis ? '\nnapis: ' + r.napis : '');
    w.appendChild(d);
  });
}

function licznik(n, data){
  $('zuz').textContent = 'dziś: ' + n;
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
        OKNO = webview.create_window(NAZWA, f'http://127.0.0.1:{port}/',
                                     width=1300, height=880, min_size=(980, 640))
        webview.start()
    except ImportError:                            # bez pywebview — otwieramy przegladarke
        import webbrowser
        webbrowser.open(f'http://127.0.0.1:{port}/')
        print(f'{NAZWA}: http://127.0.0.1:{port}/   (Ctrl+C konczy)')
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass


if __name__ == '__main__':
    main()
