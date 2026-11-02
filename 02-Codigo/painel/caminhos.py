"""Pastas padrão: no .exe ficam ao lado do executável (portátil); no desenvolvimento, em PROJETO_V2/."""
import sys
from pathlib import Path

CONGELADO = getattr(sys, "frozen", False)
RAIZ = Path(sys.executable).parent if CONGELADO else Path(__file__).resolve().parents[2]


def pasta_config() -> Path:
    """JSON de ROIs de cada câmera."""
    return RAIZ / ("config" if CONGELADO else "01-Config")


def pasta_modelos() -> Path:
    return RAIZ / ("modelos" if CONGELADO else "03-Modelos")


def pasta_saida_padrao() -> Path:
    return RAIZ / ("saida" if CONGELADO else "04-Saida")


def modelo_padrao() -> Path:
    """Modelo provisório da equipe; se não existir, o primeiro .onnx da pasta de modelos."""
    preferido = pasta_modelos() / "vehicle_stage2_full_v5_best.onnx"
    if preferido.exists():
        return preferido
    outros = sorted(pasta_modelos().glob("*.onnx"))
    return outros[0] if outros else preferido
