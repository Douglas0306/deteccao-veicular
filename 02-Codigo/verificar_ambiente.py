"""Confere o ambiente recém-instalado e já faz as preparações lentas da 1a abertura (cache de fontes do
matplotlib), para que o programa abra rápido. Chamado pelo Iniciar_ContadorOD.bat."""
import importlib
import shutil
import sys
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
falhas = []


def item(texto, funcao):
    inicio = time.time()
    print(f"      - {texto}...", end=" ", flush=True)
    try:
        detalhe = funcao()
        print(f"ok{f' ({detalhe})' if detalhe else ''} [{time.time() - inicio:.1f} s]", flush=True)
    except Exception as e:  # mostra e segue, para listar todos os problemas de uma vez
        print(f"FALHOU: {e}", flush=True)
        falhas.append(texto)


def versao(modulo):
    return lambda: importlib.import_module(modulo).__version__


def gpu():
    import onnxruntime as ort
    prov = ort.get_available_providers()
    return f"{ort.__version__}; GPU (DirectML) disponível" if "DmlExecutionProvider" in prov else \
        f"{ort.__version__}; só CPU"


def fontes_matplotlib():
    import matplotlib
    matplotlib.use("Agg")
    from matplotlib import font_manager
    font_manager.findfont("DejaVu Sans")  # cria o cache de fontes (demora só na 1a vez)
    return matplotlib.__version__


def janela():
    from PySide6 import QtWidgets  # noqa: F401
    import PySide6
    return PySide6.__version__


def leitor_links():
    exe = Path(sys.executable).parent / "yt-dlp.exe"
    if not exe.exists() and not shutil.which("yt-dlp"):
        raise RuntimeError("yt-dlp.exe não encontrado")
    return exe.name


def modelo():
    modelos = sorted((RAIZ / "03-Modelos").glob("*.onnx"))
    if not modelos:
        raise RuntimeError("nenhum .onnx em 03-Modelos")
    return ", ".join(m.name for m in modelos)


item("numpy", versao("numpy"))
item("OpenCV (leitura de vídeo)", versao("cv2"))
item("ONNX Runtime (detector)", gpu)
item("supervision", versao("supervision"))
item("trackers (ByteTrack)", lambda: importlib.import_module("trackers") and "")
item("PySide6 (janela)", janela)
item("matplotlib + cache de fontes (gráficos)", fontes_matplotlib)
item("yt-dlp (links do YouTube)", leitor_links)
item("modelo de detecção", modelo)

if falhas:
    print(f"\n      Problemas: {', '.join(falhas)}")
    sys.exit(1)
