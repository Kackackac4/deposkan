# DEPOSKAN — rozpoznawanie ręcznie pisanych numerów zamówień ze zdjęć paczek

Dokumentacja dla przyszłego AI / programisty, który ma odtworzyć albo rozbudować ten proces.
Opisuje **co działa, co nie działa i dlaczego** — łącznie z pułapkami, które kosztowały
najwięcej czasu przy pierwszym podejściu (wrzesień 2026).

---

## 1. Problem

Pracownik magazynu pakuje zamówienia, na każdej paczce nakleja **drukowaną etykietę adresową**,
a na niej **czerwonym pisakiem dopisuje ręcznie numer zamówienia**. Potem fotografuje każdą
paczkę aparatem (Sony ILCE-6000, JPG 6000×3376, ~4 MB/szt.).

Zadanie: przypisać każdemu zdjęciu numer zamówienia i nazwać pliki tak, żeby dało się je
połączyć z systemem.

### Charakterystyka danych (kluczowa dla projektu)

| Cecha | Wartość | Konsekwencja |
|---|---|---|
| Zdjęć dziennie | ~172 (jeden dzień = jeden folder z datą) | wsad, nie strumień |
| Unikalnych zamówień w dniu | 18 | **ok. 8 zdjęć na jedno zamówienie** |
| Rozdzielczość | 6000×3376 | numer to często <1% kadru |
| Format numeru | `P5204707-1`, `TU1808836-1`, `OS0219136-1` | prefiks 1–2 litery + 7 cyfr + `-N` |
| Prefiksy | `P` = Łotwa, `TU` = Litwa, `OS` = Estonia | zależny od kraju odbiorcy |
| Sufiks | zwykle `-1`, ale bywa `-2` | **to jest osobna pozycja tego samego zamówienia, nie literówka** |

**Ogromna redundancja to największy atut tego zbioru.** Skoro na jedno zamówienie przypada
~8 zdjęć, to nawet gdy jedno jest nieczytelne, sąsiednie z tej samej serii dają odpowiedź.
Buduj na tym cały system kontroli jakości.

### Zdjęcia, które nie są zdjęciami paczek

W każdym dniu jest ~20% zdjęć bez numeru — i to jest **normalne, nie błąd**:

- **karteczka-separator** — żółta samoprzylepna z napisem `DEPO NA <data>, N zamówień, M palet`,
  pisana **czarnym** długopisem. Oznacza początek partii z danego dnia.
- **ujęcia kontekstowe** — rolka taśmy, różowa karteczka na regale, blat roboczy.
- **zbiorcze ujęcia półki** — widać kilkanaście etykiet naraz, każda z innym numerem.
  Takiemu zdjęciu **nie da się przypisać jednego kodu** i nie należy próbować.

---

## 2. Czego NIE robić (sprawdzone, nie powtarzaj)

### Klasyczne OCR nie działa. W ogóle.

Tesseract dostał najlepsze możliwe wejście: maskę czerwieni zamienioną na czyste czarne pismo
na białym tle, przyciętą do samej linii, przeskalowaną do 120 px wysokości, z whitelistą
`P0123456789-` i `--psm 7`, testowaną w obu orientacjach.

**Wynik: 0/9 trafień.** Odczyty w rodzaju `P5104-`, `254203-4`, `07-7`, dwa razy pusty string.

Powód jest strukturalny: Tesseract jest trenowany na druku. Pismo odręczne to inna klasa
problemu i żadne strojenie progów tego nie zmieni. Nie trać na to czasu.

Pozostałe opcje offline (dla porządku, też nie polecane jako główne rozwiązanie):
- **EasyOCR / PaddleOCR** — to również sieci neuronowe, tylko lokalne. Na rękopisie ~60–70%
  i **nie wiadomo które odczyty są błędne**, co jest gorsze niż brak odczytu.
- **TrOCR-handwritten** — najlepszy z darmowych lokalnych, ale trenowany na angielskim piśmie
  ciągłym, nie na cyfrach pisanych grubym markerem.
