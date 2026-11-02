"""Ponto de entrada da aplicação (vira o ContadorOD.exe no empacotamento).

Rodar de dentro da pasta PROJETO_V2:
  .venv\\Scripts\\python 02-Codigo\\ContadorOD.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from painel.app import main  # noqa: E402

if __name__ == "__main__":
    main()
