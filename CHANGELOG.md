# Historia zmian

Format oparty na [Keep a Changelog](https://keepachangelog.com/pl/1.1.0/).
Numeracja wersji: `GŁÓWNA.POBOCZNA.POPRAWKA`.

Wersję podnosi się w stałej `WERSJA` w pliku `DEPOSKAN.py`, a wydanie tworzy tag:

```bash
git tag v1.0.1 && git push --tags
```

GitHub Actions zbuduje wtedy `.app` i `.exe` i utworzy Release.

---

## [Niewydane]

Nic w toku.

---

## [1.1.0] — 2026-09-09

### Poprawione

- **Aktualizacja na Windows nie podmieniała pliku.** Pobranie i weryfikacja działały,
  ale sama podmiana cicho zawodziła i po restarcie wracała stara wersja. Trzy przyczyny:
  - **Nie da się nadpisać działającego pliku `.exe`.** Skrypt czekał na zamknięcie
    aplikacji tylko po nazwie procesu, a PyInstaller w trybie onefile uruchamia proces
    potomny o tej samej nazwie. Gdy czekanie się nie powiodło, `Copy-Item` padał
    **bez żadnego komunikatu** i uruchamiana była stara wersja.
    Teraz podmiana idzie przez zmianę nazwy — działający plik da się przemianować,
    choć nie da się go nadpisać — z ponawianiem przez 40 sekund i przywróceniem
    stanu wyjściowego, gdyby kopiowanie zawiodło.
  - Skrypt kasował katalog tymczasowy, **w którym sam się wykonywał**. Teraz leży
    poza nim.
  - Brak jakiegokolwiek logu. Teraz zapisuje przebieg do
    `%TEMP%\deposkan-aktualizacja.log`, a ścieżka pokazuje się w oknie przy błędzie.
- Aplikacja czeka na zamknięcie po numerze procesu, nie tylko po nazwie, i daje
  systemowi Windows więcej czasu na zwolnienie pliku.

### Dodane

- Składnia skryptu podmiany jest sprawdzana przy każdym budowaniu na maszynie Windows.
  Wcześniej sprawdzany był tylko instalator, więc błąd w skrypcie aktualizacji wyszedłby
  dopiero u użytkownika.

---

## [1.0.9] — 2026-09-08

### Poprawione

- **Poświata przycisku „Skanuj" nie jest już ucinana po bokach.** Kolumny przycinały
  wszystko, co wystaje poza ich obrys, a cień przycisku sięga 28 px. Teraz przewijanie
  siedzi wyłącznie na liście plików i na logu, więc nic nie obcina cieni.

### Zmienione

- Przyciski „Skanuj" i „Pokaż wyniki w Finderze" stoją równo, na jednej wysokości
  u dołu obu kolumn.
- Podpis ze znakiem MAK przeniesiony do stopki na całej szerokości okna, pod obiema
  kolumnami, i wyśrodkowany.

---

## [1.0.8] — 2026-09-08

### Poprawione

- **Polskie znaki w całym interfejsie.** Teksty w oknie, komunikaty i wpisy w logu
  były pisane bez ogonków („Kadrow w zapytaniu", „zapisuje pliki", „drugie podejscie").
  Teraz wszystko jest po polsku poprawnie. Nazwy plików i funkcji zostały bez zmian —
  celowo, żeby nie psuć zgodności między systemami.

---

## [1.0.7] — 2026-09-08

### Zmienione

- **Aktualizacja pokazuje pasek postępu na żywo.** Wcześniej po kliknięciu przycisku
  okno stało bez znaku życia aż do końca pobierania — nie było wiadomo, czy działa,
  czy się zawiesiło. Teraz widać etap (pobieram → sprawdzam sumę kontrolną →
  podmieniam → uruchamiam ponownie), procent i megabajty (`38,4 / 62,1 MB`).
- Aktualizacja pracuje w osobnym wątku; wcześniej blokowała serwer na cały czas
  pobierania, więc interfejs nie miał jak zapytać o postęp.
- Błąd aktualizacji pokazuje się w oknie razem z przyciskiem „Spróbuj ponownie"
  zamiast pozostawiać zablokowany przycisk.
- Zanik połączenia w trakcie podmiany nie jest już mylony z awarią — to normalny
  moment, w którym aplikacja się zamyka przed restartem.

---

## [1.0.6] — 2026-09-08

### Dodane

- Stopka pod przyciskiem Skanuj: znak MAK i podpis
  „Stworzono w Makro-Plast® przez Kacper Makarewicz | ver. X.Y.Z".
  Numer wersji podstawia się sam.
- Znak firmowy w wersji wektorowej (`MODULES/logo/mak.svg`) i rastrowej
  (`MODULES/logo/mak.png`), obrysowany z oryginału — różnica 0,17% pikseli.

---

## [1.0.5] — 2026-09-08

### Zmienione

- **Pasek postępu pokazuje też zapisywanie plików.** Wcześniej po zakończeniu odczytu
  stał na 100%, podczas gdy kopiowanie kilkuset zdjęć trwało jeszcze długo. Teraz
  ostatni etap ma własny postęp i opis (`zapisuje 120/175: _DSC0120.JPG`).
- Pasek postępu dwa razy wyższy.
- Przycisk „Zacznij" nazywa się teraz **„Skanuj"** i siedzi na stałe u dołu lewej
  kolumny; lista wybranych zdjęć zajmuje całą przestrzeń nad nim.
- Zębatka ustawień powiększona do rozmiaru strzałki odświeżania, wyśrodkowana
  i już się nie obraca przy najechaniu (obraca się tylko strzałka).

---

## [1.0.4] — 2026-09-08

### Zmienione

- Przyciski ustawień i odświeżania w nagłówku powiększone o ok. 20% (38 → 46 px).

---

## [1.0.3] — 2026-09-08

### Dodane

- **Aktualizacja podmienia aplikację w miejscu.** Wcześniej przycisk otwierał stronę
  z plikiem do ręcznego pobrania; teraz aplikacja sama pobiera nowe wydanie,
  sprawdza jego sumę SHA-256, podmienia się na dysku i uruchamia ponownie —
  bez udziału użytkownika.
- Sprawdzenie sumy kontrolnej przed podmianą; niezgodność przerywa aktualizację.
- Podmiany dokonuje osobny skrypt pomocniczy, który czeka na zamknięcie aplikacji
  (program nie może nadpisać sam siebie w trakcie działania).

---

## [1.0.2] — 2026-09-08

### Dodane

- **Instalatory jedną komendą dla macOS i Windows**, omijające ostrzeżenia systemu.
  Etykietę, przez którą Gatekeeper i SmartScreen blokują aplikację (kwarantanna
  na macOS, „Mark of the Web" na Windows), nakłada przeglądarka przy pobieraniu —
  nie system. Plik pobrany z Terminala lub PowerShella jej nie dostaje, więc
  aplikacja uruchamia się normalnie.
  - macOS: `curl -fsSL .../instaluj-mac.sh | bash`
  - Windows: `irm .../instaluj-win.ps1 | iex`
  - Ta sama komenda służy do aktualizacji.
  - Instalator Windows tworzy skróty w Menu Start i na pulpicie.
- Składnia instalatora Windows jest sprawdzana przy każdym budowaniu na maszynie
  Windows w GitHub Actions.

### Uwagi

- Pobranie plików wprost z Releases nadal działa tak samo — różnica polega tylko
  na jednorazowym ostrzeżeniu, które trzeba wtedy zatwierdzić.

---

## [1.0.1] — 2026-09-08

### Poprawione

- Wydanie na macOS pakowane przez `ditto` zamiast `zip`. Zwykły `zip` gubi atrybuty
  i dowiązania w bundlu, przez co podpis potrafił się zepsuć, a system pokazywał
  „plik uszkodzony" zamiast zwykłego ostrzeżenia o niezweryfikowanym twórcy.

### Dodane

- Każde wydanie zawiera `checksums.txt` z sumami SHA-256 obu plików.
- README wyjaśnia, skąd biorą się ostrzeżenia systemowe i jak samodzielnie
  zweryfikować pobrane pliki.

---

## [1.0.0] — 2026-09-08

Pierwsze wydanie. Aplikacja okienkowa na macOS i Windows, zbudowana z jednego
pliku źródłowego.

### Dodane

**Rozpoznawanie numerów**
- Detekcja czerwonego odręcznego numeru w OpenCV — lokalnie i za darmo, ok. 0,1 s
  na zdjęcie. Maska łączy zakres HSV z testem przewagi czerwieni nad zielenią
  i niebieskim, dzięki czemu drewniany blat i beżowe tło nie są brane za pisak.
- Próg wykrywania dobiera się do oświetlenia zdjęcia (kontrast pisaka potrafi się
  różnić trzykrotnie między ujęciami).
- Filtr gęstości odrzuca lite plamy — różowe karteczki, czerwoną taśmę, czerwone
  przedmioty — bo pismo to cienkie kreski, a nie wypełniony prostokąt.
- Kadr wycinany z pełnej rozdzielczości z dużym zapasem, żeby nic nie było ucięte.
- Odczyt kadrów przez Gemini z wymuszoną odpowiedzią w JSON.

**Odporność na błędy**
- Drugie podejście: zdjęcia, na których nic nie znaleziono, lecą jednym zapytaniem
  jako całe, nieprzycięte kadry, z listą numerów już rozpoznanych w tej partii
  jako podpowiedzią.
- Wyczerpanie limitu nie kasuje pracy — to, co odczytane, dostaje nazwy i trafia
  do folderu, a reszta czeka. Ponowne uruchomienie na tym samym folderze dokańcza
  robotę i nie duplikuje plików.
- Trzy próby z narastającą pauzą (10/20/40 s) przy błędach 429, 500 i 503.
- Kadry z kilkoma etykietami naraz dostają najlepiej widoczny numer i dopisek
  `+wiele` zamiast lądować w koszu jako nieczytelne.

**Nazewnictwo i pliki**
- Wyniki trafiają do podfolderu **„Kopia z kodami"**, oryginały zostają nietknięte.
- Nazwa pliku zachowuje kolejność zdjęć: `DSC0002_P5204707-1.JPG`, a nierozpoznane
  `DSC0019_sprawdz.JPG`.
- Kopie są bajt w bajt identyczne z oryginałami, z zachowaniem EXIF i daty.

**Interfejs**
- Dwie kolumny: po lewej wybór zdjęć i sterowanie, po prawej postęp, wyniki i log —
  nic się nie przewija poza swoją kolumną.
- Lista wybranych plików z możliwością usuwania pojedynczych pozycji; kolejne
  kliknięcia dokładają zdjęcia zamiast kasować listę.
- Podgląd pracy na żywo (co program robi w tej sekundzie), timer i szklany
  zielony pasek postępu.
- Ustawienia w osobnym oknie: klucz API, model, liczba kadrów w zapytaniu,
  zapytania na minutę.
- Lista modeli pobierana automatycznie z konta przy starcie; domyślnie
  `gemini-3.5-flash-lite`.
- Sprawdzanie aktualizacji przy uruchomieniu — gdy jest nowsza wersja, pojawia się
  okno z przyciskiem pobierania.

### Bezpieczeństwo

- **Klucz API nie znajduje się w kodzie.** Wpisuje się go w Ustawieniach, a zapisuje
  lokalnie w katalogu ustawień systemu.

### Uwagi

- Klasyczne OCR nie nadaje się do tego zadania — Tesseract, nawet na idealnie
  przygotowanym czarno-białym piśmie z listą dozwolonych znaków, trafił 0 na 9.
  Powód jest strukturalny: jest trenowany na druku. Szczegóły w [DEPOSKAN.md](DEPOSKAN.md).
- Przy dużych paczkach kadrów w jednym zapytaniu spada trafność przypisania numeru
  do zdjęcia. Domyślne 25 to kompromis pod limit dobowy; przy zapasie limitu warto
  zejść niżej.

[Niewydane]: https://github.com/Kackackac4/deposkan/compare/v1.1.0...HEAD
[1.1.0]: https://github.com/Kackackac4/deposkan/releases/tag/v1.1.0
[1.0.9]: https://github.com/Kackackac4/deposkan/releases/tag/v1.0.9
[1.0.8]: https://github.com/Kackackac4/deposkan/releases/tag/v1.0.8
[1.0.7]: https://github.com/Kackackac4/deposkan/releases/tag/v1.0.7
[1.0.6]: https://github.com/Kackackac4/deposkan/releases/tag/v1.0.6
[1.0.5]: https://github.com/Kackackac4/deposkan/releases/tag/v1.0.5
[1.0.4]: https://github.com/Kackackac4/deposkan/releases/tag/v1.0.4
[1.0.3]: https://github.com/Kackackac4/deposkan/releases/tag/v1.0.3
[1.0.2]: https://github.com/Kackackac4/deposkan/releases/tag/v1.0.2
[1.0.1]: https://github.com/Kackackac4/deposkan/releases/tag/v1.0.1
[1.0.0]: https://github.com/Kackackac4/deposkan/releases/tag/v1.0.0
