@echo off
echo ========================================
echo Building VoidSyn Executable and Installer
echo ========================================

:: Check if Python is available
python --version >nul 2>&1
if errorlevel 1 goto no_python

:: Install/upgrade dependencies
echo Installing dependencies...
pip install -r requirements.txt

:: Clean previous builds
echo Cleaning previous builds...
if exist "dist" rmdir /s /q "dist"
if exist "build" rmdir /s /q "build"
if exist "installer" rmdir /s /q "installer"

:: Build executable with PyInstaller
echo Building executable...
pyinstaller voidsyn.spec

:: Check if build was successful
if exist "dist\VoidSyn\VoidSyn.exe" goto exe_ok
echo Error: Executable build failed
goto end

:exe_ok
echo Executable built successfully!

:: Create installer directory
mkdir installer

:: Locate Inno Setup Compiler (ISCC)
set "ISCC=iscc"
where %ISCC% >nul 2>&1
if errorlevel 1 (
    if exist "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" (
        set "ISCC=C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
    ) else if exist "C:\Program Files\Inno Setup 6\ISCC.exe" (
        set "ISCC=C:\Program Files\Inno Setup 6\ISCC.exe"
    ) else (
        goto no_iscc
    )
)

:: Build installer
echo Building installer...
:: Ensure no running app locks files
taskkill /IM VoidSyn.exe /F >nul 2>&1
:: Remove previous installer output if present
if exist "installer\VoidSyn_Setup_v1.0.0.exe" del /f /q "installer\VoidSyn_Setup_v1.0.0.exe"
"%ISCC%" voidsyn_installer.iss

:: Check if installer was built
if exist "installer\VoidSyn_Setup_v1.0.0.exe" goto installer_ok
echo Warning: Installer build may have failed
echo The executable is still available in: dist\VoidSyn\VoidSyn.exe
goto end

:installer_ok
echo ========================================
echo Build completed successfully!
echo ========================================
echo Executable: dist\VoidSyn\VoidSyn.exe
echo Installer: installer\VoidSyn_Setup_v1.0.0.exe
echo ========================================
goto end

:no_iscc
echo Warning: Inno Setup Compiler (iscc) not found in PATH
echo Please install Inno Setup and add it to your PATH, or run the .iss file manually
echo The executable is available in: dist\VoidSyn\VoidSyn.exe
goto end

:no_python
echo Error: Python is not installed or not in PATH
goto end

:end
pause
