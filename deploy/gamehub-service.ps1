#Requires -Version 7.0
<#
.SYNOPSIS
使用已准备的 NSSM 包装器安装或切换 GameHub Windows 服务，默认只预览。
.DESCRIPTION
版本目录应来自已验证的安装包。执行前由部署系统注入密钥环境，脚本不下载程序、不迁移数据库。
切换不会自动退回旧程序；涉及结构升级时须按备份恢复方案处理。
#>
[CmdletBinding()]
param(
    [ValidateSet('Install', 'Switch')][string]$Action = 'Switch',
    [Parameter(Mandatory)][string]$DeploymentRoot,
    [Parameter(Mandatory)][string]$VersionDirectory,
    [Parameter(Mandatory)][string]$ConfigPath,
    [Parameter(Mandatory)][string]$NssmPath,
    [ValidatePattern('^[A-Za-z][A-Za-z0-9_-]{0,63}$')][string]$ServiceName = 'GameHub',
    [ValidateSet('NT AUTHORITY\LocalService', 'NT AUTHORITY\NetworkService')]
    [string]$ServiceAccount = 'NT AUTHORITY\LocalService',
    [ValidateRange(1, 60)][int]$ReadyTimeoutSeconds = 30,
    [switch]$Execute
)
$ErrorActionPreference = 'Stop'

function Get-CheckedPath {
    param([string]$Value, [bool]$Directory)
    if (-not [IO.Path]::IsPathFullyQualified($Value) -or $Value.Contains("`n") -or $Value.Contains("`r") -or $Value.Contains('"')) {
        throw '路径必须为明确的绝对路径，不能包含引号或换行。'
    }
    $resolved = (Resolve-Path -LiteralPath $Value -ErrorAction Stop).ProviderPath
    $item = Get-Item -LiteralPath $resolved
    if ([bool]$item.PSIsContainer -ne $Directory) { throw '文件与目录类型不符合要求。' }
    # 拒绝重解析点，版本路径不能通过目录联接绕出部署目录。
    for ($cursor = $item; $null -ne $cursor;) {
        if ($cursor.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw '部署路径不能包含目录联接或符号链接。' }
        $cursor = if ($cursor -is [IO.FileInfo]) { $cursor.Directory } else { $cursor.Parent }
    }
    return $resolved
}

