# Run this AFTER creating empty repos on GitHub (no README/license).
# Creates:
#   https://github.com/davidhundia-boop/dt-ops-tools
#   https://github.com/davidhundia-boop/dt-ops-streamlit

$ErrorActionPreference = "Stop"
$tools = Split-Path -Parent $MyInvocation.MyCommand.Path
$streamlit = Join-Path (Split-Path -Parent $tools) "dt-ops-streamlit"

Write-Host "Pushing dt-ops-tools..."
git -C $tools push -u origin main
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "Pushing dt-ops-streamlit..."
git -C $streamlit push -u origin main
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "Done. Open:"
Write-Host "  https://github.com/davidhundia-boop/dt-ops-tools"
Write-Host "  https://github.com/davidhundia-boop/dt-ops-streamlit"
