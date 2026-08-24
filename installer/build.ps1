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
$VersionFile = Join-Path $ProjectRoot 'src\ziniao_automation\version.py'
$LockFile = Join-Path $ProjectRoot 'uv.lock'
$ToolchainFile = Join-Path $InstallerDir 'toolchain.json'
$UvPath = Join-Path $ProjectRoot 'uv.exe'
$InstallScriptFile = Join-Path $ProjectRoot 'Install.bat'

function Read-ReleaseVersion {
    if (-not (Test-Path -LiteralPath $VersionFile)) {
        throw '缺少 src\ziniao_automation\version.py，无法确定发布版本。'
    }
    $source = [IO.File]::ReadAllText($VersionFile)
    $matched = [regex]::Match(
        $source,
        '(?m)^__version__\s*=\s*["''](?<version>\d+\.\d+\.\d+)["'']\s*$'
    )
    if (-not $matched.Success) {
        throw 'version.py 里没有找到合法的 x.y.z __version__。'
    }
    return $matched.Groups['version'].Value
}

function Assert-CleanGitCheckout {
    if (-not (Get-Command 'git.exe' -ErrorAction SilentlyContinue)) {
        throw '发布构建需要 git.exe，以记录并核对准确的源码提交。'
    }
    Push-Location $ProjectRoot
    try {
        $inside = (& git rev-parse --is-inside-work-tree 2>$null).Trim()
        if ($LASTEXITCODE -ne 0 -or $inside -ne 'true') {
            throw '当前目录不是 Git 工作区，不能生成可追踪的发布包。'
        }
        $changes = @(& git status --porcelain=v1 --untracked-files=all)
        if ($LASTEXITCODE -ne 0) { throw 'git status 执行失败。' }
        if ($changes.Count -gt 0) {
            throw ("Git 工作区不是干净状态，发布构建已中止：`n" + ($changes -join "`n"))
        }
        $commit = (& git rev-parse HEAD).Trim().ToLowerInvariant()
        if ($LASTEXITCODE -ne 0 -or $commit -notmatch '^[0-9a-f]{40}$') {
            throw '无法读取完整 Git 提交号。'
        }
        return $commit
    } finally {
        Pop-Location
    }
}

function Write-Utf8NoBom([string]$Path, [string]$Content) {
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [IO.File]::WriteAllText($Path, $Content, $encoding)
}

function Assert-ChildPath([string]$Path, [string]$Parent) {
    $trimChars = [char[]]@(
        [IO.Path]::DirectorySeparatorChar,
        [IO.Path]::AltDirectorySeparatorChar
    )
    $fullParent = [IO.Path]::GetFullPath($Parent).TrimEnd($trimChars)
    $fullPath = [IO.Path]::GetFullPath($Path).TrimEnd($trimChars)
    $prefix = $fullParent + [IO.Path]::DirectorySeparatorChar
    if (-not $fullPath.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "拒绝操作预期目录之外的路径：$fullPath（预期位于 $fullParent 内）"
    }
    return $fullPath
}

function Remove-StageGeneratedCaches([string]$Root) {
    $safeRoot = Assert-ChildPath $Root $BuildDir
    foreach ($pattern in @('__pycache__', '*.egg-info')) {
        Get-ChildItem -LiteralPath $safeRoot -Recurse -Directory -Filter $pattern |
            Sort-Object -Property FullName -Descending |
            ForEach-Object { Remove-Item -LiteralPath $_.FullName -Recurse -Force }
    }
}

function Assert-StageSafe([string]$Root) {
    $safeRoot = Assert-ChildPath $Root $BuildDir
    foreach ($forbidden in @('data', '.venv', '.uv-python', '.uv-cache', 'tests')) {
        $leaked = Join-Path $safeRoot $forbidden
        if (Test-Path -LiteralPath $leaked) {
            throw "暂存目录里出现了 $forbidden，安装包可能包含运行数据，已中止。"
        }
    }
    # -Include with -LiteralPath does not filter reliably, so compare suffixes.
    $risky = @('.db', '.db-wal', '.db-shm', '.sqlite', '.jsonl', '.log', '.png', '.har', '.zip')
    $secrets = Get-ChildItem -LiteralPath $safeRoot -Recurse -File |
        Where-Object { $risky -contains $_.Extension.ToLowerInvariant() }
    if ($secrets) {
        throw "暂存目录里出现了数据库/日志/截图文件：$($secrets.FullName -join ', ')"
    }
}

