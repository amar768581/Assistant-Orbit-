# ORBIT — one-click Windows installer
# Usage:  powershell -ExecutionPolicy Bypass -File .\install.ps1

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Write-Host ""
Write-Host "  ORBIT — Bengali personal AI assistant for Windows" -ForegroundColor Cyan
Write-Host "  ------------------------------------------------" -ForegroundColor DarkGray

# 1. Python
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    Write-Host "Python পাওয়া যায়নি। https://www.python.org/downloads/ থেকে Python 3.10+ ইনস্টল করুন।" -ForegroundColor Red
    Write-Host "(ইনস্টল করার সময় 'Add python.exe to PATH' টিক দিন।)"
    exit 1
}

$version = & python -c "import sys; print('.'.join(map(str, sys.version_info[:3])))"
Write-Host "Python $version" -ForegroundColor Green

# 2. Dependencies
Write-Host "Dependencies ইনস্টল হচ্ছে (psutil, pygame, edge-tts, ...)" -ForegroundColor Yellow
& python -m pip install --upgrade pip
& python -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) {
    Write-Host "pip install ব্যর্থ হয়েছে। ইন্টারনেট/প্রক্সি দেখুন।" -ForegroundColor Red
    exit $LASTEXITCODE
}

# 3. Config + data folders
if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "নতুন .env তৈরি হয়েছে — GEMINI_API_KEY ও GROQ_API_KEY দিন।" -ForegroundColor Yellow
} else {
    Write-Host ".env আগে থেকেই আছে — অপরিবর্তিত রাখা হলো।" -ForegroundColor DarkGray
}

$orbitHome = Join-Path $HOME ".orbit"
New-Item -ItemType Directory -Force -Path $orbitHome | Out-Null
$contacts = Join-Path $orbitHome "contacts.json"
if (-not (Test-Path $contacts)) {
    [System.IO.File]::WriteAllText($contacts, "[]")
    Write-Host "চাইলে $contacts ফাইলে নাম/নম্বর যোগ করুন।" -ForegroundColor DarkGray
}

Write-Host ""
Write-Host "🔒 Banking/OTP guardrail সবসময় চালু — কোনো ব্যাংক/UPI/ওয়ালেট অ্যাপ, PIN বা OTP নয়।" -ForegroundColor Cyan
Write-Host ""
Write-Host "চালান:" -ForegroundColor Green
Write-Host "  python main.py            # ইন্টারঅ্যাকটিভ চ্যাট"
Write-Host "  python main.py once `"আজকের আবহাওয়া কেমন?`""
Write-Host "  python main.py doctor     # প্রস্তুতি পরীক্ষা"
Write-Host "  .\run_orbit.bat           # চ্যাট (সহজ উপায়)"
Write-Host ""
& python main.py doctor
