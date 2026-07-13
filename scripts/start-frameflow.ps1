[CmdletBinding()]
param(
    [ValidateRange(1, 65535)]
    [int]$BackendPort = 8000,

    [ValidateRange(1, 65535)]
    [int]$FrontendPort = 5173,

    [switch]$StrictPorts,
    [switch]$NoBrowser,
    [switch]$SkipInstall
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$BackendDir = Join-Path $ProjectRoot "backend"
$FrontendDir = Join-Path $ProjectRoot "frontend"
$VenvDir = Join-Path $BackendDir ".venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
$StateFile = Join-Path $ProjectRoot ".frameflow-run.json"

$BackendProcess = $null
$FrontendProcess = $null
$OwnsStateFile = $false
$ExitCode = 0

function Write-Step {
    param([string]$Message)
    Write-Host "[FrameFlow] $Message" -ForegroundColor Cyan
}

function Test-ProcessId {
    param([int]$ProcessId)
    return $null -ne (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)
}

function Test-HttpEndpoint {
    param([string]$Uri)
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri $Uri -TimeoutSec 2
        return $response.StatusCode -ge 200 -and $response.StatusCode -lt 500
    }
    catch {
        return $false
    }
}

function Wait-ForEndpoints {
    param(
        [string]$BackendUri,
        [string]$FrontendUri,
        [int]$TimeoutSeconds = 90,
        [System.Diagnostics.Process]$Backend,
        [System.Diagnostics.Process]$Frontend
    )

    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        if ($null -ne $Backend -and $Backend.HasExited) {
            throw "The backend exited before it became ready (exit code $($Backend.ExitCode))."
        }
        if ($null -ne $Frontend -and $Frontend.HasExited) {
            throw "The frontend exited before it became ready (exit code $($Frontend.ExitCode))."
        }

        if ((Test-HttpEndpoint $BackendUri) -and (Test-HttpEndpoint $FrontendUri)) {
            return
        }
        Start-Sleep -Milliseconds 500
    }

    throw "Timed out while waiting for FrameFlow to become ready."
}

function Test-PortAvailable {
    param([int]$Port)
    $listener = $null
    try {
        $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, $Port)
        $listener.Start()
        return $true
    }
    catch {
        return $false
    }
    finally {
        if ($null -ne $listener) {
            $listener.Stop()
        }
    }
}

function Find-AvailablePort {
    param(
        [int]$PreferredPort,
        [switch]$RequirePreferred
    )

    if (Test-PortAvailable $PreferredPort) {
        return $PreferredPort
    }
    if ($RequirePreferred) {
        throw "Port $PreferredPort is already in use. Close that program or omit -StrictPorts."
    }

    $lastCandidate = [Math]::Min($PreferredPort + 30, 65535)
    if ($PreferredPort -ge $lastCandidate) {
        throw "No free port was found near $PreferredPort."
    }
    foreach ($candidate in (($PreferredPort + 1)..$lastCandidate)) {
        if (Test-PortAvailable $candidate) {
            Write-Step "Port $PreferredPort is busy; using $candidate instead."
            return $candidate
        }
    }
    throw "No free port was found near $PreferredPort."
}

function Import-DotEnv {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        return
    }

    foreach ($line in Get-Content -LiteralPath $Path) {
        if ($line -notmatch '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$') {
            continue
        }
        $name = $Matches[1]
        $value = $Matches[2].Trim()
        if (($value.StartsWith('"') -and $value.EndsWith('"')) -or
            ($value.StartsWith("'") -and $value.EndsWith("'"))) {
            $value = $value.Substring(1, $value.Length - 2)
        }
        if ($null -eq [Environment]::GetEnvironmentVariable($name, "Process")) {
            [Environment]::SetEnvironmentVariable($name, $value, "Process")
        }
    }
}

function Stop-ProcessTree {
    param([System.Diagnostics.Process]$Process)
    if ($null -eq $Process -or $Process.HasExited) {
        return
    }

    & taskkill.exe /PID $Process.Id /T /F 2>$null | Out-Null
}

function Open-FrameFlow {
    param([string]$Uri)
    Write-Step "FrameFlow is ready: $Uri"
    if (-not $NoBrowser) {
        Start-Process $Uri
    }
}

