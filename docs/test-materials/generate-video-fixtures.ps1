[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$AudioDir = Join-Path $ProjectRoot "tmp\test-video-audio"
$OutputDir = Join-Path $PSScriptRoot "videos"
$Python = Join-Path $ProjectRoot "backend\.venv\Scripts\python.exe"
$Generator = Join-Path $PSScriptRoot "generate_test_videos.py"
$ScriptFile = Join-Path $PSScriptRoot "video-fixture-scripts.json"

New-Item -ItemType Directory -Force -Path $AudioDir, $OutputDir | Out-Null
Add-Type -AssemblyName System.Speech

function Write-SpeechWave {
    param(
        [string]$Name,
        [string]$Text,
        [int]$Rate = 0
    )
    $path = Join-Path $AudioDir $Name
    $speaker = New-Object System.Speech.Synthesis.SpeechSynthesizer
    try {
        $speaker.SelectVoice("Microsoft Huihui Desktop")
        $speaker.Rate = $Rate
        $speaker.Volume = 100
        $speaker.SetOutputToWaveFile($path)
        $speaker.Speak($Text)
    }
    finally {
        $speaker.Dispose()
    }
}

$Scripts = Get-Content -Raw -Encoding UTF8 -LiteralPath $ScriptFile | ConvertFrom-Json

Write-SpeechWave -Name "standard.wav" -Text $Scripts.standard

Write-SpeechWave -Name "terms.wav" -Rate -1 -Text $Scripts.terms

Write-SpeechWave -Name "noisy.wav" -Text $Scripts.noisy

Write-SpeechWave -Name "portrait.wav" -Rate -1 -Text $Scripts.portrait

& $Python $Generator --audio-dir $AudioDir --output-dir $OutputDir
if ($LASTEXITCODE -ne 0) {
    throw "Test video generation failed."
}

Write-Host "Test videos created in $OutputDir" -ForegroundColor Green
