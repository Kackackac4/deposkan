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

[Niewydane]: https://github.com/Kackackac4/deposkan/compare/v1.0.3...HEAD
[1.0.3]: https://github.com/Kackackac4/deposkan/releases/tag/v1.0.3
[1.0.2]: https://github.com/Kackackac4/deposkan/releases/tag/v1.0.2
[1.0.1]: https://github.com/Kackackac4/deposkan/releases/tag/v1.0.1
[1.0.0]: https://github.com/Kackackac4/deposkan/releases/tag/v1.0.0
