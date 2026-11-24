param(
    [string]$DataDir = "D:\work-rag\.work-rag-data",
    [string]$PluginDir = (Join-Path $HOME ".gemini\config\plugins\work-rag"),
    [string]$PythonExe = ""
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$SourcePlugin = Join-Path $RepoRoot "extensions\antigravity-plugin"

if (-not $PythonExe) {
    $PythonExe = Join-Path $RepoRoot ".venv-win\Scripts\python.exe"
}
if (-not (Test-Path $PythonExe)) {
    throw "Python runtime not found: $PythonExe. Run 'python -m venv .venv-win' and install work-rag first."
}
$PythonExe = (Resolve-Path $PythonExe).Path

if (-not [System.IO.Path]::IsPathRooted($DataDir)) {
    throw "DataDir must be an absolute path: $DataDir"
}

$PanelHtml = Join-Path $RepoRoot "work_rag\assets\panel.html"
if (-not (Test-Path $PanelHtml)) {
    throw "Built panel HTML not found: $PanelHtml. Run 'npm run build' in extensions\antigravity-app first."
}

# Initialize shared data directory if not yet initialized.
& $PythonExe -m work_rag --data-dir $DataDir init | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "Failed to initialize work-rag data directory: $DataDir"
}

New-Item -ItemType Directory -Force -Path $PluginDir | Out-Null
foreach ($sub in @("assets", "skills", "rules")) {
    $srcSub = Join-Path $SourcePlugin $sub
    $dstSub = Join-Path $PluginDir $sub
    if (Test-Path $dstSub) {
        Remove-Item -Recurse -Force $dstSub
    }
    if (Test-Path $srcSub) {
        Copy-Item -Recurse -Force $srcSub $dstSub
    }
}
foreach ($file in @("plugin.json", "README.md")) {
    Copy-Item -Force (Join-Path $SourcePlugin $file) (Join-Path $PluginDir $file)
}

$mcpConfig = @{
    mcpServers = @{
        "work-rag" = @{
            command = $PythonExe
            args = @(
                "-m",
                "work_rag",
                "--data-dir",
                $DataDir,
                "mcp",
                "--enable-app"
            )
            env = @{}
        }
    }
} | ConvertTo-Json -Depth 6

$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText((Join-Path $PluginDir "mcp_config.json"), $mcpConfig + "`n", $utf8NoBom)

Write-Host "Installed Antigravity plugin to: $PluginDir"
Write-Host "Shared data directory:           $DataDir"
Write-Host "Python executable:               $PythonExe"
