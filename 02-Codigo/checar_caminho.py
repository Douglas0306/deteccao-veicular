"""Roda com o Python do sistema (só biblioteca padrão), antes de criar o .venv.
O Windows limita caminhos a 260 caracteres; algumas bibliotecas têm arquivos com ~140 caracteres
dentro do .venv, então uma pasta muito "funda" faz a instalação falhar no meio."""
import os
import sys
import winreg

LIMITE = 100  # caracteres do caminho da pasta do programa

pasta = os.path.abspath(sys.argv[1])  # o .bat passa "pasta\." (a barra final escaparia a aspa)
try:
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\FileSystem") as k:
        caminhos_longos = winreg.QueryValueEx(k, "LongPathsEnabled")[0] == 1
except OSError:
    caminhos_longos = False

if len(pasta) > LIMITE and not caminhos_longos:
    print(f"\n  [ERRO] O caminho desta pasta é longo demais para o Windows ({len(pasta)} caracteres):")
    print(f"         {pasta}")
    print("         A instalação das bibliotecas falharia no meio.")
    print("         Mova a pasta para um caminho curto, por exemplo C:\\ContadorOD ou a Área de Trabalho,")
    print("         e clique de novo no Iniciar_ContadorOD.bat.")
    sys.exit(1)
print(f"       caminho ok ({len(pasta)} caracteres).")
