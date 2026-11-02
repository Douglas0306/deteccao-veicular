@echo off
setlocal EnableExtensions
rem --- O PATH do Windows pode estar incompleto (ex.: um instalador sobrescreveu o PATH e perdeu o System32). Sem isso
rem     o .bat nao acha chcp, where, py, choice, fc. Garante as pastas do sistema antes de qualquer comando.
set "PATH=%SystemRoot%\System32;%SystemRoot%;%SystemRoot%\System32\Wbem;%PATH%"
"%SystemRoot%\System32\chcp.com" 65001 >nul
title Contador de Veiculos O/D
rem ------------------------------------------------------------------------------------------
rem  1a execucao: cria o ambiente virtual (.venv) nesta pasta e instala as bibliotecas do
rem  requirements.txt. Da 2a vez em diante: so abre o programa (reinstala se o requirements mudar).
rem  Teste sem abrir a janela:  Iniciar_ContadorOD.bat --so-preparar
rem ------------------------------------------------------------------------------------------
cd /d "%~dp0"
set "RAIZ=%~dp0"
set "VENV=%RAIZ%.venv"
set "VPY=%VENV%\Scripts\python.exe"
set "REQ=%RAIZ%requirements.txt"
set "MARCA=%VENV%\requisitos_instalados.txt"

echo ==================================================================
echo    CONTADOR DE VEÍCULOS O/D
echo ==================================================================

rem --- ambiente já pronto com este mesmo requirements? vai direto para o programa
if not exist "%VPY%" goto :preparar
if not exist "%MARCA%" goto :preparar
fc /b "%REQ%" "%MARCA%" >nul 2>&1
if errorlevel 1 goto :preparar
goto :iniciar

:preparar
echo.
echo  Preparando o ambiente pela primeira vez (ou após uma atualização).
echo  Isso só acontece uma vez e leva de 5 a 15 minutos, conforme a internet
echo  (cerca de 250 MB de download). As mensagens abaixo mostram o andamento:
echo  o programa NÃO está travado. Não feche esta janela.

call :passo "1/5" "Procurando o Python (3.12, 3.13 ou 3.14)"
set "PY="
py -3.13 -c "import sys" >nul 2>&1 && set "PY=py -3.13"
if not defined PY py -3.12 -c "import sys" >nul 2>&1 && set "PY=py -3.12"
if not defined PY py -3.14 -c "import sys" >nul 2>&1 && set "PY=py -3.14"
if not defined PY python -c "import sys; sys.exit(0 if (3, 12) <= sys.version_info[:2] <= (3, 14) else 1)" >nul 2>&1 && set "PY=python"
if defined PY goto :temPython

echo.
echo  [ERRO] Não encontrei o Python 3.12, 3.13 ou 3.14 neste computador.
set "PYACHADO="
for /f "tokens=1,2" %%a in ('python --version 2^>^&1') do if /i "%%a"=="Python" set "PYACHADO=%%b"
if defined PYACHADO echo        (o python que o Windows encontra aqui e o %PYACHADO%, fora da faixa aceita 3.12 a 3.14)
where winget >nul 2>&1
if errorlevel 1 goto :semWinget
echo.
choice /c SN /m " Deseja instalar o Python 3.13 agora pelo winget (instalador da Microsoft)"
if errorlevel 2 goto :semWinget
winget install -e --id Python.Python.3.13 --scope user
echo.
echo  Terminada a instalação do Python, feche esta janela e clique de novo
echo  no Iniciar_ContadorOD.bat.
pause
exit /b 1

:semWinget
echo.
echo  Instale o Python 3.13 pelo site https://www.python.org/downloads/
echo  (na instalação, marque "Add python.exe to PATH") e depois clique de novo
echo  no Iniciar_ContadorOD.bat.
pause
exit /b 1

:temPython
for /f "delims=" %%i in ('%PY% --version 2^>^&1') do set "PYVER=%%i"
echo        encontrado: %PYVER%  [%PY%]
%PY% "%RAIZ%02-Codigo\checar_caminho.py" "%RAIZ%."
if errorlevel 1 (
    pause
    exit /b 1
)

call :passo "2/5" "Criando o ambiente virtual (.venv) nesta pasta"
if exist "%VPY%" (
    echo        já existe, reaproveitando.
    goto :pip
)
%PY% -m venv "%VENV%"
if errorlevel 1 goto :falha
echo        criado.

:pip
call :passo "3/5" "Atualizando o instalador de pacotes (pip)"
"%VPY%" -m pip install --upgrade pip --disable-pip-version-check
if errorlevel 1 goto :falha

call :passo "4/5" "Baixando e instalando as bibliotecas (a etapa mais demorada)"
"%VPY%" -m pip install -r "%REQ%" --disable-pip-version-check
if errorlevel 1 goto :falha

call :passo "5/5" "Conferindo a instalação e preparando a primeira abertura"
"%VPY%" "%RAIZ%02-Codigo\verificar_ambiente.py"
if errorlevel 1 goto :falha
copy /y "%REQ%" "%MARCA%" >nul
echo.
echo  Ambiente pronto. Das próximas vezes o programa abre direto.

:iniciar
if /i "%~1"=="--so-preparar" exit /b 0
call :passo "OK" "Abrindo o Contador O/D (a janela aparece em alguns segundos)"
echo        Mantenha esta janela aberta enquanto usa o programa:
echo        avisos e erros aparecem aqui.
"%VPY%" "%RAIZ%02-Codigo\ContadorOD.py"
if errorlevel 1 (
    echo.
    echo  [ERRO] O programa fechou com erro. Veja a mensagem acima.
    pause
    exit /b 1
)
exit /b 0

:falha
echo.
echo  [ERRO] A preparação do ambiente falhou. Veja a mensagem acima.
echo  Confira a conexão com a internet e clique de novo no .bat: ele continua
echo  de onde parou. Se o erro persistir, apague a pasta .venv e tente outra vez.
pause
exit /b 1

:passo
echo.
echo [%time:~0,8%] [%~1] %~2
exit /b 0
