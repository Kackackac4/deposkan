# DEPOSKAN

[![Build](https://github.com/Kackackac4/deposkan/actions/workflows/build.yml/badge.svg)](https://github.com/Kackackac4/deposkan/actions/workflows/build.yml)
[![Wydanie](https://img.shields.io/github/v/release/Kackackac4/deposkan?label=wydanie)](https://github.com/Kackackac4/deposkan/releases/latest)
[![Pobrania](https://img.shields.io/github/downloads/Kackackac4/deposkan/total?label=pobrania)](https://github.com/Kackackac4/deposkan/releases)
![Platformy](https://img.shields.io/badge/platformy-macOS%20%7C%20Windows-lightgrey)

Rozpoznawanie ręcznie pisanych numerów zamówień ze zdjęć paczek.

OpenCV znajduje lokalnie czerwony numer i przycina kadr (za darmo, ~0,1 s na zdjęcie),
a do Gemini idą tylko małe wycinki. Kopie oryginałów trafiają do podfolderu
**„Kopia z kodami"** pod nazwą `DSC0002_P5204707-1.JPG` — numer zdjęcia z przodu,
żeby zachować kolejność. Nierozpoznane dostają `DSC0019_sprawdz.JPG`.

## Uruchomienie

Pobierz gotowy plik z [Releases](../../releases):
- **macOS** — `DEPOSKAN-macOS.zip`, rozpakuj i przenieś `DEPOSKAN.app` do Programów
- **Windows** — `DEPOSKAN.exe`

### macOS — instalacja jedną komendą (zalecane)

Wklej w **Terminal**:

```bash
curl -fsSL https://raw.githubusercontent.com/Kackackac4/deposkan/main/instaluj-mac.sh | bash
```

Pobierze najnowszą wersję, wgra do folderu Programy i uruchomi — **bez żadnych
ostrzeżeń systemu**.

Dlaczego to działa, a pobranie przez przeglądarkę nie: ostrzeżenie „nie można otworzyć,
bo pochodzi od niezidentyfikowanego dewelopera" bierze się z etykiety kwarantanny,
którą **nakłada przeglądarka** przy pobieraniu. Plik pobrany z Terminala jej nie
dostaje. To ten sam mechanizm, z którego korzysta Homebrew i większość narzędzi
instalowanych komendą.

Ta sama komenda **aktualizuje** aplikację do najnowszej wersji.

### macOS — pobranie przez przeglądarkę

Jeśli wolisz pobrać ZIP ręcznie z Releases, system zablokuje pierwsze uruchomienie
i trzeba je raz zatwierdzić:

1. Rozpakuj i przenieś `DEPOSKAN.app` do folderu **Programy**.
2. Kliknij dwukrotnie — pojawi się ostrzeżenie. Zamknij je.
3. **Ustawienia systemowe → Prywatność i ochrona**, przewiń na dół.
4. Przy komunikacie o zablokowanym DEPOSKAN kliknij **„Otwórz mimo to"**.

Robi się to tylko raz.

### Windows — instalacja jedną komendą (zalecane)

Otwórz **PowerShell** i wklej:

```powershell
irm https://raw.githubusercontent.com/Kackackac4/deposkan/main/instaluj-win.ps1 | iex
```

Pobierze najnowszą wersję, zainstaluje w folderze użytkownika, utworzy skróty
w Menu Start i na pulpicie, po czym uruchomi aplikację.

Działa z tego samego powodu co na macOS: ostrzeżenie SmartScreen bierze się
z etykiety „Mark of the Web", którą nakłada przeglądarka przy pobieraniu.
Plik pobrany przez PowerShell jej nie dostaje.

Ta sama komenda **aktualizuje** aplikację.

### Windows — pobranie przez przeglądarkę

Po pobraniu `DEPOSKAN.exe` z Releases SmartScreen pokaże „Windows chronił Twój
komputer". Kliknij **Więcej informacji → Uruchom mimo to**. Tylko za pierwszym razem.

Antywirus może zgłosić fałszywy alarm — patrz [Czy to bezpieczne](#czy-to-bezpieczne).

---

Przy pierwszym starcie aplikacja poprosi o klucz API z
[Google AI Studio](https://aistudio.google.com/apikey). Klucz zapisuje się lokalnie
i **nie ma go w kodzie**.

## Uruchomienie ze źródeł

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt      # Windows: .venv\Scripts\pip
.venv/bin/python DEPOSKAN.py
```

## Budowanie

Nie buduj ręcznie na dwa systemy — PyInstaller nie potrafi budować w poprzek platform.
Zamiast tego otaguj wersję, a GitHub Actions zbuduje `.app` i `.exe` naraz:

```bash
# podnieś WERSJA w DEPOSKAN.py, potem:
git tag v1.0.1 && git push --tags
```

Workflow (`.github/workflows/build.yml`) zbuduje oba pliki i utworzy Release.
Aplikacja sama sprawdza Releases przez „Sprawdź aktualizacje" w Ustawieniach.

## Czy to bezpieczne

Aplikacja nie ma certyfikatu Apple ani Microsoftu (kosztują one kilkaset złotych
rocznie), więc oba systemy ostrzegają przy pierwszym uruchomieniu. Ostrzeżenie mówi
tylko tyle, że **nikt nie zapłacił za certyfikat** — nie że coś jest nie tak z plikiem.

Można to sprawdzić samodzielnie:

- **Kod jest jawny** — cały program to jeden plik [DEPOSKAN.py](DEPOSKAN.py), można go
  przeczytać w kwadrans. Nie ma w nim żadnej komunikacji poza Google Gemini.
- **Pliki buduje GitHub, nie prywatny komputer** — każde wydanie powstaje automatycznie
  z kodu w tym repozytorium, a [przebieg budowania](https://github.com/Kackackac4/deposkan/actions)
  jest publiczny i można go obejrzeć krok po kroku.
- **Sumy kontrolne** — każde wydanie ma `checksums.txt` z sumami SHA-256, więc da się
  sprawdzić, czy pobrany plik jest dokładnie tym, który zbudował GitHub.
- **Można zbudować samemu** — instrukcja niżej; wynik będzie taki sam.

Antywirus na Windows może zgłosić fałszywy alarm, bo program jest spakowany
PyInstallerem, a ten sposób pakowania bywa używany także przez złośliwe
oprogramowanie. To znany problem PyInstallera, nie oznaka infekcji.

## Historia zmian

Co doszło w której wersji: [CHANGELOG.md](CHANGELOG.md).

## Jak to działa

Opis algorytmu detekcji, zasady odczytu i pułapki: [DEPOSKAN.md](DEPOSKAN.md).

## Licencja

Oprogramowanie własnościowe — **wszelkie prawa zastrzeżone**,
MAKRO-PLAST Sp. z o.o. Repozytorium jest publiczne wyłącznie po to, żeby dało się
pobierać wydania; nie jest to projekt open source. Szczegóły: [LICENSE](LICENSE).
