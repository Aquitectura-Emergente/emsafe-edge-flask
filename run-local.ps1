param(
    [string]$ActuatorUrl = "",
    [string]$MqttHost = "localhost"
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# La primera vez crea el .venv e instala las dependencias.
$py = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Host "[edge] Creando .venv e instalando dependencias..." -ForegroundColor Yellow
    python -m venv .venv
    & $py -m pip install --disable-pip-version-check -q -r requirements.txt
}

$env:EMSAFE_MQTT_HOST = $MqttHost
if ($ActuatorUrl) { $env:EMSAFE_ACTUATOR_URL = $ActuatorUrl }

Write-Host "==================================================" -ForegroundColor Cyan
Write-Host "  EMSafe Edge Hogar"
Write-Host "  Broker MQTT : $env:EMSAFE_MQTT_HOST"
Write-Host "  Actuador    : $(if ($env:EMSAFE_ACTUATOR_URL) { $env:EMSAFE_ACTUATOR_URL } else { 'desactivado' })"
Write-Host "  API local   : http://localhost:5000/health"
Write-Host "==================================================" -ForegroundColor Cyan

& $py app.py
