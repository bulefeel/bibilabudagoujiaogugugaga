# 构建 Windows 安装包。
#
#   powershell -ExecutionPolicy Bypass -File installer\build.ps1
#
# 分两步：先把要分发的文件收进 build\stage（白名单，不是黑名单——黑名单迟早
# 会漏掉某个新目录，而这个仓库里 data\ 装的是真实资金记录和 Seller Central
# 截图，漏一次就是把客户数据打进了安装包），再交给 ISCC 编译。

[CmdletBinding()]
param(
    # 买了代码签名证书之后，把 .pfx 路径和密码传进来即可，脚本其余部分不用改。
    [string]$SignToolPath,
    [string]$CertificatePath,
    [string]$CertificatePassword
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$InstallerDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $InstallerDir
$BuildDir = Join-Path $ProjectRoot 'build'
$StageDir = Join-Path $BuildDir 'stage'

function Find-ISCC {
    # winget 的 Inno Setup 默认装到 %LOCALAPPDATA%\Programs（免管理员），
    # 官网安装包则装到 Program Files。两处都找。
    $candidates = @(
        "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
    )
    foreach ($path in $candidates) {
        if (Test-Path -LiteralPath $path) { return $path }
    }
    $onPath = Get-Command 'ISCC.exe' -ErrorAction SilentlyContinue
    if ($onPath) { return $onPath.Source }
    throw "找不到 Inno Setup 的 ISCC.exe。请先安装：winget install --id JRSoftware.InnoSetup"
}

# 白名单：只有这些进安装包。
$Include = @(
    'src',
    'migrations',
    'installer\app.ico',
    'pyproject.toml',
    'alembic.ini',
    'uv.exe',
    'Install.bat',
    'Start.bat',
    'Stop.bat',
    'Register-Startup.bat',
    'Configure-Feishu.bat',
    'Ziniao-WebDriver.bat',
    'README.md',
    'README-ZINIAO.md',
    'WORKFLOW.md'
)

Write-Host '[1/4] 清理暂存目录...'
if (Test-Path -LiteralPath $StageDir) { Remove-Item -LiteralPath $StageDir -Recurse -Force }
New-Item -ItemType Directory -Path $StageDir -Force | Out-Null

Write-Host '[2/4] 收集要分发的文件...'
foreach ($item in $Include) {
    $source = Join-Path $ProjectRoot $item
    if (-not (Test-Path -LiteralPath $source)) {
        throw "缺少 $item —— 安装包不完整，已中止。"
    }
    $destination = Join-Path $StageDir $item
    $parent = Split-Path -Parent $destination
    if (-not (Test-Path -LiteralPath $parent)) {
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }
    if ((Get-Item -LiteralPath $source) -is [System.IO.DirectoryInfo]) {
        Copy-Item -LiteralPath $source -Destination $destination -Recurse -Force
    } else {
        Copy-Item -LiteralPath $source -Destination $destination -Force
    }
}

# __pycache__ 只会让包变大；*.egg-info 是可编辑安装的产物，装的时候会重新生成，
# 带上旧的反而可能和新版本对不上。
foreach ($pattern in @('__pycache__', '*.egg-info')) {
    Get-ChildItem -LiteralPath $StageDir -Recurse -Directory -Filter $pattern |
        Sort-Object -Property FullName -Descending |
        ForEach-Object { Remove-Item -LiteralPath $_.FullName -Recurse -Force }
}

# 兜底断言：这几样绝不能出现在暂存目录里。白名单已经保证了，但这份数据
# 一旦泄漏就无法收回，值得再确认一次。
foreach ($forbidden in @('data', '.venv', '.uv-python', '.uv-cache', 'tests')) {
    $leaked = Join-Path $StageDir $forbidden
    if (Test-Path -LiteralPath $leaked) {
        throw "暂存目录里出现了 $forbidden，安装包可能包含运行数据，已中止。"
    }
}
# 注意：-Include 配 -LiteralPath 不按预期过滤（会匹配全部），必须自己筛扩展名。
$risky = @('.db', '.db-wal', '.db-shm', '.sqlite', '.jsonl', '.log', '.png', '.har', '.zip')
$secrets = Get-ChildItem -LiteralPath $StageDir -Recurse -File |
    Where-Object { $risky -contains $_.Extension.ToLowerInvariant() }
if ($secrets) {
    throw "暂存目录里出现了数据库/日志/截图文件：$($secrets.FullName -join ', ')"
}

$stageSize = (Get-ChildItem -LiteralPath $StageDir -Recurse -File | Measure-Object -Property Length -Sum).Sum
Write-Host ("      共 {0:N0} 个文件，{1:N1} MB" -f `
    (Get-ChildItem -LiteralPath $StageDir -Recurse -File).Count, ($stageSize / 1MB))

Write-Host '[3/4] 编译安装包...'
$iscc = Find-ISCC
$script = Join-Path $InstallerDir 'ziniao-automation.iss'
& $iscc $script | ForEach-Object {
    if ($_ -match 'error|Error|错误') { Write-Host $_ -ForegroundColor Red } else { Write-Verbose $_ }
}
if ($LASTEXITCODE -ne 0) { throw "ISCC 编译失败，退出码 $LASTEXITCODE" }

$output = Get-ChildItem -LiteralPath $BuildDir -Filter '*.exe' |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (-not $output) { throw '编译没有产出 .exe' }

if ($CertificatePath) {
    Write-Host '[4/4] 代码签名...'
    if (-not $SignToolPath) {
        $found = Get-ChildItem "${env:ProgramFiles(x86)}\Windows Kits\10\bin" -Recurse -Filter 'signtool.exe' -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -match 'x64' } | Select-Object -First 1
        if (-not $found) { throw '找不到 signtool.exe，请用 -SignToolPath 指定。' }
        $SignToolPath = $found.FullName
    }
    & $SignToolPath sign /f $CertificatePath /p $CertificatePassword `
        /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 $output.FullName
    if ($LASTEXITCODE -ne 0) { throw "签名失败，退出码 $LASTEXITCODE" }
} else {
    Write-Host '[4/4] 跳过签名（未提供证书）。'
    Write-Host '      未签名的安装包在 Win10 上会弹 SmartScreen“Windows 已保护你的电脑”，' -ForegroundColor Yellow
    Write-Host '      需要点“更多信息”→“仍要运行”。请在分发说明里写清楚这一步。' -ForegroundColor Yellow
}

Write-Host ''
Write-Host ("[完成] {0}  ({1:N1} MB)" -f $output.FullName, ($output.Length / 1MB)) -ForegroundColor Green
