# DEPOSKAN

Rozpoznawanie ręcznie pisanych numerów zamówień ze zdjęć paczek.

OpenCV znajduje lokalnie czerwony numer i przycina kadr (za darmo, ~0,1 s na zdjęcie),
a do Gemini idą tylko małe wycinki. Oryginały dostają nazwy `KOD_DSCxxxx.JPG`.

## Uruchomienie

Pobierz gotowy plik z [Releases](../../releases):
- **macOS** — `DEPOSKAN-macOS.zip`, rozpakuj i przenieś `DEPOSKAN.app` do Programów
- **Windows** — `DEPOSKAN.exe`

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

## Historia zmian

Co doszło w której wersji: [CHANGELOG.md](CHANGELOG.md).

## Jak to działa

Opis algorytmu detekcji, zasady odczytu i pułapki: [DEPOSKAN.md](DEPOSKAN.md).

## Licencja

Oprogramowanie własnościowe — **wszelkie prawa zastrzeżone**,
MAKRO-PLAST Sp. z o.o. Repozytorium jest publiczne wyłącznie po to, żeby dało się
pobierać wydania; nie jest to projekt open source. Szczegóły: [LICENSE](LICENSE).
