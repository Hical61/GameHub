<#
.SYNOPSIS
为 Windows 计划任务提供有界归档入口，默认只预览；令牌由环境或外部文件提供。
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$PythonPath,
    [Parameter(Mandatory)][string]$AdminScript,
    [Parameter(Mandatory)][string]$Url,
    [Parameter(Mandatory)][ValidatePattern('^[A-Za-z0-9_:.-]{1,32}$')][string]$Game,
    [ValidateRange(1, 36500)][int]$KeepDays = 30,
    [ValidateRange(1, 1000)][int]$BatchSize = 100,
    [ValidateRange(1, 10000)][int]$MaxBatches = 10,
    [ValidateRange(1, 86400)][int]$BudgetSeconds = 60,
    [string]$TokenFile,
    [switch]$RedactPayload,
    [switch]$Execute
)
$ErrorActionPreference = 'Stop'
foreach ($path in @($PythonPath, $AdminScript)) {
    if (-not [IO.Path]::IsPathFullyQualified($path) -or -not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw 'Python 和管理脚本必须是已存在文件的绝对路径。'
    }
}
$arguments = @($AdminScript, '--url', $Url, '--budget', [string]$BudgetSeconds)
if ($TokenFile) { $arguments += @('--token-file', $TokenFile) }
$arguments += @('archive', '--game', $Game, '--keep-days', [string]$KeepDays,
    '--batch-size', [string]$BatchSize, '--max-batches', [string]$MaxBatches)
if ($RedactPayload) { $arguments += '--redact-payload' }
Write-Output ('归档游戏：' + $Game + '；最多批次：' + $MaxBatches + '；总预算秒：' + $BudgetSeconds)
if (-not $Execute) { Write-Output '当前仅预览。添加 -Execute 才执行归档。'; return }
& $PythonPath @arguments
if ($LASTEXITCODE -ne 0) { throw '归档维护未完成，请查询已有批次结果后处理，不自动补发任务。' }
