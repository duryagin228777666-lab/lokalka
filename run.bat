@echo off
rem Lokalka launcher. Order: portable Python next to this file, then "py", then "python".
setlocal
set "HERE=%~dp0"

if exist "%HERE%python\python.exe" (
    "%HERE%python\python.exe" "%HERE%run.py" %*
) else (
    py -3 -c "" >nul 2>nul
    if not errorlevel 1 (
        py -3 "%HERE%run.py" %*
    ) else (
        python -c "" >nul 2>nul
        if not errorlevel 1 (
            python "%HERE%run.py" %*
        ) else (
            echo Python was not found on this computer.
            echo Put a portable Python into "%HERE%python" - see README.md for the steps.
            pause
        )
    )
)
