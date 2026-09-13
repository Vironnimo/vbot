param(
    [Parameter(Mandatory = $true)][string]$Installer,
    [Parameter(Mandatory = $true)][ValidateSet('server', 'server-desktop', 'desktop-client')][string]$Shape,
    [Parameter(Mandatory = $true)][string]$Version
)

$ErrorActionPreference = 'Stop'
if (-not $env:RUNNER_TEMP) {
    throw 'Installer smoke runs only in a disposable CI runner.'
}
$installerPath = (Resolve-Path -LiteralPath $Installer).Path
$temporary = Join-Path $env:RUNNER_TEMP ('vbot-installer-smoke-' + [guid]::NewGuid().ToString('N'))
$installDir = Join-Path $temporary 'application'
$dataDir = Join-Path $temporary 'data'
$installLog = Join-Path $temporary 'install.log'
$uninstallLog = Join-Path $temporary 'uninstall.log'
New-Item -ItemType Directory -Path $temporary | Out-Null
$sentinel = Join-Path $dataDir 'preserved.txt'
$listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
$listener.Start()
$port = $listener.LocalEndpoint.Port
$listener.Stop()
$vbot = Join-Path $installDir 'vBot.exe'
$verifiedInstall = $false

try {
    $setup = Start-Process -FilePath $installerPath -WindowStyle Hidden -Wait -PassThru -ArgumentList @(
        '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/NOICONS', '/TASKS=""',
        ('/DIR="' + $installDir + '"'), ('/VBOTDATA="' + $dataDir + '"'),
        '/VBOTHOST=127.0.0.1', ('/VBOTPORT=' + $port), ('/LOG="' + $installLog + '"')
    )
    if ($setup.ExitCode -ne 0) { throw "Installer failed with code $($setup.ExitCode)" }
    $state = Get-Content -LiteralPath (Join-Path $installDir 'application.json') -Raw | ConvertFrom-Json
    if ($state.schema_version -ne 1 -or $state.install_shape -cne $Shape) {
        throw 'Installed application has the wrong schema or shape'
    }
    if ($Shape -ne 'desktop-client' -and (
        $state.server_data_directory -ine $dataDir -or $state.server_port -ne $port
    )) { throw 'Installer did not preserve the requested isolated server target' }
    $active = (Get-Content -LiteralPath (Join-Path $installDir 'active-version') -Raw).Trim()
    if ($active -notmatch '^[a-z0-9_]+$') { throw 'Invalid active version pointer' }
    $release = Get-Content -LiteralPath (Join-Path $installDir "versions/$active/release.json") -Raw | ConvertFrom-Json
    if ($release.version -cne $Version) { throw 'Installer selected the wrong version' }
    if (Test-Path -LiteralPath (Join-Path $installDir '.git')) { throw 'Installer copied a Git checkout' }
    $verifiedInstall = $true
    New-Item -ItemType Directory -Path $dataDir -Force | Out-Null
    Set-Content -LiteralPath $sentinel -Value 'preserve user data' -NoNewline
    & $vbot application status
    if ($LASTEXITCODE -ne 0) { throw 'Installed application CLI did not start' }
    if ($Shape -ne 'desktop-client') {
        $status = (& $vbot server status) -join [Environment]::NewLine
        if ($LASTEXITCODE -ne 0 -or $status -notmatch '(?m)^running: no\s*$') {
            throw 'Installer started a server despite the empty startup task selection'
        }
        & $vbot server start
        if ($LASTEXITCODE -ne 0) { throw 'Installed server could not start' }
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:$port/health"
        if ($health.status -ne 'ok') { throw 'Installed server health probe failed' }
        # Leave it running: the actual uninstaller must perform its own safe stop.
    }
    $uninstallers = @(Get-ChildItem -LiteralPath $installDir -File | Where-Object { $_.Name -match '^unins\d{3}\.exe$' })
    if ($uninstallers.Count -ne 1) { throw 'Missing or ambiguous native uninstaller' }
    $removal = Start-Process -FilePath $uninstallers[0].FullName -WindowStyle Hidden -Wait -PassThru -ArgumentList @(
        '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', ('/LOG="' + $uninstallLog + '"')
    )
    if ($removal.ExitCode -ne 0) { throw "Uninstaller failed with code $($removal.ExitCode)" }
    foreach ($relative in @('vBot.exe', 'application.json', 'active-version', 'versions', 'removal-pending.json')) {
        if (Test-Path -LiteralPath (Join-Path $installDir $relative)) {
            throw "Uninstaller retained application path: $relative"
        }
    }
    if ((Get-Content -LiteralPath $sentinel -Raw) -cne 'preserve user data') {
        throw 'Uninstaller changed or removed user data'
    }
    Write-Output "Native $Shape installer, safe removal, and preserved data verified"
} catch {
    foreach ($log in @($installLog, $uninstallLog)) {
        if (Test-Path -LiteralPath $log) { Get-Content -LiteralPath $log -Tail 60 }
    }
    Write-Output "Installer smoke evidence retained at $temporary"
    throw
} finally {
    if ($verifiedInstall -and $Shape -ne 'desktop-client' -and (Test-Path -LiteralPath $vbot)) {
        & $vbot server stop
    }
}
