"""Leitura de quadros para a coleta: arquivo/vídeo gravado ou live, com reconexão.

- Arquivo e vídeo gravado: lê em sequência e entrega um quadro a cada 1/fps_alvo s de vídeo;
  a fila bloqueia, nenhum quadro é perdido (o resultado não depende da velocidade do PC).
- Live: lê no ritmo da transmissão e entrega sempre o quadro mais recente; se a detecção
  estiver lenta, quadros antigos são descartados (senão o atraso cresceria sem parar).
  Se o stream cair, reabre pedindo uma URL nova (as do YouTube expiram em algumas horas).
"""
import queue
import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import cv2
import numpy as np

ESPERAS_RECONEXAO_S = (2, 5, 10, 20, 30, 60)


@dataclass
class Quadro:
    indice: int
    t: float             # segundos desde o início da coleta (vídeo: tempo do vídeo)
    ts: datetime         # horário do computador, com fuso
    imagem: np.ndarray


def agora() -> datetime:
    return datetime.now().astimezone()


def inicio_pelo_nome(caminho, tz) -> datetime | None:
    """Gravações do tipo CAM-..._AAAAMMDD_HHMMSS.mp4 trazem a hora de início no nome."""
    m = re.search(r"(\d{8})_(\d{6})", Path(caminho).stem)
    if not m:
        return None
    return datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").replace(tzinfo=tz)