try {
    if (-not (Test-Path -LiteralPath $BackendDir) -or
        -not (Test-Path -LiteralPath $FrontendDir)) {
        throw "Run this launcher from the FrameFlow project; backend or frontend is missing."
    }

    if (Test-Path -LiteralPath $StateFile) {
        try {
            $state = Get-Content -Raw -LiteralPath $StateFile | ConvertFrom-Json
            $backendPid = [int]$state.backend_pid
            $frontendPid = [int]$state.frontend_pid
            $existingUrl = [string]$state.frontend_url
            $existingHealth = [string]$state.backend_health_url

            if ((Test-ProcessId $backendPid) -and (Test-ProcessId $frontendPid)) {
                Write-Step "FrameFlow is already running. Waiting for its page..."
                Wait-ForEndpoints -BackendUri $existingHealth -FrontendUri $existingUrl -TimeoutSeconds 60
                Open-FrameFlow $existingUrl
                return
            }
        }
        catch {
            Write-Step "Ignoring stale launcher state."
        }
        Remove-Item -Force -LiteralPath $StateFile -ErrorAction SilentlyContinue
    }

    $systemPythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if ($null -eq $systemPythonCommand) {
        throw "Python 3.11 or newer was not found. Install Python and enable 'Add Python to PATH'."
    }
    $systemPython = $systemPythonCommand.Source
    $pythonVersionText = (& $systemPython -c "import sys; print('.'.join(map(str, sys.version_info[:3])))").Trim()
    if ($LASTEXITCODE -ne 0 -or [Version]$pythonVersionText -lt [Version]"3.11") {
        throw "Python 3.11 or newer is required; found $pythonVersionText."
    }

    $nodeCommand = Get-Command node -ErrorAction SilentlyContinue
    $npmCommand = Get-Command npm.cmd -ErrorAction SilentlyContinue
    if ($null -eq $nodeCommand -or $null -eq $npmCommand) {
        throw "Node.js 20 or newer and npm were not found."
    }
    $nodeVersionText = (& $nodeCommand.Source -p "process.versions.node").Trim()
    if ($LASTEXITCODE -ne 0 -or [Version]$nodeVersionText -lt [Version]"20.0") {
        throw "Node.js 20 or newer is required; found $nodeVersionText."
    }

    if (-not (Test-Path -LiteralPath $VenvPython)) {
        if ($SkipInstall) {
            throw "The Python virtual environment is missing; run without -SkipInstall once."
        }
        Write-Step "Creating the Python virtual environment..."
        & $systemPython -m venv $VenvDir
        if ($LASTEXITCODE -ne 0) {
            throw "Could not create the Python virtual environment."
        }
    }

    $requirementsFile = Join-Path $BackendDir "requirements.txt"
    $requirementsMarker = Join-Path $VenvDir ".frameflow-requirements.sha256"
    $requirementsHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $requirementsFile).Hash
    $installedRequirementsHash = if (Test-Path -LiteralPath $requirementsMarker) {
        (Get-Content -Raw -LiteralPath $requirementsMarker).Trim()
    } else {
        ""
    }
    if ($requirementsHash -ne $installedRequirementsHash) {
        if ($SkipInstall) {
            throw "Backend dependencies are missing or outdated; run without -SkipInstall once."
        }
        Write-Step "Installing backend dependencies..."
        & $VenvPython -m pip install --disable-pip-version-check -r $requirementsFile
        if ($LASTEXITCODE -ne 0) {
            throw "Backend dependency installation failed."
        }
        Set-Content -NoNewline -LiteralPath $requirementsMarker -Value $requirementsHash
    }

    $packageLock = Join-Path $FrontendDir "package-lock.json"
    $nodeModules = Join-Path $FrontendDir "node_modules"
    $frontendMarker = Join-Path $nodeModules ".frameflow-lock.sha256"
    $packageLockHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $packageLock).Hash
    $requiredFrontendFiles = @(
        (Join-Path $nodeModules ".bin\vite.cmd"),
        (Join-Path $nodeModules "vite\package.json"),
        (Join-Path $nodeModules "react\package.json"),
        (Join-Path $nodeModules "react-dom\package.json"),
        (Join-Path $nodeModules "@vitejs\plugin-react\package.json")
    )
    $frontendDependenciesReady = $true
    foreach ($requiredFile in $requiredFrontendFiles) {
        if (-not (Test-Path -LiteralPath $requiredFile)) {
            $frontendDependenciesReady = $false
            break
        }
    }
    $installedPackageLockHash = if (Test-Path -LiteralPath $frontendMarker) {
        (Get-Content -Raw -LiteralPath $frontendMarker).Trim()
    } else {
        ""
    }
    if ($frontendDependenciesReady -and -not (Test-Path -LiteralPath $frontendMarker)) {
        # Adopt an already prepared checkout without deleting modules that may
        # currently be in use by another editor or development process.
        Set-Content -NoNewline -LiteralPath $frontendMarker -Value $packageLockHash
        $installedPackageLockHash = $packageLockHash
    }
    if (-not $frontendDependenciesReady -or $packageLockHash -ne $installedPackageLockHash) {
        if ($SkipInstall) {
            throw "Frontend dependencies are missing or outdated; run without -SkipInstall once."
        }
        Write-Step "Installing frontend dependencies..."
        Push-Location $FrontendDir
        try {
            & $npmCommand.Source ci --no-audit --no-fund
            if ($LASTEXITCODE -ne 0) {
                throw "Frontend dependency installation failed."
            }
        }
        finally {
            Pop-Location
        }
        Set-Content -NoNewline -LiteralPath $frontendMarker -Value $packageLockHash
    }

    Import-DotEnv (Join-Path $ProjectRoot ".env")
    Import-DotEnv (Join-Path $BackendDir ".env")

    $SelectedBackendPort = Find-AvailablePort -PreferredPort $BackendPort -RequirePreferred:$StrictPorts
    $SelectedFrontendPort = Find-AvailablePort -PreferredPort $FrontendPort -RequirePreferred:$StrictPorts
    if ($SelectedBackendPort -eq $SelectedFrontendPort) {
        $SelectedFrontendPort = Find-AvailablePort -PreferredPort ($SelectedFrontendPort + 1) -RequirePreferred:$false
    }

    $BackendHealthUrl = "http://127.0.0.1:$SelectedBackendPort/api/v1/health/live"
    $FrontendUrl = "http://127.0.0.1:$SelectedFrontendPort/"

    $env:HOST = "127.0.0.1"
    $env:PORT = [string]$SelectedBackendPort
    $env:VITE_API_PROXY = "http://127.0.0.1:$SelectedBackendPort"

    Write-Step "Starting API and worker on port $SelectedBackendPort..."
    $BackendProcess = Start-Process -FilePath $VenvPython `
        -ArgumentList "-u", "-m", "app.serve" `
        -WorkingDirectory $BackendDir `
        -NoNewWindow -PassThru

    Write-Step "Starting web app on port $SelectedFrontendPort..."
    $FrontendProcess = Start-Process -FilePath $npmCommand.Source `
        -ArgumentList "run", "dev", "--", "--host", "127.0.0.1", "--port", $SelectedFrontendPort, "--strictPort" `
        -WorkingDirectory $FrontendDir `
        -NoNewWindow -PassThru

    @{
        launcher_pid = $PID
        backend_pid = $BackendProcess.Id
        frontend_pid = $FrontendProcess.Id
        backend_health_url = $BackendHealthUrl
        frontend_url = $FrontendUrl
        started_at = [DateTime]::UtcNow.ToString("o")
    } | ConvertTo-Json | Set-Content -Encoding UTF8 -LiteralPath $StateFile
    $OwnsStateFile = $true

    Write-Step "Waiting for the services to become ready..."
    Wait-ForEndpoints -BackendUri $BackendHealthUrl -FrontendUri $FrontendUrl `
        -Backend $BackendProcess -Frontend $FrontendProcess
    Open-FrameFlow $FrontendUrl
    Write-Host "[FrameFlow] Keep this window open. Close it when you want to stop everything." -ForegroundColor Green

    while (-not $BackendProcess.HasExited -and -not $FrontendProcess.HasExited) {
        Start-Sleep -Seconds 1
    }

    if ($BackendProcess.HasExited) {
        throw "The backend stopped unexpectedly (exit code $($BackendProcess.ExitCode))."
    }
    throw "The frontend stopped unexpectedly (exit code $($FrontendProcess.ExitCode))."
}
catch {
    $ExitCode = 1
    Write-Host "[FrameFlow] ERROR: $($_.Exception.Message)" -ForegroundColor Red
}
finally {
    Stop-ProcessTree $FrontendProcess
    Stop-ProcessTree $BackendProcess
    if ($OwnsStateFile) {
        Remove-Item -Force -LiteralPath $StateFile -ErrorAction SilentlyContinue
    }
}

exit $ExitCode
