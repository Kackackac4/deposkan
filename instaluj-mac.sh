#!/bin/bash
# Instalator DEPOSKAN dla macOS.
#
# Pobiera najnowsze wydanie i wgrywa je do folderu Programy.
#
# Dlaczego tak, a nie przez przeglądarkę: kwarantannę ("nie można otworzyć,
# bo pochodzi od niezidentyfikowanego dewelopera") nakłada przeglądarka przy
# pobieraniu, a nie system. Plik pobrany z Terminala jej nie dostaje, więc
# aplikacja uruchamia się normalnie — bez chodzenia do Ustawień.
#
# Użycie:
#   bash instaluj-mac.sh

set -e

ZRODLO="https://github.com/Kackackac4/deposkan/releases/latest/download/DEPOSKAN-macOS.zip"
CEL="/Applications"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

echo "Pobieram najnowsze wydanie DEPOSKAN…"
curl -fL# -o "$TMP/deposkan.zip" "$ZRODLO"

echo "Rozpakowuję…"
ditto -x -k "$TMP/deposkan.zip" "$TMP"

if [ ! -d "$TMP/DEPOSKAN.app" ]; then
    echo "Coś poszło nie tak — w archiwum nie ma DEPOSKAN.app" >&2
    exit 1
fi

if [ -d "$CEL/DEPOSKAN.app" ]; then
    echo "Usuwam poprzednią wersję…"
    rm -rf "$CEL/DEPOSKAN.app"
fi

echo "Instaluję w $CEL…"
ditto "$TMP/DEPOSKAN.app" "$CEL/DEPOSKAN.app"

echo
echo "Gotowe. Uruchamiam."
open "$CEL/DEPOSKAN.app"
