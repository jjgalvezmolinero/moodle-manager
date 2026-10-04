# Arranca Moodle Manager en Windows (Docker Desktop con backend WSL2).
#
# Genera/actualiza el fichero .env con las rutas que necesita compose.windows.yml
# y levanta el contenedor. Tras la primera ejecución, `docker compose up -d`
# funciona igual porque el .env ya activa el override de Windows.
#
# Uso:
#   powershell -ExecutionPolicy Bypass -File .\start-windows.ps1
#   powershell -ExecutionPolicy Bypass -File .\start-windows.ps1 -NoStart   # solo genera el .env

param(
    [switch]$NoStart
)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot

if ($root -notmatch '^([A-Za-z]):\\(.*)$') {
    throw "El proyecto debe estar en una unidad local (C:\..., D:\...). Ruta actual: $root"
}
$drive = $Matches[1].ToLower()
$rest = ($Matches[2] -replace '\\', '/').TrimEnd('/')
$mountPrefix = '/run/desktop/mnt/host'
$bundledPath = "$mountPrefix/$drive/$rest/moodle-docker"

$managed = [ordered]@{
    'COMPOSE_FILE'               = 'compose.yml;compose.windows.yml'
    'COMPOSE_PATH_SEPARATOR'     = ';'
    'MOODLE_DOCKER_BUNDLED_PATH' = $bundledPath
    'HOST_BROWSE_ROOT'           = $env:USERPROFILE
}

# Conserva las variables que el usuario tenga en .env y reescribe solo las gestionadas.
$envFile = Join-Path $root '.env'
$lines = @()
if (Test-Path $envFile) {
    $lines = Get-Content $envFile | Where-Object {
        $key = ($_ -split '=', 2)[0].Trim()
        -not $managed.Contains($key)
    }
}
foreach ($key in $managed.Keys) {
    # Comillas simples: compose no interpreta las barras invertidas de las rutas Windows.
    $lines += "$key='$($managed[$key])'"
}
# UTF-8 sin BOM: compose no acepta el BOM en la primera clave.
[System.IO.File]::WriteAllLines($envFile, [string[]]$lines)

Write-Host "[moodle-manager] .env actualizado:"
foreach ($key in $managed.Keys) { Write-Host "  $key=$($managed[$key])" }

if ($NoStart) { return }

docker compose up -d --build
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Write-Host "[moodle-manager] Listo: http://localhost:9000"
