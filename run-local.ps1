# Lokálne spustenie kontroly tak, aby si neliezlo do cesty s GitHub Actions.
#
# Workflow commituje `cinema_watcher_state.json` späť do repa. Keby doň písal aj
# lokálny beh, boli by z toho dva problémy:
#   1. každý `git pull` by končil konfliktom v tom súbore;
#   2. lokálny beh by "zjedol" zmeny — označil by ich za videné, takže ten na
#      GitHube by o nich už nemal čo poslať mailom.
# Preto má lokál vlastný stav (aj log) v adresári `local/`, ktorý je v .gitignore.
#
# Použitie:
#   .\run-local.ps1                            # jedna kontrola
#   .\run-local.ps1 --watch --interval 600     # každých 10 minút
#   .\run-local.ps1 --test-email               # overenie SMTP
#
# Prvý beh si len založí stav a mail nepošle (inak by prvý mail obsahoval
# všetky termíny). Ak chceš mail aj z prvého behu: .\run-local.ps1 --mail-first-run

$ErrorActionPreference = "Stop"

$root = $PSScriptRoot
$localDir = Join-Path $root "local"
if (-not (Test-Path $localDir)) {
    New-Item -ItemType Directory -Path $localDir | Out-Null
}

# Vlastný stav a log, mimo gitu.
$env:CINEMA_STATE_FILE = Join-Path $localDir "state.json"
$env:CINEMA_LOG_FILE = Join-Path $localDir "cinema_watcher.log"

# Voliteľné nastavenia z local\.env (riadky KEY=value, `#` je komentár).
# Sem patrí SMTP_HOST, SMTP_USER, SMTP_PASS, MAIL_TO, ... Bez tohto súboru
# kontrola aj výpis fungujú normálne, len sa neposielajú e-maily.
$envFile = Join-Path $localDir ".env"
if (Test-Path $envFile) {
    foreach ($line in Get-Content $envFile) {
        if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
            $value = $Matches[2].Trim()
            # Úvodzovky okolo hodnoty sú kvôli medzerám, do premennej nepatria.
            if ($value.Length -ge 2 -and
                (($value.StartsWith('"') -and $value.EndsWith('"')) -or
                 ($value.StartsWith("'") -and $value.EndsWith("'")))) {
                $value = $value.Substring(1, $value.Length - 2)
            }
            Set-Item -Path "Env:$($Matches[1])" -Value $value
        }
    }
    Write-Host "Nastavenia načítané z $envFile" -ForegroundColor DarkGray
}

python (Join-Path $root "cinema_watcher.py") @args
exit $LASTEXITCODE