class Leitor(threading.Thread):
    ATRASO_S = 6.0  # live: buffer de reprodução, um pouco maior que um bloco HLS (~5 s)

    def __init__(self, stream_url: str, ao_vivo: bool, fps_alvo: float, fila: queue.Queue,
                 parar: threading.Event, inicio: datetime | None = None, reabrir=None):
        super().__init__(name="leitor", daemon=True)
        self.url, self.ao_vivo, self.fps_alvo = stream_url, ao_vivo, fps_alvo
        self.fila, self.parar = fila, parar
        self.inicio = inicio or agora()
        self.reabrir = reabrir            # callable -> nova URL do stream (live)
        self.estado = "conectando"
        self.reconexoes = 0
        self.tempo_sem_video_s = 0.0
        self.quedas: list[tuple[datetime, datetime]] = []
        self.fim_do_video = False
        self.descartados = 0              # live: quadros jogados fora por atraso da detecção
        self.duracao_video_s: float | None = None
        self.erro: Exception | None = None

    def run(self):
        try:
            if self.ao_vivo:
                self._ler_live()
            else:
                self._ler_sequencial()
        except Exception as e:
            self.erro = e
        finally:
            self.estado = "encerrado"
            self.fila.put(None)

    # ---------------------------------------------------------- arquivo / vídeo gravado
    def _ler_sequencial(self):
        cap = cv2.VideoCapture(self.url, cv2.CAP_FFMPEG)
        if not cap.isOpened():
            raise IOError("não foi possível abrir o vídeo")
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        n = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
        self.duracao_video_s = n / fps if n else None
        self.estado = "rodando"
        intervalo, proximo, indice = 1.0 / self.fps_alvo, 0.0, -1
        while not self.parar.is_set():
            if not cap.grab():
                self.fim_do_video = True
                break
            indice += 1
            t = indice / fps
            if t + 1e-6 < proximo:
                continue
            ok, img = cap.retrieve()
            if not ok:
                self.fim_do_video = True
                break
            proximo += intervalo
            q = Quadro(indice, t, self.inicio + timedelta(seconds=t), img)
            while not self.parar.is_set():
                try:
                    self.fila.put(q, timeout=0.5)
                    break
                except queue.Full:
                    continue
        cap.release()

    # ---------------------------------------------------------- live
    def _abrir(self, url):
        cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 2)
        return cap if cap.isOpened() else None

    def _ler_live(self):
        """A live do YouTube (HLS) chega em blocos: ~5 s sem nada e depois ~150 quadros de uma vez.
        Entregar cada bloco de uma vez fazia a prévia "acelerar e travar" e o processamento
        trabalhar aos trancos. Por isso há um buffer de reprodução (como num player de vídeo):
        a leitura escolhe os quadros pelo tempo do vídeo e um liberador os entrega no ritmo real,
        com atraso fixo de ATRASO_S. O horário de cada quadro é o do momento em que é liberado,
        então o espaçamento entre quadros é o real (antes o 1o bloco, que chega com ~15 s
        acumulados, ficava com os horários espremidos)."""
        buffer: deque = deque()
        trava = threading.Lock()
        liberador = threading.Thread(target=self._liberar, args=(buffer, trava), name="liberador", daemon=True)
        liberador.start()
        cap = self._abrir(self.url)
        if cap is None:
            cap = self._reconectar()
        intervalo = 1.0 / self.fps_alvo
        self.estado = "rodando"
        while not self.parar.is_set() and cap is not None:
            fps_fonte = cap.get(cv2.CAP_PROP_FPS) or 30.0
            lidos, proximo, ancora = 0, 0.0, None  # ancora = (relógio, tempo de vídeo) de referência
            while not self.parar.is_set():
                ok, img = cap.read()
                if not ok or img is None:
                    break
                t_video = lidos / fps_fonte
                lidos += 1
                if t_video + 1e-6 < proximo:
                    continue
                proximo = max(proximo + intervalo, t_video)
                agora_m = time.monotonic()
                if ancora is None:
                    ancora = (agora_m, t_video)
                devido = ancora[0] + (t_video - ancora[1]) + self.ATRASO_S
                if devido < agora_m - 2.0:  # a live atrasou mais que o buffer: recomeça a referência
                    ancora = (agora_m, t_video)
                    devido = agora_m + self.ATRASO_S
                with trava:
                    buffer.append((devido, img))
                    while len(buffer) > self.fps_alvo * 60:  # proteção: no máximo ~1 min guardado
                        buffer.popleft()
                        self.descartados += 1
            cap.release()
            cap = None if self.parar.is_set() else self._reconectar()
        liberador.join(timeout=2)

    def _liberar(self, buffer: deque, trava):
        """Entrega à detecção cada quadro no seu horário previsto."""
        indice, t0, ultimo_t = 0, time.monotonic(), -1.0
        while not self.parar.is_set():
            with trava:
                proximo = buffer[0] if buffer else None
            if proximo is None:
                time.sleep(0.02)
                continue
            espera = proximo[0] - time.monotonic()
            if espera > 0:
                time.sleep(min(espera, 0.05))
                continue
            with trava:
                _, img = buffer.popleft()
            indice += 1
            t = max(time.monotonic() - t0, ultimo_t + 1e-3)
            ultimo_t = t
            q = Quadro(indice, t, agora(), img)
            try:
                self.fila.put_nowait(q)
            except queue.Full:  # detecção mais lenta que a live: descarta o quadro mais velho
                try:
                    self.fila.get_nowait()
                    self.descartados += 1
                except queue.Empty:
                    pass
                self.fila.put_nowait(q)

    def _reconectar(self):
        self.estado = "reconectando"
        inicio_queda, t_queda = agora(), time.monotonic()
        tentativa = 0
        while not self.parar.is_set():
            espera = ESPERAS_RECONEXAO_S[min(tentativa, len(ESPERAS_RECONEXAO_S) - 1)]
            if self.parar.wait(espera if tentativa else 1):
                break
            tentativa += 1
            try:
                url = self.reabrir() if self.reabrir else self.url
            except Exception:
                continue  # site fora do ar / sem internet: tenta de novo mais tarde
            cap = self._abrir(url)
            if cap is not None:
                self.url = url
                self.reconexoes += 1
                self.tempo_sem_video_s += time.monotonic() - t_queda
                self.quedas.append((inicio_queda, agora()))
                self.estado = "rodando"
                return cap
        self.tempo_sem_video_s += time.monotonic() - t_queda
        return None