$rootPath = Get-CheckedPath $DeploymentRoot $true
$versionPath = Get-CheckedPath $VersionDirectory $true
$configFile = Get-CheckedPath $ConfigPath $false
$wrapperFile = Get-CheckedPath $NssmPath $false
$prefix = $rootPath.TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
if (-not $versionPath.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw '版本目录必须位于指定的部署根目录内。'
}
$binaryFile = Get-CheckedPath (Join-Path $versionPath 'bin/gamehub.exe') $false
if ($wrapperFile -eq $binaryFile -or $configFile -eq $binaryFile) { throw '程序、配置和服务包装器不能是同一文件。' }
$config = Get-Content -LiteralPath $configFile -Raw -Encoding utf8 | ConvertFrom-Json
$listenAddress = $null
if (-not [Net.IPAddress]::TryParse([string]$config.server.listen, [ref]$listenAddress) -or
    $listenAddress.Equals([Net.IPAddress]::Any) -or $listenAddress.Equals([Net.IPAddress]::IPv6Any)) {
    throw '配置必须使用明确的监听 IP。'
}
$port = [int]$config.server.port
if ($port -lt 1 -or $port -gt 65535) { throw '监听端口无效。' }
$hostPart = if ($listenAddress.AddressFamily -eq [Net.Sockets.AddressFamily]::InterNetworkV6) { '[' + $listenAddress + ']' } else { [string]$listenAddress }
$readyUrl = 'http://' + $hostPart + ':' + $port + '/admin/ready'
$parameters = '--config "' + $configFile + '"'
$steps = @(
    '检查版本配置：' + $binaryFile,
    '目标服务：' + $ServiceName + '；操作：' + $Action,
    '目标版本：' + $versionPath,
    '使用已准备的 NSSM：' + $wrapperFile,
    '服务命令参数：' + $parameters,
    '启动后就绪检查：' + $readyUrl
)
$steps | Write-Output
if (-not $Execute) { Write-Output '当前仅预览。添加 -Execute 才会修改或启停 Windows 服务。'; return }
if (-not $IsWindows) { throw '只能在 Windows 上管理服务。' }
& $binaryFile --config $configFile --check-config
if ($LASTEXITCODE -ne 0) { throw '目标版本配置检查失败，尚未修改服务。' }
$existingService = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($Action -eq 'Install' -and $null -ne $existingService) { throw '服务已存在，请使用 Switch。' }
if ($Action -eq 'Switch' -and $null -eq $existingService) { throw '服务不存在，请先使用 Install。' }
if ($Action -eq 'Switch') {
    $definition = Get-CimInstance -ClassName Win32_Service -Filter ("Name='" + $ServiceName + "'")
    if ($definition.PathName.Trim().Trim('"') -ne $wrapperFile) {
        throw '现有服务未使用指定 NSSM 包装器，已停止操作。'
    }
}
$logPath = Join-Path $rootPath 'logs'
if (-not (Test-Path -LiteralPath $logPath)) { [void][IO.Directory]::CreateDirectory($logPath) }
$logPath = Get-CheckedPath $logPath $true
function Invoke-Nssm {
    param([string[]]$Arguments)
    & $wrapperFile @Arguments
    if ($LASTEXITCODE -ne 0) { throw 'NSSM 操作失败，请检查服务状态与已应用配置。' }
}
if ($Action -eq 'Install') {
    Invoke-Nssm -Arguments @('install', $ServiceName, $binaryFile)
    Invoke-Nssm -Arguments @('set', $ServiceName, 'ObjectName', $ServiceAccount)
}
# NSSM 按毫秒等待 Control-C；至少容纳 GameHub 的 20 秒受控退出预算。
# 参数语义见 https://nssm.cc/usage 的 Shutdown 章节。切换时须在停止旧进程前设置。
Invoke-Nssm -Arguments @('set', $ServiceName, 'AppStopMethodConsole', '25000')
if ($Action -eq 'Switch' -and $existingService.Status -ne 'Stopped') { Invoke-Nssm -Arguments @('stop', $ServiceName) }
Invoke-Nssm -Arguments @('set', $ServiceName, 'Application', $binaryFile)
Invoke-Nssm -Arguments @('set', $ServiceName, 'AppDirectory', $versionPath)
Invoke-Nssm -Arguments @('set', $ServiceName, 'AppParameters', $parameters)
Invoke-Nssm -Arguments @('set', $ServiceName, 'AppStdout', (Join-Path $logPath ($ServiceName + '.log')))
Invoke-Nssm -Arguments @('set', $ServiceName, 'AppStderr', (Join-Path $logPath ($ServiceName + '.log')))
Invoke-Nssm -Arguments @('set', $ServiceName, 'AppRotateFiles', '1')
Invoke-Nssm -Arguments @('set', $ServiceName, 'AppRotateOnline', '1')
Invoke-Nssm -Arguments @('set', $ServiceName, 'AppRotateBytes', '10485760')
Invoke-Nssm -Arguments @('set', $ServiceName, 'AppExit', 'Default', 'Restart')
Invoke-Nssm -Arguments @('set', $ServiceName, 'AppRestartDelay', '5000')
Invoke-Nssm -Arguments @('set', $ServiceName, 'Start', 'SERVICE_AUTO_START')
Invoke-Nssm -Arguments @('start', $ServiceName)
$deadline = [DateTime]::UtcNow.AddSeconds($ReadyTimeoutSeconds)
while ([DateTime]::UtcNow -lt $deadline) {
    try {
        $ready = Invoke-RestMethod -Uri $readyUrl -TimeoutSec 1 -NoProxy
        if ($ready.ready -eq $true) { Write-Output '服务已启动且就绪。'; return }
    }
    catch { }
    Start-Sleep -Milliseconds 200
}
throw '新版本未在预算内就绪。服务保留当前版本，请检查数据库结构、密钥和日志，按发布恢复方案处理。'
