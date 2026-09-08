# DEPOSKAN

Rozpoznawanie ręcznie pisanych numerów zamówień ze zdjęć paczek.

OpenCV znajduje lokalnie czerwony numer i przycina kadr (za darmo, ~0,1 s na zdjęcie),
a do Gemini idą tylko małe wycinki. Kopie oryginałów trafiają do podfolderu
**„Kopia z kodami"** pod nazwą `DSC0002_P5204707-1.JPG` — numer zdjęcia z przodu,
żeby zachować kolejność. Nierozpoznane dostają `DSC0019_sprawdz.JPG`.

## Uruchomienie

Pobierz gotowy plik z [Releases](../../releases):
- **macOS** — `DEPOSKAN-macOS.zip`, rozpakuj i przenieś `DEPOSKAN.app` do Programów
- **Windows** — `DEPOSKAN.exe`

### macOS blokuje pierwsze uruchomienie

Aplikacja nie jest podpisana certyfikatem Apple, więc przy pierwszym otwarciu system
pokaże ostrzeżenie. To normalne — trzeba raz zatwierdzić:

1. Rozpakuj ZIP i przenieś `DEPOSKAN.app` do folderu **Programy**.
2. Kliknij dwukrotnie — pojawi się ostrzeżenie. Zamknij je.
3. Otwórz **Ustawienia systemowe → Prywatność i ochrona**, przewiń na dół.
4. Przy komunikacie o zablokowanym DEPOSKAN kliknij **„Otwórz mimo to"**.

Robi się to **tylko raz**. Kolejne uruchomienia działają normalnie.

Kto woli terminal, jedna komenda załatwia sprawę przed pierwszym uruchomieniem:

```bash
xattr -dr com.apple.quarantine /Applications/DEPOSKAN.app
```

### Windows blokuje pierwsze uruchomienie

To samo z innej strony: SmartScreen pokaże „Windows chronił Twój komputer".
Kliknij **Więcej informacji → Uruchom mimo to**. Też tylko za pierwszym razem.

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

## Historia zmian

Co doszło w której wersji: [CHANGELOG.md](CHANGELOG.md).

## Jak to działa

Opis algorytmu detekcji, zasady odczytu i pułapki: [DEPOSKAN.md](DEPOSKAN.md).

## Licencja

Oprogramowanie własnościowe — **wszelkie prawa zastrzeżone**,
MAKRO-PLAST Sp. z o.o. Repozytorium jest publiczne wyłącznie po to, żeby dało się
pobierać wydania; nie jest to projekt open source. Szczegóły: [LICENSE](LICENSE).
