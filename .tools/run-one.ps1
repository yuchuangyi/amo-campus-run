# 跑一单（随机里程/配速/种子，避免每天参数完全一样）
#
# 用法：
#   pwsh .tools\run-one.ps1                 # 默认 3000 m（单日上限），配速随机 330~380
#   pwsh .tools\run-one.ps1 -RandomDistance # 里程也随机 2100~2900
#   pwsh .tools\run-one.ps1 -Distance 3000 -Pace 340
#   pwsh .tools\run-one.ps1 -DryRun         # 只打印命令，不真正提交
#
# 不需要手机、不需要 USB、不需要开发者模式。token 存在 8.7.9\session.json。

param(
    [int]$Distance = 0,
    [int]$Pace = 0,
    [int]$Seed = 0,
    [string]$Version = "8.7.9",
    [string]$FenceCode = "",
    [switch]$RandomDistance,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$verDir = Join-Path $projectRoot "com.huachenjie.shandong_school-main\$Version"
if (-not (Test-Path $verDir)) {
    Write-Host "[错误] 找不到版本目录 $verDir" -ForegroundColor Red
    exit 1
}

# 里程默认顶格 3000（单日上限），配速仍随机；要变化就加 -RandomDistance
if (-not $Distance) {
    if ($RandomDistance) { $Distance = Get-Random -Minimum 2100 -Maximum 2950 }
    else { $Distance = 3000 }
}
if (-not $Pace) { $Pace = Get-Random -Minimum 330 -Maximum 380 }
if (-not $Seed) { $Seed = Get-Random -Minimum 1 -Maximum 999999 }

$sessionFile = Join-Path $verDir "session.json"
if (-not (Test-Path $sessionFile)) {
    Write-Host "[错误] 没有 session.json，先抓一次 token" -ForegroundColor Red
    exit 1
}
$token = (Get-Content $sessionFile -Raw -Encoding utf8 | ConvertFrom-Json).token
if (-not $token -or $token -match "YOUR_TOKEN") {
    Write-Host "[错误] session.json 里没有有效 token" -ForegroundColor Red
    exit 1
}

Write-Host ("[参数] 里程 {0}m  配速 {1}s/km  种子 {2}" -f $Distance, $Pace, $Seed) -ForegroundColor Cyan

$pyArgs = @(
    "run_cli.py",
    "-d", "$Distance",
    "-p", "$Pace",
    "--seed", "$Seed",
    "--verify"
)
if ($FenceCode) { $pyArgs += @("--fence-code", $FenceCode) }

if ($DryRun) {
    Write-Host ("[DryRun] cd {0}" -f $verDir)
    Write-Host ("[DryRun] python {0}" -f ($pyArgs -join " "))
    exit 0
}

Push-Location $verDir
try {
    $env:PYTHONIOENCODING = "utf-8"
    python @pyArgs
    $code = $LASTEXITCODE
}
finally {
    Pop-Location
}

Write-Host ""
if ($code -eq 0) {
    Write-Host "[完成] 这一单已提交，判定见上面的「跑单回查」。" -ForegroundColor Green
} else {
    Write-Host "[失败] 退出码 $code，把上面的输出发我。" -ForegroundColor Yellow
}
exit $code
