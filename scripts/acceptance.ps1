[CmdletBinding()]
param(
    [string]$BaseUrl = "http://127.0.0.1:8000",
    [int]$TimeoutSeconds = 90,
    [switch]$SkipCreate
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$BaseUrl = $BaseUrl.TrimEnd("/")
$ApiBase = "$BaseUrl/api/v1"

function Write-Step {
    param([string]$Message)
    Write-Host "`n==> $Message" -ForegroundColor Cyan
}

function Invoke-ContractRequest {
    param(
        [ValidateSet("GET", "POST", "PUT", "PATCH", "DELETE")]
        [string]$Method,
        [string]$Url,
        [int[]]$ExpectedStatus = @(200),
        [string]$Body,
        [hashtable]$Headers = @{}
    )

    $arguments = @{
        Uri = $Url
        Method = $Method
        Headers = $Headers
        UseBasicParsing = $true
        TimeoutSec = 30
    }
    if ($PSBoundParameters.ContainsKey("Body")) {
        $arguments["Body"] = $Body
        $arguments["ContentType"] = "application/json; charset=utf-8"
    }

    try {
        $response = Invoke-WebRequest @arguments
    }
    catch {
        $status = $null
        $errorBody = $null
        if ($_.Exception.Response) {
            try { $status = [int]$_.Exception.Response.StatusCode } catch { }
            try {
                $reader = New-Object System.IO.StreamReader($_.Exception.Response.GetResponseStream())
                $errorBody = $reader.ReadToEnd()
                $reader.Dispose()
            }
            catch { }
        }
        throw "HTTP request failed: $Method $Url; status=$status; body=$errorBody"
    }

    if ($ExpectedStatus -notcontains [int]$response.StatusCode) {
        throw "Unexpected status for $Method ${Url}: $($response.StatusCode); expected $($ExpectedStatus -join ',')"
    }

    $data = $null
    if ($response.Content) {
        try { $data = $response.Content | ConvertFrom-Json } catch { }
    }
    [pscustomobject]@{
        Status = [int]$response.StatusCode
        Data = $data
        Raw = $response.Content
    }
}

function First-Value {
    param([object[]]$Values)
    foreach ($value in $Values) {
        if ($null -ne $value -and "$value".Length -gt 0) { return "$value" }
    }
    return $null
}

function Job-Status {
    param([object]$Data)
    if ($null -eq $Data) { return $null }
    return First-Value @($Data.job.status, $Data.status)
}

Write-Host "FrameFlow AI contract acceptance" -ForegroundColor Green
Write-Host "Target: $BaseUrl"
Write-Host "This script never reads or prints provider API keys."

Write-Step "Liveness"
$live = Invoke-ContractRequest -Method GET -Url "$BaseUrl/health/live"
Write-Host "live HTTP $($live.Status)"

Write-Step "Readiness (database and worker)"
$ready = Invoke-ContractRequest -Method GET -Url "$BaseUrl/health/ready"
Write-Host "ready HTTP $($ready.Status)"

Write-Step "Seed assets"
$assets = Invoke-ContractRequest -Method GET -Url "$ApiBase/assets"
$assetItems = @()
if ($null -ne $assets.Data.items) { $assetItems = @($assets.Data.items) }
elseif ($assets.Data -is [System.Array]) { $assetItems = @($assets.Data) }
if ($assetItems.Count -lt 10) {
    throw "Expected at least 10 active assets, got $($assetItems.Count)."
}
Write-Host "active assets: $($assetItems.Count)"

Write-Step "Read-only collection endpoints"
$projects = Invoke-ContractRequest -Method GET -Url "$ApiBase/projects"
$runs = Invoke-ContractRequest -Method GET -Url "$ApiBase/runs"
$audit = Invoke-ContractRequest -Method GET -Url "$ApiBase/audit"
Write-Host "projects/runs/audit HTTP: $($projects.Status)/$($runs.Status)/$($audit.Status)"

if ($SkipCreate) {
    Write-Host "`nPASS: read-only checks completed." -ForegroundColor Green
    exit 0
}

Write-Step "Create a unique text project"
$suffix = "{0}-{1}" -f (Get-Date -Format "yyyyMMdd-HHmmss"), ([guid]::NewGuid().ToString("N").Substring(0, 8))
$idempotencyKey = "acceptance-$suffix"
$payloadObject = [ordered]@{
    title = "验收脚本-$suffix"
    text = "清晨，我骑着自行车穿过城市，准时来到办公室。团队用数据看板梳理项目进度，把复杂任务拆成清晰步骤。午后，我们讨论怎样用绿色科技让工作更高效，也让生活更健康。"
}
$payload = $payloadObject | ConvertTo-Json -Compress
$headers = @{ "Idempotency-Key" = $idempotencyKey }
$created = Invoke-ContractRequest -Method POST -Url "$ApiBase/projects/text" -ExpectedStatus @(200, 202) -Body $payload -Headers $headers

$projectId = First-Value @($created.Data.project.id, $created.Data.project_id)
$jobId = First-Value @($created.Data.job.id, $created.Data.job_id)
if (-not $projectId -or -not $jobId) {
    throw "Create response must expose project.id and job.id (or direct project_id/job_id)."
}
Write-Host "created project=$projectId job=$jobId HTTP=$($created.Status)"

Write-Step "Replay the same idempotent request"
$replayed = Invoke-ContractRequest -Method POST -Url "$ApiBase/projects/text" -ExpectedStatus @(200, 202) -Body $payload -Headers $headers
$replayProjectId = First-Value @($replayed.Data.project.id, $replayed.Data.project_id)
$replayJobId = First-Value @($replayed.Data.job.id, $replayed.Data.job_id)
if ($replayProjectId -ne $projectId -or $replayJobId -ne $jobId) {
    throw "Idempotency replay returned a different project/job."
}
Write-Host "idempotency replay returned the original resources"

Write-Step "Wait for the durable job terminal state"
$deadline = (Get-Date).AddSeconds($TimeoutSeconds)
$lastStatus = $null
do {
    $jobResponse = Invoke-ContractRequest -Method GET -Url "$ApiBase/jobs/$jobId"
    $status = Job-Status $jobResponse.Data
    $stage = First-Value @($jobResponse.Data.job.stage, $jobResponse.Data.stage)
    $progress = First-Value @($jobResponse.Data.job.progress, $jobResponse.Data.progress)
    if ($status -ne $lastStatus) {
        Write-Host "job status=$status stage=$stage progress=$progress"
        $lastStatus = $status
    }
    if ($status -eq "succeeded") { break }
    if ($status -eq "failed" -or $status -eq "canceled") {
        $code = First-Value @($jobResponse.Data.job.error_code, $jobResponse.Data.error_code)
        $message = First-Value @($jobResponse.Data.job.error_message, $jobResponse.Data.error_message)
        throw "Job reached $status; code=$code; message=$message"
    }
    if ((Get-Date) -ge $deadline) {
        throw "Timed out after $TimeoutSeconds seconds waiting for job $jobId (last status=$status)."
    }
    Start-Sleep -Milliseconds 750
} while ($true)

Write-Step "Verify persisted project result"
$detail = Invoke-ContractRequest -Method GET -Url "$ApiBase/projects/$projectId"
$segments = @($detail.Data.segments)
if ($segments.Count -lt 1) { throw "Ready project has no persisted segments." }
foreach ($segment in $segments) {
    $recommendations = @($segment.recommendations)
    if ($recommendations.Count -lt 3) {
        throw "Segment $($segment.id) has fewer than three recommendations."
    }
    $assetIds = @($recommendations | ForEach-Object { $_.asset_id })
    if (($assetIds | Select-Object -Unique).Count -lt 3) {
        throw "Segment $($segment.id) recommendations are not three unique assets."
    }
    foreach ($recommendation in $recommendations) {
        if (-not $recommendation.explanation) {
            throw "Recommendation $($recommendation.id) has no explanation."
        }
    }
}
Write-Host "persisted segments: $($segments.Count); every segment has >=3 unique explainable candidates"

Write-Host "`nPASS: FrameFlow AI contract smoke completed." -ForegroundColor Green
Write-Host "Created demo project remains available for refresh/manual UI checks: $projectId"
