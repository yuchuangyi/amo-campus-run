# 登录 + 只读探针（自动对齐手机设备指纹）
#
# 用法：
#   pwsh .tools\login-and-probe.ps1                 # 自动读手机信息 → 提示输入手机号/密码 → 登录 → 探针
#   pwsh .tools\login-and-probe.ps1 -Mobile 138xxxxxxxx
#   pwsh .tools\login-and-probe.ps1 -DryRun
#
# 说明：
#   · 需要手机用 USB 连着并已授权调试；没接手机也能跑，只是设备指纹为随机值
#   · 密码通过环境变量传给子进程，不出现在命令行、不写日志
#   · --config-only 只读，不起跑、不产生跑单

param(
    [string]$Mobile = "",
    [string]$Version = "8.7.9",
    [string]$Adb = "",
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$verDir = Join-Path $projectRoot "com.huachenjie.shandong_school-main\$Version"
if (-not (Test-Path $verDir)) {
    Write-Host "[错误] 找不到版本目录 $verDir" -ForegroundColor Red
    exit 1
}

if (-not $Adb) {
    $cand = Join-Path $PSScriptRoot "platform-tools\adb.exe"
    if (Test-Path $cand) { $Adb = $cand }
}

function Run-Adb {
    param([string[]]$CmdArgs)
    if (-not $Adb -or -not (Test-Path $Adb)) { return "" }
    $tmp = [System.IO.Path]::GetTempFileName()
    Start-Process -FilePath $Adb -ArgumentList $CmdArgs -NoNewWindow -Wait `
        -RedirectStandardOutput $tmp -RedirectStandardError "$tmp.err" -ErrorAction SilentlyContinue | Out-Null
    $out = (Get-Content $tmp -Raw -ErrorAction SilentlyContinue)
    Remove-Item $tmp, "$tmp.err" -ErrorAction SilentlyContinue
    if ($null -eq $out) { return "" }
    return ($out -as [string]).Trim()
}

# ---------- 1. 从手机取设备指纹 ----------
$dev = Run-Adb @("devices")
$online = $dev -split "`n" | Where-Object { $_ -match "\sdevice$" }
if ($online) {
    $androidId = Run-Adb @("shell", "settings", "get", "secure", "android_id")
    $model = Run-Adb @("shell", "getprop", "ro.product.model")
    $brand = Run-Adb @("shell", "getprop", "ro.product.brand")
    $rel = Run-Adb @("shell", "getprop", "ro.build.version.release")
    if ($androidId -match "^[0-9a-f]{16}$") {
        $env:SHANDONG_DEVICE_ID = $androidId
    }
    if ($brand -and $model) { $env:SHANDONG_MODEL = "$brand|$model" }
    if ($rel -match "^\d+") { $env:SHANDONG_SYSVER = $rel }
    Write-Host "[设备] 已对齐手机指纹：$brand|$model  Android $rel  deviceId=$androidId" -ForegroundColor Green
} else {
    Write-Host "[设备] 没有检测到在线设备，使用随机指纹（可能被判为新设备）" -ForegroundColor Yellow
}

# ---------- 2. 账号 ----------
if (-not $Mobile) { $Mobile = Read-Host "登录手机号" }
if (-not $Mobile) { Write-Host "[错误] 手机号为空" -ForegroundColor Red; exit 1 }

if ($DryRun) {
    Write-Host "[DryRun] cd $verDir"
    Write-Host "[DryRun] SHANDONG_DEVICE_ID=$env:SHANDONG_DEVICE_ID"
    Write-Host "[DryRun] SHANDONG_MODEL=$env:SHANDONG_MODEL"
    Write-Host "[DryRun] SHANDONG_SYSVER=$env:SHANDONG_SYSVER"
    Write-Host "[DryRun] python login_cli.py -u $Mobile"
    Write-Host "[DryRun] python risk_probe.py --config-only"
    exit 0
}

$sec = Read-Host "登录密码" -AsSecureString
$plain = [pscredential]::new("u", $sec).GetNetworkCredential().Password
if (-not $plain) { Write-Host "[错误] 密码为空" -ForegroundColor Red; exit 1 }

Push-Location $verDir
try {
    $env:PYTHONIOENCODING = "utf-8"
    $env:SHANDONG_PASSWORD = $plain

    Write-Host "`n[步骤] 登录" -ForegroundColor Cyan
    python login_cli.py -u $Mobile
    $loginCode = $LASTEXITCODE

    $sessionFile = Join-Path $verDir "session.json"
    $token = ""
    if (Test-Path $sessionFile) {
        try { $token = (Get-Content $sessionFile -Raw -Encoding utf8 | ConvertFrom-Json).token } catch { }
    }
    if ($loginCode -ne 0 -or -not $token -or $token -match "YOUR_TOKEN") {
        Write-Host "[失败] 没拿到有效 token，探针跳过。" -ForegroundColor Yellow
        Write-Host "  常见原因：密码错、滑块没过、账号被限制登录、学校要求绑定设备。" -ForegroundColor Gray
        exit 2
    }

    Write-Host "`n[步骤] 只读探针（不起跑）" -ForegroundColor Cyan
    python risk_probe.py --config-only
    $probeCode = $LASTEXITCODE

    Write-Host "`n[完成] 探针退出码 $probeCode" -ForegroundColor Green
    if ($probeCode -eq 0) {
        Write-Host "  规则已读取。确认没人脸硬门槛后，再跑最小里程：" -ForegroundColor Gray
        Write-Host "  python risk_probe.py --lat <纬度> --lng <经度> -d 800 -p 420 --clean" -ForegroundColor Gray
    }
}
finally {
    Remove-Item Env:SHANDONG_PASSWORD -ErrorAction SilentlyContinue
    $plain = $null
    Pop-Location
}
