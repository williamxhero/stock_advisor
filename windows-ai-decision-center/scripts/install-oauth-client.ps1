[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateScript({ Test-Path -LiteralPath $_ -PathType Leaf })]
    [string]$ClientJson
)

$ErrorActionPreference = 'Stop'
$source = (Resolve-Path -LiteralPath $ClientJson).Path
$dataDirectory = Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'AIDecisionCenter'
$target = Join-Path $dataDirectory 'oauth-client.json'
New-Item -ItemType Directory -Force -Path $dataDirectory | Out-Null

$document = Get-Content -LiteralPath $source -Raw | ConvertFrom-Json
if ($document.web) {
    throw '该 JSON 是 Web application 类型，不能用于本客户端。请在 Google Cloud Console 创建 Desktop app 类型的 OAuth Client。'
}
if (-not $document.installed) {
    throw '该 JSON 缺少 installed 节点，不是 Google Desktop app OAuth Client。'
}
$client = $document.installed
if (-not $client.client_id -or -not $client.client_secret) {
    throw 'OAuth JSON 缺少 client_id 或 client_secret。请下载 Desktop app 类型的客户端配置。'
}

Copy-Item -LiteralPath $source -Destination $target -Force
Write-Output "OAuth client installed: $target"
