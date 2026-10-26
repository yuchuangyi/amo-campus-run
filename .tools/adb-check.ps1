# 安卓设备接入体检
#
# 用法：
#   pwsh .tools\adb-check.ps1                 # 体检
#   pwsh .tools\adb-check.ps1 -Pull           # 体检并把已安装的 base.apk 拉回本地
#   pwsh .tools\adb-check.ps1 -Adb D:\xxx\adb.exe
#
# 说明：所有 adb 调用都走临时文件重定向，避免 adb 守护进程占住管道导致脚本不退出。

param(
    [string]$Adb = "",
    [string]$Package = "com.huachenjie.shandong_school",
    [switch]$Pull,
    [string]$OutDir = "D:\downloads\com.huachenjie.shandong_school-main\.apk879"
)

$ErrorActionPreference = "Continue"
if (-not $Adb) {
    $cand = Join-Path $PSScriptRoot "platform-tools\adb.exe"
    if (Test-Path $cand) { $Adb = $cand }
}
if (-not $Adb -or -not (Test-Path $Adb)) {
    Write-Host "[错误] 找不到 adb.exe，请用 -Adb 指定路径" -ForegroundColor Red
    exit 1
}

$script:serial = ""

function AdbOut {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$AdbArgs)
    $tmp = [System.IO.Path]::GetTempFileName()
    $err = "$tmp.err"
    $full = @()
    if ($script:serial) { $full += @("-s", $script:serial) }
    $full += $AdbArgs
    Start-Process -FilePath $Adb -ArgumentList $full -NoNewWindow -Wait `
        -RedirectStandardOutput $tmp -RedirectStandardError $err -ErrorAction SilentlyContinue | Out-Null
    $out = @()
    if (Test-Path $tmp) { $out += (Get-Content $tmp -Raw -ErrorAction SilentlyContinue) }
    if (Test-Path $err) { $out += (Get-Content $err -Raw -ErrorAction SilentlyContinue) }
    Remove-Item $tmp, $err -ErrorAction SilentlyContinue
    return (($out -join "`n").Trim())
}

function Step($text) { Write-Host "`n[步骤] $text" -ForegroundColor Cyan }
function Info($k, $v) { Write-Host ("  {0,-16} {1}" -f $k, ($v -replace "\s+$", "")) }
function Bad($text) { Write-Host "  [问题] $text" -ForegroundColor Yellow }

Step "启动 adb 服务"
AdbOut start-server | Out-Null
Info "adb" (AdbOut version)

Step "探测设备"
$devices = AdbOut devices -l
$devLine = $devices -split "`n" | Where-Object { $_ -match "^\S+\s+(device|unauthorized|offline)" } | Select-Object -First 1

if (-not $devLine) {
    Write-Host "  未发现设备。" -ForegroundColor Yellow
    Write-Host @"

  手机端按这个顺序操作：

    1. 设置 → 关于手机 → 连续点「版本号」7 次，开启开发者选项
    2. 设置 → 系统 → 开发者选项（部分机型在「其他设置」里）
    3. 打开「USB 调试」
       · 小米/红米：还要开「USB 调试（安全设置）」，需登录小米账号
       · OPPO/一加/realme：还要关掉「禁止权限监控」
       · 华为/荣耀：要开「仅充电模式下允许 ADB 调试」
    4. 数据线连电脑，下拉通知栏把 USB 用途改成「传输文件(MTP)」
    5. 手机弹「允许 USB 调试吗」→ 勾「始终允许」→ 确定

  插好后重新跑：pwsh .tools\adb-check.ps1
"@ -ForegroundColor Gray
    exit 2
}

$parts = $devLine -split "\s+"
$script:serial = $parts[0]
$state = $parts[1]
Info "序列号" $script:serial
Info "状态" $state

if ($state -eq "unauthorized") {
    Bad "手机还没授权这台电脑。解锁手机，点弹窗里的「允许 USB 调试」（勾选始终允许）。"
    Write-Host "  没弹窗的话：拔插数据线，或 adb kill-server 后重跑本脚本。" -ForegroundColor Gray
    exit 3
}
if ($state -ne "device") {
    Bad "设备状态为 $state，先解锁手机屏幕再试。"
    exit 3
}

Step "设备信息"
Info "型号" (AdbOut shell getprop ro.product.model)
Info "品牌" (AdbOut shell getprop ro.product.brand)
Info "Android" (AdbOut shell getprop ro.build.version.release)
Info "SDK" (AdbOut shell getprop ro.build.version.sdk)
Info "ABI" (AdbOut shell getprop ro.product.cpu.abi)

Step "Root / 调试环境"
$su = AdbOut shell "su -c id"
if ($su -match "uid=0") {
    Info "root" "已 root（$su）"
} else {
    Info "root" "未 root"
    Write-Host "    未 root 只能拉 APK、跑协议；要 dump 加固后的 dex 需要 root + Frida/LSPosed。" -ForegroundColor Gray
}
Info "ro.debuggable" (AdbOut shell getprop ro.debuggable)
Info "SELinux" (AdbOut shell getenforce)
$magisk = AdbOut shell "which magisk"
if ($magisk -and $magisk -notmatch "not found") { Info "magisk" $magisk } else { Info "magisk" "未发现" }

Step "目标应用"
$pathLine = AdbOut shell "pm path $Package"
if (-not $pathLine -or $pathLine -match "Error|Unknown|not found") {
    Bad "设备上没有安装 $Package"
    exit 4
}
Info "安装路径" (($pathLine -replace "package:", "") -replace "`n", " ; ")

$dump = AdbOut shell "dumpsys package $Package"
$verName = ([regex]::Match($dump, "versionName=(\S+)")).Groups[1].Value
$verCode = ([regex]::Match($dump, "versionCode=(\d+)")).Groups[1].Value
Info "versionName" $verName
Info "versionCode" $verCode
if ($verCode -and $verCode -ne "26101910") {
    Bad "设备上是 versionCode=$verCode，不是 8.7.9（26101910）。要分析 8.7.9 请先更新应用。"
}

if ($Pull) {
    Step "拉取 base.apk"
    if (-not (Test-Path $OutDir)) { New-Item -ItemType Directory -Force -Path $OutDir | Out-Null }
    $paths = ($pathLine -split "`n") | ForEach-Object { ($_ -replace "package:", "").Trim() } | Where-Object { $_ }
    $remote = ($paths | Where-Object { $_ -match "base\.apk" } | Select-Object -First 1)
    if (-not $remote) { $remote = $paths[0] }
    $local = Join-Path $OutDir "device-base.apk"
    AdbOut pull $remote $local | ForEach-Object { "  $_" }
    if (Test-Path $local) {
        Info "本地文件" $local
        Info "MD5" (Get-FileHash $local -Algorithm MD5).Hash
        Info "大小" ("{0:N0} B" -f (Get-Item $local).Length)
        Write-Host "  对照：官方 8.7.9 包 MD5 = 520303F6F25BCBC3D5CCEB3DD9DF0A34" -ForegroundColor Gray
    }
}

Write-Host "`n[完成] 设备可用。" -ForegroundColor Green
Write-Host "  下一步：python 8.7.9\risk_probe.py --config-only      # 协议兼容性 + 学校规则" -ForegroundColor Gray
