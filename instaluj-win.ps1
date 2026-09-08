# Instalator DEPOSKAN dla Windows.
#
# Pobiera najnowsze wydanie, wgrywa do folderu uzytkownika i tworzy skroty
# w Menu Start i na pulpicie.
#
# Dlaczego tak, a nie przez przegladarke: ostrzezenie SmartScreen ("Windows
# chronil Twoj komputer") bierze sie z etykiety Mark of the Web, ktora nakłada
# PRZEGLADARKA przy pobieraniu. Plik pobrany przez PowerShell jej nie dostaje.
# To ten sam mechanizm co kwarantanna na macOS.
#
# Uzycie (PowerShell):
#   irm https://raw.githubusercontent.com/Kackackac4/deposkan/main/instaluj-win.ps1 | iex

$ErrorActionPreference = 'Stop'

$zrodlo = 'https://github.com/Kackackac4/deposkan/releases/latest/download/DEPOSKAN.exe'
$katalog = Join-Path $env:LOCALAPPDATA 'Programs\DEPOSKAN'
$plik = Join-Path $katalog 'DEPOSKAN.exe'

Write-Host 'Pobieram najnowsze wydanie DEPOSKAN...'
New-Item -ItemType Directory -Force -Path $katalog | Out-Null

# jesli aplikacja chodzi, zamykamy ja przed podmiana pliku
Get-Process DEPOSKAN -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Milliseconds 500

Invoke-WebRequest -Uri $zrodlo -OutFile $plik -UseBasicParsing

# gdyby jakas warstwa posrednia jednak doczepila Mark of the Web — zdejmujemy
Unblock-File -Path $plik -ErrorAction SilentlyContinue

Write-Host 'Tworze skroty...'
$shell = New-Object -ComObject WScript.Shell
foreach ($gdzie in @(
    (Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\DEPOSKAN.lnk'),
    (Join-Path ([Environment]::GetFolderPath('Desktop')) 'DEPOSKAN.lnk')
)) {
    $skrot = $shell.CreateShortcut($gdzie)
    $skrot.TargetPath = $plik
    $skrot.WorkingDirectory = $katalog
    $skrot.IconLocation = $plik
    $skrot.Description = 'DEPOSKAN - rozpoznawanie numerow zamowien ze zdjec'
    $skrot.Save()
}

Write-Host ''
Write-Host "Gotowe. Zainstalowano w: $katalog"
Write-Host 'Uruchamiam.'
Start-Process $plik
