"""Fonte do vídeo: resolve link (YouTube ou outro site) ou arquivo local e tira o snapshot.

O link é resolvido pelo yt-dlp chamado como programa externo (yt-dlp.exe), não como
biblioteca: no executável final ele fica ao lado do .exe e pode ser atualizado sozinho
quando o YouTube mudar, sem gerar um executável novo.
"""
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

# vídeo até 720p: decodificar 1080p consome CPU que faz falta à detecção
FORMATO = "bestvideo[height<=720][vcodec^=avc1]/best[height<=720]/best"
EXTENSOES_VIDEO = {".mp4", ".avi", ".mkv", ".mov", ".ts", ".m4v", ".webm", ".flv"}


@dataclass
class InfoFonte:
    entrada: str                 # o que o usuário digitou
    tipo: str                    # "arquivo" | "link"
    stream_url: str              # o que o OpenCV abre
    video_id: str
    titulo: str
    ao_vivo: bool
    largura: int = 0
    altura: int = 0
    fps: float = 0.0
    duracao_s: float | None = None
    extrator: str = ""           # site reconhecido pelo yt-dlp (youtube, generic...)
    avisos: list = field(default_factory=list)


class ErroFonte(Exception):
    """Mensagem já pronta para mostrar ao usuário."""


def localizar_ytdlp() -> str | None:
    """yt-dlp.exe ao lado do executável/script, no ambiente Python ou no PATH."""
    candidatos = [Path(sys.executable).parent / "yt-dlp.exe",
                  Path(sys.executable).parent / "Scripts" / "yt-dlp.exe",
                  Path(__file__).resolve().parents[2] / "yt-dlp.exe"]
    for c in candidatos:
        if c.exists():
            return str(c)
    return shutil.which("yt-dlp")


def eh_arquivo(texto: str) -> bool:
    p = Path(texto.strip().strip('"'))
    return p.suffix.lower() in EXTENSOES_VIDEO or p.exists()


def _id_curto(texto: str) -> str:
    """Identificador só com letras minúsculas, números e _ (vira nome de pasta e de arquivo)."""
    s = re.sub(r"[^a-z0-9]+", "_", texto.lower()).strip("_")
    return s[:40] or "camera"


def sugerir_camera_id(info: InfoFonte) -> str:
    if info.tipo == "arquivo":
        return _id_curto(Path(info.entrada).stem)
    # o YouTube acrescenta "AAAA-MM-DD HH:MM" ao título das lives
    titulo = re.sub(r"\d{4}-\d{2}-\d{2}( \d{2}:\d{2})?", "", info.titulo)
    return _id_curto(titulo) if re.search(r"[a-z]", titulo, re.I) else _id_curto(info.video_id)


def _resolver_arquivo(texto: str) -> InfoFonte:
    caminho = Path(texto.strip().strip('"'))
    if not caminho.exists():
        raise ErroFonte(f"Arquivo não encontrado:\n{caminho}")
    return InfoFonte(entrada=str(caminho), tipo="arquivo", stream_url=str(caminho),
                     video_id=caminho.stem, titulo=caminho.name, ao_vivo=False)