function Read-ToolchainDefinition {
    if (-not (Test-Path -LiteralPath $ToolchainFile -PathType Leaf)) {
        throw '缺少 installer\toolchain.json，无法确定发布工具链。'
    }
    try {
        $definition = [IO.File]::ReadAllText($ToolchainFile) | ConvertFrom-Json
    } catch {
        throw "installer\toolchain.json 不是合法 JSON：$($_.Exception.Message)"
    }
    if ($definition.schema_version -ne 1) {
        throw 'installer\toolchain.json 的 schema_version 必须为 1。'
    }
    if ($definition.python_version -notmatch '^3\.12\.\d+$') {
        throw 'installer\toolchain.json 缺少合法的 Python 3.12.x 版本。'
    }
    if ($definition.uv.version -notmatch '^\d+\.\d+\.\d+$') {
        throw 'installer\toolchain.json 缺少合法的 uv 版本。'
    }
    $officialUvUrl = 'https://github.com/astral-sh/uv/releases/download/{0}/uv-x86_64-pc-windows-msvc.zip' -f `
        $definition.uv.version
    if ($definition.uv.url -cne $officialUvUrl) {
        throw 'installer\toolchain.json 的 uv.url 不是对应版本的官方 Windows x64 下载地址。'
    }
    if ($definition.uv.sha256 -notmatch '^[0-9A-Fa-f]{64}$') {
        throw 'installer\toolchain.json 缺少合法的 uv.exe SHA256。'
    }
    return $definition
}

function Assert-InstallScriptPythonVersion($Toolchain) {
    if (-not (Test-Path -LiteralPath $InstallScriptFile -PathType Leaf)) {
        throw '缺少 Install.bat，无法核对安装端 Python 版本。'
    }
    $source = [IO.File]::ReadAllText($InstallScriptFile)
    $matched = [regex]::Match(
        $source,
        '(?m)^set "PYTHON_VERSION=(?<version>\d+\.\d+\.\d+)"\s*$'
    )
    if (-not $matched.Success) {
        throw 'Install.bat 里没有找到合法的 PYTHON_VERSION=x.y.z。'
    }
    $installVersion = $matched.Groups['version'].Value
    if ($installVersion -cne [string]$Toolchain.python_version) {
        throw ("Python 版本不一致：installer\toolchain.json={0}，Install.bat={1}。" -f `
            $Toolchain.python_version, $installVersion)
    }
}

function Assert-UvHash([string]$Path, [string]$ExpectedHash) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "缺少待校验的 uv.exe：$Path"
    }
    $actualHash = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToUpperInvariant()
    if ($actualHash -cne $ExpectedHash.ToUpperInvariant()) {
        throw "uv.exe SHA256 不匹配。预期 $ExpectedHash，实际 $actualHash。"
    }
}

function Ensure-VerifiedUv($Toolchain) {
    $expectedHash = [string]$Toolchain.uv.sha256
    if (Test-Path -LiteralPath $UvPath -PathType Leaf) {
        Assert-UvHash $UvPath $expectedHash
        return
    }

    $downloadDir = Assert-ChildPath (Join-Path $BuildDir 'toolchain-download') $BuildDir
    $archivePath = Join-Path $downloadDir ("uv-{0}.zip" -f $Toolchain.uv.version)
    $extractDir = Join-Path $downloadDir ("uv-{0}-extracted" -f $Toolchain.uv.version)
    if (Test-Path -LiteralPath $downloadDir) {
        Remove-Item -LiteralPath $downloadDir -Recurse -Force
    }
    New-Item -ItemType Directory -Path $downloadDir -Force | Out-Null

    Write-Host ("本地缺少 uv.exe，正在下载固定版本 uv {0}..." -f $Toolchain.uv.version)
    Invoke-WebRequest -Uri ([string]$Toolchain.uv.url) -OutFile $archivePath -UseBasicParsing
    New-Item -ItemType Directory -Path $extractDir -Force | Out-Null
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [IO.Compression.ZipFile]::OpenRead($archivePath)
    try {
        $uvEntries = @($archive.Entries | Where-Object { $_.FullName -ceq 'uv.exe' })
        if ($uvEntries.Count -ne 1) {
            throw '固定 uv 压缩包必须且只能包含一个顶层 uv.exe。'
        }
        $downloadedUv = Join-Path $extractDir 'uv.exe'
        [IO.Compression.ZipFileExtensions]::ExtractToFile($uvEntries[0], $downloadedUv, $false)
    } finally {
        $archive.Dispose()
    }
    Assert-UvHash $downloadedUv $expectedHash
    Copy-Item -LiteralPath $downloadedUv -Destination $UvPath
    Assert-UvHash $UvPath $expectedHash
}

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

