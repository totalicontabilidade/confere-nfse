@echo off
rem Mantem o servidor do Confere NFS-e no ar: se ele cair, sobe de novo em 5 segundos.
rem Roda com python.exe (e o que o firewall do Windows ja libera para a rede) e e
rem chamado escondido pelos .vbs, entao nao aparece janela nenhuma.
cd /d "%~dp0"
set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not exist "%PY%" set "PY=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if not exist "%PY%" set "PY=python"

:rodar
rem o log nao cresce para sempre
if exist servidor.log for %%A in (servidor.log) do if %%~zA GTR 5000000 del servidor.log
echo [%date% %time%] iniciando>> servidor.log
"%PY%" -u servidor.py 8131 --sem-navegador>> servidor.log 2>&1
if "%errorlevel%"=="3" (
  echo [%date% %time%] a porta 8131 ja esta em uso: outro servidor ja esta rodando.>> servidor.log
  exit /b
)
if exist parar.sinal (
  del parar.sinal
  echo [%date% %time%] encerrado a pedido.>> servidor.log
  exit /b
)
echo [%date% %time%] o servidor parou (codigo %errorlevel%). Reiniciando em 5 segundos.>> servidor.log
rem timeout nao espera quando nao ha janela; o ping espera
ping -n 6 127.0.0.1 >nul
goto rodar
