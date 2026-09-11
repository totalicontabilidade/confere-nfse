@echo off
chcp 65001 >nul
title Confere NFS-e - Totali
cd /d "%~dp0"

rem procura o Python instalado nesta maquina
set PY=
for %%P in (
  "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
  "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
  "%ProgramFiles%\Python312\python.exe"
) do if exist %%P set PY=%%P
if "%PY%"=="" set PY=python

echo.
echo  ============================================
echo   Confere NFS-e - Totali
echo  ============================================
echo   Endereco: http://localhost:8131
echo   Mantenha esta janela aberta enquanto usa.
echo  ============================================
echo.

:rodar
%PY% -u servidor.py 8131
echo.
echo  O servidor parou. Reiniciando em 3 segundos...
timeout /t 3 /nobreak >nul
goto rodar