$Version = Read-ReleaseVersion
$Commit = Assert-CleanGitCheckout
$BuiltAtUtc = [DateTime]::UtcNow.ToString('o')
$Toolchain = Read-ToolchainDefinition
Assert-InstallScriptPythonVersion $Toolchain

if (-not (Test-Path -LiteralPath $LockFile)) {
    throw '缺少 uv.lock。先运行 uv lock 并提交锁文件，再构建发布包。'
}
Ensure-VerifiedUv $Toolchain
$uv = $UvPath
$oldCache = $env:UV_CACHE_DIR
$env:UV_CACHE_DIR = Join-Path $ProjectRoot '.uv-cache'
try {
    & $uv lock --check
    if ($LASTEXITCODE -ne 0) { throw 'uv.lock 与 pyproject.toml 不一致。请先运行 uv lock。' }
} finally {
    $env:UV_CACHE_DIR = $oldCache
}

# 白名单：只有这些进安装包。
$Include = @(
    'src',
    'migrations',
    'installer\app.ico',
    'installer\toolchain.json',
    'pyproject.toml',
    'uv.lock',
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

Write-Host ("发布版本 {0}，Git {1}" -f $Version, $Commit.Substring(0, 12))
Write-Host '[1/7] 清理暂存目录...'
$StageDir = Assert-ChildPath $StageDir $BuildDir
if (Test-Path -LiteralPath $StageDir) { Remove-Item -LiteralPath $StageDir -Recurse -Force }
New-Item -ItemType Directory -Path $StageDir -Force | Out-Null

Write-Host '[2/7] 收集要分发的文件...'
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

# 安装后的诊断页靠这份文件显示准确构建提交；安装包不携带 .git。
$buildInfo = [ordered]@{
    version = $Version
    commit = $Commit
    built_at_utc = $BuiltAtUtc
}
Write-Utf8NoBom (Join-Path $StageDir 'build-info.json') `
    (($buildInfo | ConvertTo-Json -Depth 3) + "`n")

# __pycache__ 只会让包变大；*.egg-info 是可编辑安装的产物，装的时候会重新生成，
# 带上旧的反而可能和新版本对不上。
Remove-StageGeneratedCaches $StageDir

# 兜底断言：这几样绝不能出现在暂存目录里。白名单已经保证了，但这份数据
# 一旦泄漏就无法收回，值得再确认一次。
Assert-StageSafe $StageDir

$stageSize = (Get-ChildItem -LiteralPath $StageDir -Recurse -File | Measure-Object -Property Length -Sum).Sum
Write-Host ("      共 {0:N0} 个文件，{1:N1} MB" -f `
    (Get-ChildItem -LiteralPath $StageDir -Recurse -File).Count, ($stageSize / 1MB))

Write-Host '[3/7] 使用暂存源码运行完整测试...'
$releaseTestVenv = Assert-ChildPath (Join-Path $BuildDir 'release-test-venv') $BuildDir
$releaseTestTemp = Assert-ChildPath (Join-Path $BuildDir 'release-test-tmp') $BuildDir
$releaseTestCache = Assert-ChildPath (Join-Path $BuildDir 'release-test-cache') $BuildDir
if (Test-Path -LiteralPath $releaseTestVenv) {
    Remove-Item -LiteralPath $releaseTestVenv -Recurse -Force
}
foreach ($testDirectory in @($releaseTestTemp, $releaseTestCache)) {
    if (Test-Path -LiteralPath $testDirectory) {
        Remove-Item -LiteralPath $testDirectory -Recurse -Force
    }
    New-Item -ItemType Directory -Path $testDirectory -Force | Out-Null
}
$oldPythonPath = $env:PYTHONPATH
$oldProjectEnvironment = $env:UV_PROJECT_ENVIRONMENT
$oldPythonInstallDir = $env:UV_PYTHON_INSTALL_DIR
$oldPythonPreference = $env:UV_PYTHON_PREFERENCE
$oldTestCache = $env:UV_CACHE_DIR
$oldStageSource = $env:ZINIAO_RELEASE_STAGE_SRC
$oldTemp = $env:TEMP
$oldTmp = $env:TMP
$env:UV_PROJECT_ENVIRONMENT = $releaseTestVenv
$env:UV_PYTHON_INSTALL_DIR = Join-Path $ProjectRoot '.uv-python'
$env:UV_PYTHON_PREFERENCE = 'only-managed'
$env:UV_CACHE_DIR = Join-Path $ProjectRoot '.uv-cache'
$env:TEMP = $releaseTestTemp
$env:TMP = $releaseTestTemp
Push-Location $ProjectRoot
try {
    & $uv python install ([string]$Toolchain.python_version)
    if ($LASTEXITCODE -ne 0) {
        throw "固定 Python $($Toolchain.python_version) 准备失败，退出码 $LASTEXITCODE。"
    }
    & $uv sync --locked --extra dev --no-install-project `
        --python ([string]$Toolchain.python_version)
    if ($LASTEXITCODE -ne 0) {
        throw "独立发布测试环境同步失败，退出码 $LASTEXITCODE。"
    }
    $testPython = Join-Path $releaseTestVenv 'Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $testPython -PathType Leaf)) {
        throw '独立发布测试环境缺少 python.exe。'
    }
    & $testPython -c `
        "import sys; assert '.'.join(map(str, sys.version_info[:3])) == '$($Toolchain.python_version)'"
    if ($LASTEXITCODE -ne 0) {
        throw "独立发布测试环境不是 Python $($Toolchain.python_version)。"
    }
    $env:PYTHONPATH = Join-Path $StageDir 'src'
    $env:ZINIAO_RELEASE_STAGE_SRC = $env:PYTHONPATH
    & $testPython -c `
        "import os; from pathlib import Path; import ziniao_automation; expected=Path(os.environ['ZINIAO_RELEASE_STAGE_SRC']).resolve(); actual=Path(ziniao_automation.__file__).resolve(); assert actual.is_relative_to(expected), (actual, expected)"
    if ($LASTEXITCODE -ne 0) {
        throw '独立发布测试环境没有从 build\stage\src 导入业务代码。'
    }
    & $testPython -m pytest (Join-Path $ProjectRoot 'tests') `
        --basetemp $releaseTestTemp -o ("cache_dir={0}" -f $releaseTestCache)
    if ($LASTEXITCODE -ne 0) { throw "暂存源码完整测试失败，退出码 $LASTEXITCODE。" }
} finally {
    Pop-Location
    $env:PYTHONPATH = $oldPythonPath
    $env:UV_PROJECT_ENVIRONMENT = $oldProjectEnvironment
    $env:UV_PYTHON_INSTALL_DIR = $oldPythonInstallDir
    $env:UV_PYTHON_PREFERENCE = $oldPythonPreference
    $env:UV_CACHE_DIR = $oldTestCache
    $env:ZINIAO_RELEASE_STAGE_SRC = $oldStageSource
    $env:TEMP = $oldTemp
    $env:TMP = $oldTmp
}
# Importing the staged tree during pytest recreates bytecode caches.  Purge
# them a second time so the files tested are the files packaged, without local
# interpreter artefacts mixed into the installer.
Remove-StageGeneratedCaches $StageDir
$testCreatedData = Join-Path $StageDir 'data'
if (Test-Path -LiteralPath $testCreatedData) {
    $testDataEntries = @(Get-ChildItem -LiteralPath $testCreatedData -Force -Recurse)
    if ($testDataEntries.Count -gt 0) {
        throw "暂存源码测试在 stage\data 中生成了运行数据，安装包构建已中止。"
    }
    Remove-Item -LiteralPath $testCreatedData -Recurse -Force
}
Assert-StageSafe $StageDir

Write-Host '[4/7] 归档旧的 0.2.1 测试包...'
$legacyPackage = Join-Path $BuildDir 'ZiniaoAutomation-Setup-0.2.1.exe'
if (Test-Path -LiteralPath $legacyPackage) {
    $archiveDir = Join-Path $BuildDir 'archive'
    New-Item -ItemType Directory -Path $archiveDir -Force | Out-Null
    $archiveName = Split-Path -Leaf $legacyPackage
    $archiveTarget = Join-Path $archiveDir $archiveName
    if (Test-Path -LiteralPath $archiveTarget) {
        $archiveTarget = Join-Path $archiveDir `
            ("ZiniaoAutomation-Setup-0.2.1-{0}.exe" -f [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ'))
    }
    Move-Item -LiteralPath $legacyPackage -Destination $archiveTarget
    Write-Host ("      已保留到 {0}" -f $archiveTarget)
} else {
    Write-Host '      没有发现旧包，无需归档。'
}

Write-Host '[5/7] 编译安装包...'
$iscc = Find-ISCC
$script = Join-Path $InstallerDir 'ziniao-automation.iss'
& $iscc ("/DAppVersion={0}" -f $Version) $script | ForEach-Object {
    if ($_ -match 'error|Error|错误') { Write-Host $_ -ForegroundColor Red } else { Write-Verbose $_ }
}
if ($LASTEXITCODE -ne 0) { throw "ISCC 编译失败，退出码 $LASTEXITCODE" }

$outputPath = Join-Path $BuildDir ("ZiniaoAutomation-Setup-{0}.exe" -f $Version)
if (-not (Test-Path -LiteralPath $outputPath)) { throw '编译没有产出预期的 .exe' }
$output = Get-Item -LiteralPath $outputPath

if ($CertificatePath) {
    Write-Host '[6/7] 代码签名...'
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
    Write-Host '[6/7] 跳过签名（未提供证书）。'
    Write-Host '      未签名的安装包在 Win10 上会弹 SmartScreen“Windows 已保护你的电脑”，' -ForegroundColor Yellow
    Write-Host '      需要点“更多信息”→“仍要运行”。请在分发说明里写清楚这一步。' -ForegroundColor Yellow
}

Write-Host '[7/7] 生成校验文件与发布清单...'
$artifactHash = (Get-FileHash -LiteralPath $output.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
$lockHash = (Get-FileHash -LiteralPath $LockFile -Algorithm SHA256).Hash.ToLowerInvariant()
$checksumPath = Join-Path $BuildDir ("ZiniaoAutomation-Setup-{0}.sha256" -f $Version)
Write-Utf8NoBom $checksumPath ("{0}  {1}`n" -f $artifactHash, $output.Name)
$manifest = [ordered]@{
    schema_version = 1
    version = $Version
    commit = $Commit
    built_at_utc = $BuiltAtUtc
    artifact = $output.Name
    sha256 = $artifactHash
    dependency_lock = 'uv.lock'
    dependency_lock_sha256 = $lockHash
    toolchain = [ordered]@{
        definition = 'installer/toolchain.json'
        definition_sha256 = (Get-FileHash -LiteralPath $ToolchainFile -Algorithm SHA256).Hash.ToLowerInvariant()
        python_version = [string]$Toolchain.python_version
        uv = [ordered]@{
            version = [string]$Toolchain.uv.version
            url = [string]$Toolchain.uv.url
            sha256 = ([string]$Toolchain.uv.sha256).ToLowerInvariant()
        }
    }
}
$manifestPath = Join-Path $BuildDir ("release-{0}.json" -f $Version)
Write-Utf8NoBom $manifestPath (($manifest | ConvertTo-Json -Depth 4) + "`n")

Write-Host ''
Write-Host ("[完成] {0}  ({1:N1} MB)" -f $output.FullName, ($output.Length / 1MB)) -ForegroundColor Green
Write-Host ("       SHA256: {0}" -f $checksumPath)
Write-Host ("       清单:   {0}" -f $manifestPath)
