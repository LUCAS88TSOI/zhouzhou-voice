@echo off
cd /d "%~dp0"

echo.
echo ============================================
echo   Zhouzhou Voice - Starting...
echo ============================================
echo.

:: Find Python (from PATH; prefer python, fallback to py launcher)
set "PYTHON=python"
where python >nul 2>&1 || set "PYTHON=py"
where %PYTHON% >nul 2>&1 || (
    echo [ERROR] Python not found. Install Python 3.10+ and add it to PATH.
    pause
    exit /b 1
)

:: Check model file
if not exist "models\sensevoice\model.onnx" (
    echo [ERROR] ASR model not found
    echo Please put model.onnx in models\sensevoice\
    pause
    exit /b 1
)

echo Python: %PYTHON%
echo Model:  models\sensevoice\model.onnx
echo.
echo Loading ASR model (10-30 seconds first time)...
echo.

%PYTHON% main.py

echo.
echo ============================================
echo   Program ended
echo ============================================
pause