- **własny klasyfikator cyfr** — teoretycznie sensowny (format jest sztywny, więc wystarczyłaby
  segmentacja na pojedyncze znaki + prosty CNN), ale to kilka godzin pracy plus ręczne
  oznaczanie danych treningowych. Patrz następny punkt — nie opłaca się.

### Nie optymalizuj kosztu AI. Nie ma czego optymalizować.

Kadr zmniejszony do ~1000 px to ok. **750 tokenów obrazu**. Dla 500 zdjęć:

| Model | Normalnie | Batch API (−50%) |
|---|---|---|
| Haiku 4.5 ($1/$5 za MTok) | ~$0,40 | **~$0,20** |
| Sonnet 5 ($2/$10) | ~$0,80 | ~$0,40 |
| Opus 5 ($5/$25) | ~$2,00 | ~$1,00 |

Cały dzień pracy magazynu to **kilkadziesiąt groszy**. Budowanie deduplikacji czy własnego
klasyfikatora, żeby zejść z 40 gr na 20 gr, to strata czasu. **Jedyne, co się liczy, to
trafność** — jeden błędnie przypisany numer na paczce kosztuje więcej niż roczne
przetwarzanie wszystkich zdjęć.

---

## 3. Architektura: dwa etapy

```
zdjęcia/            wytnij_numery.py          wycinki/         (AI czyta)
_DSC0002.JPG   ──►  detekcja czerwieni   ──►  _DSC0002.jpg  ──►  P5204707-1_DSC0002.jpg
6000×3376           kadr z zapasem            1800 px                      │
                                                                           │
                    przenies_oryginaly.py                                  │
oryginaly_wg_kodu/  ◄──────────────────────────────────────────────────────┘
P5204707-1_DSC0002.JPG   (kopia 1:1 oryginału, 6000×3376, bez kadrowania)
```

**Dlaczego dwa etapy, a nie od razu AI na całych zdjęciach?**
Model dostaje kadr ~1000 px zamiast całego zdjęcia 6000 px. Numer zajmuje wtedy sensowną
część obrazu zamiast być plamką w rogu. To jednocześnie tańsze i **znacznie dokładniejsze**.

---

## 4. Etap 1: `wytnij_numery.py` — detekcja i kadrowanie

```bash
python3 -m venv .venv && .venv/bin/pip install opencv-python-headless numpy
.venv/bin/python Deposkan/wytnij_numery.py "Deposkan/02.09.2026" wycinki [limit]
```

> Systemowy Python na macOS (homebrew) jest „externally managed" — `pip install` się nie uda.
> Zawsze venv.

Wynik na 172 zdjęciach: **166 skadrowanych, 6 oznaczonych, 19 sekund.**

### Jak działa maska czerwieni

Samo HSV nie wystarcza — **drewniany blat i beżowe tło wpadają w zakres czerwieni**.
Dlatego maska to koniunkcja dwóch warunków:

1. **HSV** — czerwień leży po obu stronach zawinięcia H:
   `(0,60,40)–(12,255,255)` OR `(165,60,40)–(180,255,255)`
2. **przewaga czerwieni w RGB** — `d = R − max(G,B)`, próg `t = max(20·czułość, 0.45·percentyl(d, 99.9))`

Drugi warunek jest tym, który odsiewa drewno. Test RGB przechodzi pisak, nie przechodzi
brązowy blat.

**Próg musi być adaptacyjny.** Zmierzone: przy dobrym świetle `d` sięga 107, przy słabym
tylko 26 — różnica ponad trzykrotna. Sztywny próg albo gubi połowę zdjęć, albo łapie tło.

### Filtry komponentów (kolejność ma znaczenie)

Po `MORPH_OPEN 3×3` (usuwa szum) i `MORPH_CLOSE` (skleja litery w jedną linię) zostają
komponenty, z których odrzucamy:

| Filtr | Wartość | Po co |
|---|---|---|
| powierzchnia min. | 400 px (fallback 250) | szum |
| powierzchnia maks. | **3% kadru** | ogromna plama tła/cienia |
| wysokość min. | 14 px (fallback 9) | pojedyncze piksele |
| proporcja `w/h` | 0.8–30 (fallback 1.2–30) | pismo jest poziome |
| **gęstość czerwieni** | **0.02–0.62** | ← najważniejszy |