def _resolver_link(url: str, timeout_s: float) -> InfoFonte:
    ytdlp = localizar_ytdlp()
    if ytdlp is None:
        raise ErroFonte("Leitor de links (yt-dlp.exe) não encontrado.")
    # no-certifi: usa os certificados do Windows. Com antivírus que inspeciona HTTPS, o pacote
    # próprio de certificados do yt-dlp recusa a conexão ("CERTIFICATE_VERIFY_FAILED").
    base = [ytdlp, "-J", "--no-playlist", "--no-warnings", "--compat-options", "no-certifi", "-f", FORMATO]
    # O cliente padrão do YouTube passou a exigir um runtime JavaScript (Deno); sem ele a live
    # vem "UNPLAYABLE". O cliente android entrega o HLS sem isso, então é a 2a tentativa.
    tentativas = [[]]
    if re.search(r"youtube\.com|youtu\.be", url, re.I):
        tentativas.append(["--extractor-args", "youtube:player_client=android"])
    for extra in tentativas:
        try:
            r = subprocess.run(base + extra + [url], capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=timeout_s,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired:
            raise ErroFonte(f"O site não respondeu em {timeout_s:.0f} s. Verifique a internet e o link.")
        if r.returncode == 0:
            break
        if "Unsupported URL" in r.stderr or "is not a valid URL" in r.stderr:
            break  # outro cliente não resolve link inválido
    if r.returncode != 0:
        msg = (r.stderr or "").strip().splitlines()
        detalhe = msg[-1] if msg else "erro desconhecido"
        if "Unsupported URL" in detalhe:
            raise ErroFonte("Este site não é reconhecido pelo leitor de links.")
        if "is not a valid URL" in detalhe or "No such file" in detalhe:
            raise ErroFonte("Link inválido.")
        raise ErroFonte(f"Não foi possível ler o link:\n{detalhe}")
    info = json.loads(r.stdout)
    stream = info.get("url")
    if not stream and info.get("requested_formats"):
        stream = info["requested_formats"][0].get("url")
    if not stream:
        raise ErroFonte("O link não tem um vídeo que possa ser lido.")
    ao_vivo = bool(info.get("is_live"))
    fonte = InfoFonte(
        entrada=url, tipo="link", stream_url=stream,
        video_id=str(info.get("id") or "video"), titulo=info.get("title") or url, ao_vivo=ao_vivo,
        largura=int(info.get("width") or 0), altura=int(info.get("height") or 0),
        fps=float(info.get("fps") or 0), duracao_s=None if ao_vivo else info.get("duration"),
        extrator=info.get("extractor_key") or "",
    )
    if info.get("live_status") == "is_upcoming":
        raise ErroFonte("A transmissão ainda não começou.")
    if info.get("live_status") == "post_live":
        fonte.avisos.append("A live terminou; o vídeo gravado dela será usado.")
    return fonte


def resolver_fonte(texto: str, timeout_s: float = 45.0) -> InfoFonte:
    texto = texto.strip()
    if not texto:
        raise ErroFonte("Informe um link ou escolha um arquivo de vídeo.")
    if eh_arquivo(texto) and not re.match(r"https?://", texto, re.I):
        return _resolver_arquivo(texto)
    if not re.match(r"https?://", texto, re.I):
        raise ErroFonte("O link deve começar com http:// ou https://")
    return _resolver_link(texto, timeout_s)


def capturar_snapshot(info: InfoFonte, momento_s: float | None = None) -> np.ndarray:
    """Abre a fonte e devolve um quadro. Também completa resolução, fps e duração.

    Arquivo e vídeo gravado: quadro no instante momento_s (padrão: 5 s ou metade do vídeo).
    Live: descarta os primeiros quadros (às vezes chegam cinza) e devolve o seguinte.
    """
    cap = cv2.VideoCapture(info.stream_url, cv2.CAP_FFMPEG)
    if not cap.isOpened():
        raise ErroFonte("O vídeo foi encontrado, mas não abriu para leitura.")
    try:
        fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
        n = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0.0
        if not info.ao_vivo:
            if fps > 0 and n > 0:
                info.duracao_s = info.duracao_s or n / fps
            if momento_s is None:
                momento_s = min(5.0, (info.duracao_s or 10.0) / 2)
            cap.set(cv2.CAP_PROP_POS_MSEC, momento_s * 1000)
        descartar = 10 if info.ao_vivo else 0
        quadro = None
        for _ in range(descartar + 30):
            ok, img = cap.read()
            if not ok:
                break
            if descartar:
                descartar -= 1
                continue
            if img is not None and img.std() > 2:  # quadro "vazio" (todo cinza ou preto) não serve
                quadro = img
                break
        if quadro is None:
            raise ErroFonte("Não foi possível obter um quadro do vídeo.")
        info.altura, info.largura = quadro.shape[:2]
        info.fps = info.fps or fps
        if info.altura > 720:
            info.avisos.append(f"Vídeo em {info.altura}p: acima de 720p a detecção fica mais lenta.")
        return quadro
    finally:
        cap.release()
