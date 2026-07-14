[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$launcherPath = Join-Path $PSScriptRoot "start-frameflow.ps1"
$tokens = $null
$parseErrors = $null
$launcherAst = [System.Management.Automation.Language.Parser]::ParseFile(
    $launcherPath,
    [ref]$tokens,
    [ref]$parseErrors
)
if ($parseErrors.Count -gt 0) {
    throw "The FrameFlow launcher contains PowerShell syntax errors."
}

$contractFunction = $launcherAst.Find({
    param($node)
    return $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -eq "Test-BackendContract"
}, $true)
if ($null -eq $contractFunction) {
    throw "Test-BackendContract was not found in the FrameFlow launcher."
}
Invoke-Expression $contractFunction.Extent.Text

$script:MockOpenApi = $null
function Invoke-RestMethod {
    param(
        [string]$Uri,
        [int]$TimeoutSec
    )
    return $script:MockOpenApi
}

function New-OpenApiSpec {
    param(
        [switch]$OmitLogin,
        [switch]$OmitDelete
    )

    $paths = [ordered]@{
        '/api/v1/projects/{project_id}/timeline' = [pscustomobject]@{ get = [pscustomobject]@{} }
        '/api/v1/projects/{project_id}/preview' = [pscustomobject]@{
            get = [pscustomobject]@{}
            post = [pscustomobject]@{}
        }
        '/api/v1/auth/session' = [pscustomobject]@{ get = [pscustomobject]@{} }
    }
    if (-not $OmitLogin) {
        $paths['/api/v1/auth/login'] = [pscustomobject]@{ post = [pscustomobject]@{} }
    }

    $assetOperations = [ordered]@{ patch = [pscustomobject]@{} }
    if (-not $OmitDelete) {
        $assetOperations['delete'] = [pscustomobject]@{}
    }
    $paths['/api/v1/assets/{asset_id}'] = [pscustomobject]$assetOperations

    return [pscustomobject]@{ paths = [pscustomobject]$paths }
}

$cases = @(
    @{ Name = "complete current API"; Spec = (New-OpenApiSpec); Expected = $true },
    @{ Name = "missing login route"; Spec = (New-OpenApiSpec -OmitLogin); Expected = $false },
    @{ Name = "missing asset delete method"; Spec = (New-OpenApiSpec -OmitDelete); Expected = $false }
)

foreach ($case in $cases) {
    $script:MockOpenApi = $case.Spec
    $actual = Test-BackendContract "http://127.0.0.1:8000/api/v1/health/live"
    if ($actual -ne $case.Expected) {
        throw "Contract check '$($case.Name)' returned $actual; expected $($case.Expected)."
    }
    Write-Host "PASS: $($case.Name)"
}
