$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

# 保证中文控制台和重定向日志统一使用 UTF-8。
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
try { [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false) } catch {}

$python = $null
if (Test-Path -LiteralPath ".\.venv\Scripts\python.exe") {
    $python = (Resolve-Path ".\.venv\Scripts\python.exe").Path
} else {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { $python = $cmd.Source }
}

if (-not $python) {
    Write-Host "未找到 Python。请先安装 Python 3.10 或更高版本。" -ForegroundColor Red
    exit 1
}

& $python -c "import flask, httpx, waitress" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "缺少依赖，请先执行：" -ForegroundColor Yellow
    Write-Host "  python -m pip install -r requirements.txt"
    exit 1
}

Write-Host "正在以前台模式启动，窗口不要关闭；按 Ctrl+C 停止。" -ForegroundColor Cyan
& $python ".\bigmodel_diagnostic_proxy.py" serve
exit $LASTEXITCODE
