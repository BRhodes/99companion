# First run: this project's own Python in runtime\ -- the embeddable build from python.org, pip, and
# requirements.txt -- so nothing needs installing by hand, no administrator rights, and nothing outside this
# folder changes. tools\py.cmd runs it when there's no runtime\python.exe yet (and no installed Python with
# what's needed). Built in runtime.partial\ and renamed at the end, so an interrupted setup just starts over.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'      # (the progress bar makes Invoke-WebRequest many times slower)
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$version = '3.14.4'
$root = Split-Path -Parent $PSScriptRoot
$runtime = Join-Path $root 'runtime'
$partial = Join-Path $root 'runtime.partial'

try {
    if (Test-Path $partial) { Remove-Item -Recurse -Force $partial }
    New-Item -ItemType Directory $partial | Out-Null

    Write-Host "  downloading Python $version..."
    Invoke-WebRequest "https://www.python.org/ftp/python/$version/python-$version-embed-amd64.zip" -OutFile "$partial\python.zip"
    Expand-Archive "$partial\python.zip" $partial
    Remove-Item "$partial\python.zip"

    # The ._pth file is the embeddable build's whole search path: turn on site-packages (pip, numpy, Pillow)
    # and add the project folder (..) so "python -m companion..." finds it.
    $pth = Get-ChildItem $partial -Filter 'python*._pth' | Select-Object -First 1
    $lines = @(Get-Content $pth.FullName) -replace '^#\s*import site', 'import site'
    Set-Content $pth.FullName ($lines + '..') -Encoding ascii

    Write-Host '  installing pip...'
    Invoke-WebRequest 'https://bootstrap.pypa.io/get-pip.py' -OutFile "$partial\get-pip.py"
    & "$partial\python.exe" "$partial\get-pip.py" --quiet --no-warn-script-location
    if ($LASTEXITCODE) { throw 'installing pip failed' }
    Remove-Item "$partial\get-pip.py"

    Write-Host '  installing numpy and Pillow (for the map)...'
    & "$partial\python.exe" -m pip install --quiet --no-warn-script-location -r "$root\requirements.txt"
    if ($LASTEXITCODE) { throw 'installing requirements.txt failed' }

    Rename-Item $partial 'runtime'
    Write-Host '  done.'
} catch {
    Write-Host ''
    Write-Host "Setting up Python failed: $_" -ForegroundColor Red
    Write-Host 'Check the internet connection and run start.cmd again.'
    exit 1
}
