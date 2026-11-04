Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  Cloth Shop Management System - PowerShell Build Script" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan

foreach ($folder in @('build', 'dist')) {
    if (Test-Path $folder) {
        Write-Host "[+] Cleaning old '$folder' directory..." -ForegroundColor Yellow
        Remove-Item -Recurse -Force $folder
    }
}

if (!(Test-Path "cloth_shop_app.spec")) {
    Write-Host "[-] Error: Spec file 'cloth_shop_app.spec' not found!" -ForegroundColor Red
    exit
}

Write-Host "[+] Building single-file executable using PyInstaller..." -ForegroundColor Green
python -m PyInstaller --clean cloth_shop_app.spec

if ($LASTEXITCODE -eq 0) {
    $exePath = Resolve-Path "dist\ClothShopManagementSystem.exe"
    Write-Host "`n============================================================" -ForegroundColor Green
    Write-Host " SUCCESS: Executable created successfully!" -ForegroundColor Green
    Write-Host " Location: $exePath" -ForegroundColor White
    Write-Host "============================================================" -ForegroundColor Green
} else {
    Write-Host "`n[-] Build failed with errors." -ForegroundColor Red
}
