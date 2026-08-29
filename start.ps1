param([switch]$CheckOnly)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

# 保证中文控制台和重定向日志统一使用 UTF-8。
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
try { [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false) } catch {}

# 显式代理环境下必须保证本地 Codex -> 127.0.0.1 不被送往企业代理。
$noProxyEntries = @()
if ($env:NO_PROXY) {
    $noProxyEntries += $env:NO_PROXY -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ }
}
foreach ($entry in @("127.0.0.1", "localhost", "::1")) {
    if ($noProxyEntries -notcontains $entry) { $noProxyEntries += $entry }
}
$env:NO_PROXY = $noProxyEntries -join ','
$env:no_proxy = $env:NO_PROXY

function Test-PythonCandidate {
    param(
        [Parameter(Mandatory = $true)][string]$Command,
        [string[]]$PrefixArgs = @()
    )
    $oldPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $output = & $Command @PrefixArgs -c "import sys, flask, httpx, waitress; print(sys.executable)" 2>&1 | Out-String
        $code = $LASTEXITCODE
    } catch {
        $output = $_ | Out-String
        $code = 1
    } finally {
        $ErrorActionPreference = $oldPreference
    }
    return [pscustomobject]@{ ExitCode = $code; Output = $output.Trim() }
}

$candidates = @()
if (Test-Path -LiteralPath ".\.venv\Scripts\python.exe") {
    $candidates += [pscustomobject]@{
        Label = "项目 .venv"
        Command = (Resolve-Path ".\.venv\Scripts\python.exe").Path
        PrefixArgs = @()
    }
}
$pyLauncher = Get-Command py -ErrorAction SilentlyContinue
if ($pyLauncher) {
    $candidates += [pscustomobject]@{
        Label = "Python Launcher (py -3)"
        Command = $pyLauncher.Source
        PrefixArgs = @("-3")
    }
}
$pythonCmd = Get-Command python -ErrorAction SilentlyContinue
if ($pythonCmd) {
    $candidates += [pscustomobject]@{
        Label = "PATH 中的 python"
        Command = $pythonCmd.Source
        PrefixArgs = @()
    }
}

$selected = $null
$failures = @()
foreach ($candidate in $candidates) {
    $probe = Test-PythonCandidate -Command $candidate.Command -PrefixArgs $candidate.PrefixArgs
    if ($probe.ExitCode -eq 0) {
        $selected = $candidate
        $selected | Add-Member -NotePropertyName Executable -NotePropertyValue (($probe.Output -split "`r?`n")[-1])
        break
    }
    $failures += "[$($candidate.Label)] $($probe.Output)"
}

if (-not $selected) {
    Write-Host "未找到同时具备 Flask、httpx、Waitress 的 Python 3。" -ForegroundColor Red
    if ($failures.Count -gt 0) {
        Write-Host "解释器探测结果：" -ForegroundColor Yellow
        $failures | ForEach-Object { Write-Host $_ }
    }
    Write-Host "请执行：py -3 -m pip install -r requirements.txt" -ForegroundColor Yellow
    exit 1
}

$pythonCommand = $selected.Command
$pythonPrefixArgs = @($selected.PrefixArgs)
Write-Host "使用解释器：$($selected.Label) -> $($selected.Executable)" -ForegroundColor DarkCyan

if ($CheckOnly) {
    Write-Host "解释器和依赖检查通过。" -ForegroundColor Green
    exit 0
}

Write-Host "正在以前台模式启动，窗口不要关闭；按 Ctrl+C 停止。" -ForegroundColor Cyan
# Windows PowerShell 5.1 会把原生程序 stderr 包装成 NativeCommandError；启动阶段改为 Continue，保留原始错误。
$ErrorActionPreference = "Continue"
& $pythonCommand @pythonPrefixArgs ".\bigmodel_diagnostic_proxy.py" serve
exit $LASTEXITCODE
