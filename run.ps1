# Resume AI: start the app from source (Windows PowerShell).
#   .\run.ps1
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

if (-not (Test-Path ".env")) {
    Write-Host "No .env file yet. Copy .env.example to .env and fill in SUPABASE_URL and SUPABASE_PUBLISHABLE_KEY." -ForegroundColor Yellow
}

if (Get-Command uv -ErrorAction SilentlyContinue) {
    uv sync
    uv run python -m app.main
}
else {
    # No uv: fall back to pip + venv.
    if (-not (Test-Path ".venv")) { python -m venv .venv }
    & .\.venv\Scripts\python.exe -m pip install --upgrade pip | Out-Null
    & .\.venv\Scripts\python.exe -m pip install -r requirements.txt
    & .\.venv\Scripts\python.exe -m app.main
}
