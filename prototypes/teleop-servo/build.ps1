param([string]$Port, [switch]$Flash)
$ErrorActionPreference = 'Stop'
# Use the installed IDF 6.1 toolchain without the interactive profile's EIM selector.
$env:IDF_PATH = 'C:\Users\sahlu\esp\v6.1\esp-idf'
$env:ESP_IDF_VERSION = '6.1'
$env:IDF_TOOLS_PATH = 'C:\Espressif\tools'
$env:IDF_PYTHON_ENV_PATH = 'C:\Espressif\tools\python\v6.1\venv'
$env:IDF_COMPONENT_LOCAL_STORAGE_URL = 'file://C:\Espressif\tools'
$env:ESP_ROM_ELF_DIR = 'C:\Espressif\tools\esp-rom-elfs\20241011'
$env:PYTHONUTF8 = '1'
$env:IDF_CCACHE_ENABLE = '0'
$env:PATH = @(
    "$env:IDF_PYTHON_ENV_PATH\Scripts"
    'C:\Espressif\tools\cmake\4.0.3\bin'
    'C:\Espressif\tools\ninja\1.12.1'
    'C:\Espressif\tools\xtensa-esp-elf\esp-15.2.0_20251204\xtensa-esp-elf\bin'
    'C:\Espressif\tools\esp32ulp-elf\2.38_20240113\esp32ulp-elf\bin'
    $env:PATH
) -join ';'
Push-Location (Join-Path $PSScriptRoot 'firmware')
try {
    $idfArgs = @('build')
    if ($Flash) {
        if (-not $Port) { throw 'Specify the ESP32 port, for example -Flash -Port COM25' }
        $idfArgs = @('-p', $Port, 'flash')
    }
    & "$env:IDF_PYTHON_ENV_PATH\Scripts\python.exe" "$env:IDF_PATH\tools\idf.py" @idfArgs
    if ($LASTEXITCODE -ne 0) { throw "ESP-IDF exited with $LASTEXITCODE" }
} finally { Pop-Location }