**Filtr gęstości** liczy udział surowych czerwonych pikseli wewnątrz bboxa (przed operacją
CLOSE). Pismo to cienkie kreski — gęstość ~0.2. Różowa karteczka, czerwona taśma albo lity
czerwony przedmiot wypełniają prostokąt niemal całkowicie — gęstość >0.6 i wypadają.
Bez tego filtru skrypt kadruje karteczki zamiast numerów.

### Rozrost obszaru zamiast unii wszystkich komponentów

Naiwna wersja („weź bbox obejmujący wszystkie kandydaty") łączy numer z przypadkową
czerwienią po drugiej stronie kadru. Zamiast tego:

1. **seed** = największy komponent, **który leży na jasnym tle** (mediana V ≥ 70)
2. iteracyjnie dołączaj sąsiadów w odległości < `2.2·h` w poziomie i < `0.7·h` w pionie
3. powtarzaj aż do zbieżności

**Krytyczny szczegół: próg jasności obowiązuje TYLKO przy wyborze seeda, nie przy rozroście.**
Numer jest zawsze na jasnej etykiecie, więc ciemny blat nie może być punktem startowym — ale
doklejane dalej znaki bywają w cieniu. Gdy zastosowano jasność również do rozrostu,
`_DSC0007` gubił lewą połowę numeru (`P5` zniknęło, zostało `204707-1`).

### Dwa przebiegi

```python
box = szukaj(prac, skala, 1.0, 0.62, 60, 0.8, 14, 400, jasnosc_min=70)   # podstawowy
if box is None:
    box = szukaj(prac, skala, 0.7, 0.75, 90, 1.2, 9, 250, jasnosc_min=75) # czulszy
```

Drugi przebieg (niższy próg, szersze sklejanie, wymóg wyraźnie poziomego bloku) ratuje zdjęcia
w słabym świetle. Odpala się **tylko** gdy pierwszy nic nie znalazł, więc nie psuje udanych.

### Rozdzielczość detekcji — najdroższy błąd pierwszego podejścia

Pierwsza wersja skalowała zdjęcie do **2000 px** przed szukaniem. Przy zdjęciach robionych
z większej odległości numer schodził do kilku pikseli wysokości i wypadał z filtrów.

Rozwiązanie: **detekcja przy 3600 px, a kadr wycinany z oryginału 6000 px**:

```python
DETEKCJA_PX = 3600   # dłuższy bok kopii roboczej
WYNIK_PX    = 1800   # dłuższy bok zapisywanego kadru
```

Zmniejszona kopia służy **wyłącznie** do znalezienia bboxa; współrzędne przeliczamy przez
skalę i tniemy oryginał. Efekt: wykrywalność w górę i ostry wynik.

### Marginesy — kadr, nie wycinek

To była wyraźna decyzja użytkownika: **nie chodzi o wycięcie samych cyfr, tylko o przycięcie
zdjęcia do okolicy numeru z dużym zapasem**, żeby całą etykietę i kontekst było widać.

```python
mx = int(max(0.9 * bw, 4.0 * bh, 0.05 * W))   # poziomo
my = int(max(3.0 * bh, 0.30 * bw, 0.05 * H))  # pionowo
```

Margines skalowany do wielkości pisma, z twardą dolną granicą 5% boku zdjęcia.
**Nigdy nie kadruj ciasno — ucięty ogonek cyfry to błędny odczyt.**

### Gdy nic nie znaleziono

Całe zdjęcie (przeskalowane do 1800 px) trafia do tego samego folderu jako
`_DSCxxxx_DO-SPRAWDZENIA.jpg`. Nie znika, nie jest po cichu pomijane.

---

## 5. Etap 2: odczyt numerów przez AI

Model z widzeniem (Claude) czyta kadry i nadaje plikom nazwy. Wynik z pierwszego przebiegu:
**139/172 rozpoznanych, 33 do sprawdzenia.**

### Zasady odczytu — wyciągnięte z realnych pomyłek

**1. Nie czytaj wielkimi paczkami naraz.**
Przy 18–20 zdjęciach w jednym wywołaniu jakość oceny spada — łatwo prześlizgnąć się po
kadrze, w którym numer jest mały albo obrócony. Przy 20 naraz pojawiły się 4 błędy,
przy paczkach po **8 sztuk** — zero. To nie jest problem techniczny (każdy odczyt jest
przypisany do konkretnej ścieżki pliku), tylko kwestia uwagi poświęconej pojedynczemu obrazowi.

**2. ~30–40% zdjęć jest obróconych o 180°.**
Fotograf strzela z przeciwnej strony stołu. To normalne. Jeśli tekst jest do góry nogami —
obróć i czytaj, nie oznaczaj jako nieczytelny.

**3. Gdy cyfra jest niepewna — zbliż, nie zgaduj.**
Wytnij region i przeskaluj do ~1400 px szerokości:
```python
crop = im[y0-p:y1+p, x0-p:x1+p]
crop = cv2.resize(crop, None, fx=r, fy=r, interpolation=cv2.INTER_CUBIC)
crop = cv2.rotate(crop, cv2.ROTATE_180)  # gdy trzeba
```

**4. `3` i `9` w szybkim piśmie wyglądają identycznie.**
Realna pomyłka z tej sesji: `OS0219196-1` vs `OS0219136-1`. Rozstrzygnięcie: zestaw kilka
zdjęć tego samego zamówienia **jeden pod drugim w jednym obrazie** (`np.vstack`) i porównaj.
Przy siedmiu ujęciach obok siebie odpowiedź jest jednoznaczna.

**5. Wykorzystuj bliźniacze zdjęcia.**
Gdy cyfra jest fizycznie zasłonięta zagięciem folii (`_DSC0057`), znajdź inne zdjęcie tej
samej etykiety z tej samej serii (`_DSC0059`) — pracownik robi po kilka ujęć z tego samego
kąta. To nie jest zgadywanie z kontekstu, tylko odczyt z bliźniaczego ujęcia.

**6. Sufiks `-2` istnieje.**
`P5204701-1` i `P5204701-2` to dwie różne pozycje tego samego zamówienia, obie prawidłowe.
Nie „poprawiaj" `-2` na `-1`.

**7. Zbiorczego ujęcia półki nie da się przypisać do jednego kodu.**
Jeśli w kadrze widać kilkanaście etykiet z różnymi numerami — to jest zdjęcie kontekstowe,
oznacz do sprawdzenia. (Bonus: takie zdjęcia świetnie nadają się do **weryfikacji krzyżowej**
wcześniejszych odczytów, bo widać na nich wiele numerów naraz.)

**8. Gdy nie masz pewności — `sprawdz_`.**
Lepiej 33 zdjęcia do ręcznego przejrzenia niż jedno zamówienie z błędnym numerem.

### Konwencja nazw

| Wzorzec | Znaczenie |
|---|---|
| `P5204707-1_DSC0002.jpg` | rozpoznany kod + zachowany oryginalny numer DSC |
| `sprawdz_DSC0024.jpg` | AI nie było pewne |
| `_DSC0001_DO-SPRAWDZENIA.jpg` | skrypt nie wykrył czerwieni |

**Numer `DSCxxxx` musi zostać w nazwie** — to jedyny klucz łączący kadr z oryginałem.

---

## 6. Etap 3: `przenies_oryginaly.py` — połączenie z oryginałami

```bash
python3 Deposkan/przenies_oryginaly.py \
    "Deposkan/wycinki" "Deposkan/02.09.2026" "Deposkan/oryginaly_wg_kodu"
```

Tylko biblioteka standardowa, bez OpenCV. Dla każdego kadru `KOD_DSCxxxx.jpg`:
znajduje oryginał po numerze `DSCxxxx`, kopiuje `shutil.copy2` (**bajt w bajt, bez
przycinania, z zachowaniem EXIF i daty**) do folderu wyjściowego jako `KOD_DSCxxxx.JPG`.

Zabezpieczenia:

- pliki z prefiksem `sprawdz` (zbiór `NIEPEWNE_PREFIKSY`) są **pomijane** — nie mają kodu
- gdy dwa oryginały mają ten sam numer DSC → zgłoszenie konfliktu, **bez kopiowania**
- gdy dwa różne kadry dają tę samą nazwę wyjściową → zgłoszenie, **bez nadpisywania**

> **Pułapka, w którą wpadła pierwsza wersja:** regex `^(?P<kod>.+)_(?P<dsc>DSC\d+)\.` łapie
> również `sprawdz_DSC0024.jpg` i traktuje `sprawdz` jak kod zamówienia. Efekt: 167 plików
> zamiast 139. Stąd jawna lista `NIEPEWNE_PREFIKSY` — jeśli dodasz nowy prefiks dla
> niepewnych, **dopisz go tam**.

Weryfikacja poprawności kopii:
```bash
md5 Deposkan/02.09.2026/_DSC0157.JPG Deposkan/oryginaly_wg_kodu/OS0219136-1_DSC0157.JPG
# sumy muszą być identyczne
```

---

## 7. Jak to zrobić „od ręki" dla nowego dnia

```bash
# 1. kadrowanie (ok. 20 s na 172 zdjęcia)
.venv/bin/python Deposkan/wytnij_numery.py "Deposkan/<data>" "Deposkan/wycinki_<data>"

# 2. odczyt przez AI — paczkami po 8, zmiana nazw na KOD_DSCxxxx.jpg / sprawdz_DSCxxxx.jpg

# 3. połączenie z oryginałami
python3 Deposkan/przenies_oryginaly.py \
    "Deposkan/wycinki_<data>" "Deposkan/<data>" "Deposkan/oryginaly_<data>"

# 4. przegląd ręczny plików sprawdz_* i *_DO-SPRAWDZENIA
```

### Gdyby robić to skryptem przez API (produkcyjnie)

Etap 2 automatyzuje się przez Batch API (−50% kosztu). Zalecana kolejność kontroli jakości:

1. **walidacja formatu** — regex `^(P|TU|OS)\d{7}-\d$` odsiewa śmieci automatycznie
2. **głosowanie w grupach** — zdjęcia z tym samym odczytem potwierdzają się nawzajem;
   rozbieżność w serii sama zgłasza się do sprawdzenia
3. **dopasowanie do listy zamówień z systemu** — najmocniejszy filtr, praktycznie eliminuje
   błędy. **Nie było jeszcze dostępne** — jeśli da się wyeksportować numery zamówień z danego
   dnia (choćby CSV), zrób to; wtedy każdy odczyt dopasowujesz do zamkniętej listy
   i literówka typu `3` vs `9` przestaje być problemem.

Model: zacznij od **Haiku 4.5**, ale **zmierz** na 15–20 kadrach z prawdą, zanim puścisz
wszystko. Przy różnicy 20 gr vs 40 gr za komplet nie ma sensu walczyć o tańszy model —
jeśli Haiku pomyli się choć raz, bierz Sonnet.

---

## 8. Pliki

| Plik | Rola |
|---|---|
| `wytnij_numery.py` | detekcja czerwieni + kadrowanie z zapasem |
| `przenies_oryginaly.py` | łączenie kadrów z oryginałami po numerze DSC |
| `DEPOSKAN.md` | ten dokument |
| `<data>/` | oryginały z aparatu (nietykalne, tylko do odczytu) |
| `wycinki/` | kadry do odczytu + nazwy z kodami |
| `oryginaly_wg_kodu/` | wynik końcowy: pełne oryginały nazwane kodem |

---

## 9. Wynik referencyjny (02.09.2026, 172 zdjęcia)

139 rozpoznanych, 33 do sprawdzenia, 18 unikalnych zamówień:

```
16  P5204706-1     8  TU1808836-1     5  TU1808835-1
15  P5204702-1     8  P5204708-1      4  TU1808841-1
10  P5204701-1     8  P5204704-1      4  TU1808840-1
 9  P5204707-1     7  TU1808838-1     3  TU1808839-1
 9  P5204705-1     7  P5204709-1      2  P5204701-2
 9  P5204703-1     6  TU1808837-1
 9  OS0219136-1
```
