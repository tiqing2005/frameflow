[CmdletBinding()]
param(
    [string]$OutputPath = (Join-Path $PSScriptRoot "oversize-101mb.bin"),
    [int]$SizeMb = 101
)

$ErrorActionPreference = "Stop"

if ($SizeMb -le 100) {
    throw "SizeMb 必须大于 100，才能验证默认 100 MB 上传上限。"
}

$resolvedDirectory = [System.IO.Path]::GetFullPath((Split-Path -Parent $OutputPath))
$allowedDirectory = [System.IO.Path]::GetFullPath($PSScriptRoot)
if (-not $resolvedDirectory.StartsWith($allowedDirectory, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "为避免误写大文件，OutputPath 必须位于脚本目录内：$allowedDirectory"
}

$stream = [System.IO.File]::Open($OutputPath, [System.IO.FileMode]::Create, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
try {
    $stream.SetLength([int64]$SizeMb * 1MB)
}
finally {
    $stream.Dispose()
}

$file = Get-Item -LiteralPath $OutputPath
Write-Host "已生成超限测试文件：$($file.FullName)"
Write-Host ("大小：{0:N2} MB" -f ($file.Length / 1MB))
Write-Host "仅在隔离环境测试，使用后请手动删除。"
