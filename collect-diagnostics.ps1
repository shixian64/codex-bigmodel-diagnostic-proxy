$ErrorActionPreference = "Continue"
Set-Location -LiteralPath $PSScriptRoot

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$work = Join-Path $PSScriptRoot "logs\windows-$stamp"
New-Item -ItemType Directory -Force -Path $work | Out-Null

function Write-Section([string]$Path, [string]$Title, [scriptblock]$Action) {
    "===== $Title =====" | Out-File -FilePath $Path -Encoding utf8 -Append
    try { & $Action | Out-String -Width 260 | Out-File -FilePath $Path -Encoding utf8 -Append }
    catch { $_ | Out-String | Out-File -FilePath $Path -Encoding utf8 -Append }
    "" | Out-File -FilePath $Path -Encoding utf8 -Append
}

function Protect-DiagnosticLine([string]$Line) {
    if ($null -eq $Line) { return "" }
    $safe = $Line -replace '(?i)^(Set-Cookie|Authorization|Proxy-Authorization):.*$', '$1: ***REDACTED***'
    $safe = $safe -replace '(?i)(https?://)[^/:\s]+:[^@\s/]+@', '$1***:***@'
    $safe = $safe -replace '(?i)([?&](?:api[-_]?key|access[-_]?token|token|key|authorization)=)[^&\s]+', '$1***REDACTED***'
    $safe = $safe -replace '(?i)(\bBearer\s+)[A-Za-z0-9._~+/=-]{8,}', '$1***REDACTED***'
    return $safe
}

$system = Join-Path $work "system.txt"
Write-Section $system "时间" { Get-Date -Format o }
Write-Section $system "Windows" { Get-CimInstance Win32_OperatingSystem | Select-Object Caption,Version,BuildNumber,OSArchitecture }
Write-Section $system "Python" { python --version }
Write-Section $system "Codex" { codex --version }
Write-Section $system "WinHTTP 代理" { netsh winhttp show proxy }
Write-Section $system "显式代理环境（值已隐藏）" {
    Get-ChildItem Env: | Where-Object { $_.Name -match '^(HTTP|HTTPS|ALL|NO)_PROXY$|SSL_CERT|NODE_EXTRA_CA_CERTS' } |
        ForEach-Object { [pscustomobject]@{ Name=$_.Name; Present=$true; ValueLength=$_.Value.Length } }
}
Write-Section $system "相关安全/代理进程（只列名称）" {
    Get-Process -ErrorAction SilentlyContinue |
        Where-Object { $_.ProcessName -match '(?i)qianxin|qax|天擎|360|edr|mihomo|clash|sparkle|proxy' } |
        Select-Object ProcessName,Id
}

$network = Join-Path $work "network.txt"
Write-Section $network "DNS" { Resolve-DnsName open.bigmodel.cn -ErrorAction SilentlyContinue | Select-Object Name,Type,IPAddress,NameHost }
Write-Section $network "TCP 443" { Test-NetConnection open.bigmodel.cn -Port 443 -InformationLevel Detailed }
Write-Section $network "API 无鉴权探测" {
    curl.exe -sS -D - -o NUL --connect-timeout 15 --max-time 30 "https://open.bigmodel.cn/api/v1/models" |
        ForEach-Object { Protect-DiagnosticLine $_ }
}
Write-Section $network "IPv4 默认/198.18 路由" {
    Get-NetRoute -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.DestinationPrefix -eq '0.0.0.0/0' -or $_.DestinationPrefix -like '198.18*' } |
        Select-Object ifIndex,InterfaceAlias,DestinationPrefix,NextHop,RouteMetric
}

$configOut = Join-Path $work "codex-provider-config.txt"
$codexHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME ".codex" }
$config = Join-Path $codexHome "config.toml"
if (Test-Path -LiteralPath $config) {
    Get-Content -LiteralPath $config |
        Where-Object { $_ -match '^\s*(model|model_provider|model_catalog_json|wire_api|base_url|request_max_retries|stream_max_retries|stream_idle_timeout_ms|requires_openai_auth|name)\s*=|^\s*\[model_providers\.' } |
        ForEach-Object { Protect-DiagnosticLine $_ } |
        Out-File -FilePath $configOut -Encoding utf8
} else {
    "未找到 $config" | Out-File -FilePath $configOut -Encoding utf8
}

$python = if (Test-Path ".\.venv\Scripts\python.exe") { ".\.venv\Scripts\python.exe" } else { "python" }
& $python ".\bigmodel_diagnostic_proxy.py" pack

$zip = Join-Path $PSScriptRoot "logs\windows-diagnostics-$stamp.zip"
Compress-Archive -Path "$work\*" -DestinationPath $zip -Force
Write-Host "Windows 诊断包：$zip" -ForegroundColor Green
Write-Host "代理自身诊断包也已生成在 logs 目录。" -ForegroundColor Green
