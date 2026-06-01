@echo off
REM ============================================================
REM   ZhouZhou Voice - Nuitka Build Script
REM   Outputs: dist\main.dist\zhouzhou-voice.exe
REM ============================================================

setlocal

REM -- Find Python (from PATH; prefer python, fallback to py launcher)
set "PYTHON=python"
where python >nul 2>&1 || set "PYTHON=py"
where %PYTHON% >nul 2>&1 || (
    echo [ERROR] Python not found. Install Python 3.10+ and add it to PATH.
    exit /b 1
)
set PROJECT_ROOT=%~dp0..
set MAIN=%PROJECT_ROOT%\main.py
set ICON=%PROJECT_ROOT%\assets\icon.ico

REM -- Read version from VERSION file
set /p APP_VERSION=<"%PROJECT_ROOT%\VERSION"

echo ============================================
echo   ZhouZhou Voice - Build v%APP_VERSION%
echo ============================================
echo.

REM -- Check Python
%PYTHON% --version
if errorlevel 1 (
    echo [ERROR] Python not found at %PYTHON%
    exit /b 1
)

echo.
echo Starting Nuitka build (this may take 10-30 minutes)...
echo.

%PYTHON% -m nuitka ^
    --standalone ^
    --assume-yes-for-downloads ^
    --output-dir="%PROJECT_ROOT%\dist" ^
    --output-filename=zhouzhou-voice.exe ^
    --enable-plugin=pyside6 ^
    --windows-console-mode=disable ^
    --windows-icon-from-ico="%ICON%" ^
    --windows-company-name="ZhouZhou Voice" ^
    --windows-product-name="ZhouZhou Voice" ^
    --windows-file-version=%APP_VERSION%.0 ^
    --windows-product-version=%APP_VERSION%.0 ^
    --windows-file-description="Offline Voice Input Tool" ^
    --include-data-dir="%PROJECT_ROOT%\assets"=assets ^
    --include-data-files="%PROJECT_ROOT%\VERSION"=VERSION ^
    --include-package=app ^
    --include-package=core ^
    --include-package=gui ^
    --include-package=hotword ^
    --include-package=llm ^
    --include-package=transcribe ^
    --include-package=utils ^
    --include-module=multiprocessing ^
    --include-module=_cffi_backend ^
    --include-module=packaging ^
    --include-package-data=_sounddevice_data ^
    --include-package-data=sherpa_onnx ^
    --include-package-data=opencc ^
    --include-package-data=certifi ^
    --nofollow-import-to=tkinter,unittest,test,doctest,pydoc,xmlrpc ^
    --nofollow-import-to=PySide6.Qt3DAnimation,PySide6.Qt3DCore,PySide6.Qt3DExtras,PySide6.Qt3DInput,PySide6.Qt3DLogic,PySide6.Qt3DRender ^
    --nofollow-import-to=PySide6.QtBluetooth,PySide6.QtCharts,PySide6.QtDataVisualization,PySide6.QtDesigner ^
    --nofollow-import-to=PySide6.QtGraphs,PySide6.QtGraphsWidgets,PySide6.QtHelp,PySide6.QtHttpServer ^
    --nofollow-import-to=PySide6.QtLocation ^
    --nofollow-import-to=PySide6.QtNfc,PySide6.QtPdf,PySide6.QtPdfWidgets,PySide6.QtPositioning ^
    --nofollow-import-to=PySide6.QtQml,PySide6.QtQuick,PySide6.QtQuick3D,PySide6.QtQuickControls2,PySide6.QtQuickWidgets ^
    --nofollow-import-to=PySide6.QtRemoteObjects,PySide6.QtScxml,PySide6.QtSensors,PySide6.QtSerialBus,PySide6.QtSerialPort ^
    --nofollow-import-to=PySide6.QtSpatialAudio,PySide6.QtSql,PySide6.QtStateMachine,PySide6.QtSvg,PySide6.QtSvgWidgets ^
    --nofollow-import-to=PySide6.QtTest,PySide6.QtTextToSpeech,PySide6.QtUiTools,PySide6.QtVirtualKeyboard ^
    --nofollow-import-to=PySide6.QtWebChannel,PySide6.QtWebEngineCore,PySide6.QtWebEngineQuick,PySide6.QtWebEngineWidgets ^
    --nofollow-import-to=PySide6.QtWebSockets,PySide6.QtWebView,PySide6.QtXml ^
    --nofollow-import-to=PySide6.QtDBus,PySide6.QtOpenGL,PySide6.QtOpenGLWidgets,PySide6.QtConcurrent ^
    --nofollow-import-to=PySide6.QtAxContainer,PySide6.QtNetworkAuth,PySide6.QtPrintSupport,PySide6.QtQuickTest ^
    --nofollow-import-to=PIL._tkinter_finder ^
    "%MAIN%"

if errorlevel 1 (
    echo.
    echo [ERROR] Build failed!
    exit /b 1
)

echo.
echo ============================================
echo   Build successful!
echo ============================================

REM -- Locate dist directory (Nuitka names it after the source file)
set DIST_DIR=
if exist "%PROJECT_ROOT%\dist\main.dist" (
    set DIST_DIR=%PROJECT_ROOT%\dist\main.dist
) else if exist "%PROJECT_ROOT%\dist\zhouzhou-voice.dist" (
    set DIST_DIR=%PROJECT_ROOT%\dist\zhouzhou-voice.dist
) else (
    echo [WARNING] Cannot find dist directory, skipping model copy
    exit /b 0
)

echo.
echo Copying model files to %DIST_DIR%\models\ ...
if exist "%PROJECT_ROOT%\models" (
    xcopy /E /Y /I "%PROJECT_ROOT%\models" "%DIST_DIR%\models" >nul 2>&1
)

echo.
echo Running build verification...
%PYTHON% "%PROJECT_ROOT%\build\verify_build.py"
if errorlevel 1 (
    echo [WARNING] Build verification failed! Check the output above.
)

echo.
echo Done! Output: %DIST_DIR%\zhouzhou-voice.exe
echo.

endlocal
