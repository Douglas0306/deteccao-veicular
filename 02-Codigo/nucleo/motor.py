"""Motor da coleta: leitor -> detecção + rastreamento -> passagens -> CSVs.

Usado pela aba 4 do painel (e pelo coletar.py, sem janela). Roda em threads próprias;
a janela recebe a prévia anotada e o estado por callbacks.
"""
import queue
import threading
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import cv2

from .detector import DetectorONNX, Rastreador
from .leitor import Leitor, agora
from .passagens import LinhaRoi, LogicaPassagens
from .saida import (COLUNAS_PARADOS, COLUNAS_VEICULOS, GravadorIntervalo, SerieParados, copiar_rois,
                    iso, linha_veiculo, registrar_sessao)

META_QUADROS_S = 5.0


class _Graficos(threading.Thread):
    """Gera os PNGs fora da thread da coleta: um gráfico lento nunca atrasa a detecção."""

    def __init__(self, camera_id: str, janela_livre_s: float, snapshot=None, rois=None):
        super().__init__(name="graficos", daemon=True)
        self.camera_id, self.janela_livre_s = camera_id, janela_livre_s
        self.snapshot, self.rois = snapshot, rois or []
        self.fila: queue.Queue = queue.Queue()
        self.gerados = 0
        self.erro = ""

    def pedir(self, veiculos_csv: list, parados_csv: list, pasta: Path, prefixo: str, periodo: str,
              filtro_veiculos=None, filtro_parados=None):
        self.fila.put((veiculos_csv, parados_csv, pasta, prefixo, periodo, filtro_veiculos, filtro_parados))

    def encerrar(self):
        self.fila.put(None)
        self.join(timeout=120)

    def run(self):
        from . import graficos  # matplotlib só é carregado quando há gráfico a fazer
        cena = None
        if self.snapshot:  # sem snapshot: gráficos sem legenda e sem os dois mapas
            try:
                cena = graficos.Cena(self.snapshot, self.rois)
            except Exception as e:
                self.erro = f"gráficos: não foi possível ler o snapshot ({e})"
        while True:
            item = self.fila.get()
            if item is None:
                break
            veic, par, pasta, prefixo, periodo, fv, fp = item
            try:
                v, p = graficos.ler_csv(veic), graficos.ler_csv(par)
                v = [x for x in v if fv(x)] if fv else v
                p = [x for x in p if fp(x)] if fp else p
                self.gerados += len(graficos.gerar(v, p, pasta, prefixo, self.camera_id, periodo,
                                                   self.janela_livre_s, cena=cena))
            except Exception as e:  # gráfico com problema não pode derrubar a coleta
                self.erro = f"gráficos ({prefixo}): {type(e).__name__}: {e}"


def parametros_rastreamento(conf_minima: float) -> dict:
    """Só detecção com confiança >= conf_minima abre um veículo novo; abaixo disso (até 40% dela)
    a detecção apenas mantém um veículo já seguido (ex.: parcialmente encoberto)."""
    return {"memoria_s": 1.5, "conf_ativacao": conf_minima, "conf_alta": conf_minima,
            "iou_min": 0.10, "quadros_confirmacao": 2}


@dataclass
class ConfigColeta:
    camera_id: str
    video_id: str
    fonte: str                     # o que o usuário digitou (link ou arquivo)
    stream_url: str
    ao_vivo: bool
    rois_json: dict                # conteúdo do JSON salvo na aba 2
    pasta_saida: str
    modelo: str
    snapshot: str | None = None    # imagem salva com as ROIs: legenda e mapas dos gráficos
    intervalo_min: int = 15
    modo_duracao: str = "parar"    # parar | minutos | horario
    minutos: float = 60
    horario: datetime | None = None
    video_anotado: bool = False
    imgsz: int = 960
    fps_alvo: float = 6.0
    conf_minima: float = 0.25      # confiança mínima para considerar um veículo
    dispositivo: str = "cpu"       # cpu | gpu (DirectML)
    vel_limiar: float = 0.10       # alturas de caixa por segundo
    tempo_parado_min: float = 2.0
    janela_sem_movimento_s: float = 5.0  # CSV 1: espera com cruzamento livre por >= isto
    graficos: bool = True          # PNGs a cada intervalo fechado e no fim da execução
    inicio_video: datetime | None = None  # arquivo: hora de início do vídeo (do nome), senão agora
    reabrir: object = None         # callable -> nova URL do stream (live)


@dataclass
class Estado:
    estado: str = "iniciando"
    inicio: datetime | None = None
    decorrido_s: float = 0.0
    horario_video: datetime | None = None
    quadros: int = 0
    quadros_s: float = 0.0
    veiculos_na_tela: int = 0
    registros: int = 0
    situacoes: Counter = field(default_factory=Counter)
    passagens: Counter = field(default_factory=Counter)
    parados_agora: int = 0
    reconexoes: int = 0
    tempo_sem_video_s: float = 0.0
    arquivo_veiculos: str = ""
    arquivo_parados: str = ""
    aviso: str = ""
    erro: str = ""
    motivo_fim: str = ""
    progresso: float | None = None  # 0..1 em arquivo/vídeo gravado
    provedor: str = ""
    aviso_fixo: str = ""            # ex.: GPU falhou, voltou para a CPU
    graficos_gerados: int = 0


class Motor:
    def __init__(self, cfg: ConfigColeta, ao_preview=None, ao_estado=None, ao_fim=None,
                 preview_s: float = 0.12):  # prévia até ~8x/s (a live agora chega em ritmo contínuo)
        self.cfg = cfg
        self.ao_preview, self.ao_estado, self.ao_fim = ao_preview, ao_estado, ao_fim
        self.preview_s = preview_s
        self.parar_evento = threading.Event()
        self.estado = Estado()
        self._thread = threading.Thread(target=self._executar, name="motor", daemon=True)
        self._motivo_usuario = False

    def iniciar(self):
        self._thread.start()

    def parar(self):
        self._motivo_usuario = True
        self.parar_evento.set()

    def rodando(self) -> bool:
        return self._thread.is_alive()

    def aguardar(self, timeout=None):
        self._thread.join(timeout)

    # ---------------------------------------------------------- execução
    def _executar(self):
        c, e = self.cfg, self.estado
        e.inicio = agora()
        leitor = None
        try:
            detector = DetectorONNX(c.modelo, c.imgsz, max(0.05, c.conf_minima * 0.4), 0.5,
                                    dispositivo=c.dispositivo)
            e.provedor = "GPU (DirectML)" if detector.provedor == "DmlExecutionProvider" else "CPU"
            e.aviso_fixo = detector.aviso
            rastreador = Rastreador(parametros_rastreamento(c.conf_minima), c.fps_alvo, detector)
            # live: cabe um bloco HLS inteiro (~5 s); a detecção consome enquanto chega o próximo
            fila = queue.Queue(maxsize=max(8, int(c.fps_alvo * 12)) if c.ao_vivo else 8)
            inicio_video = c.inicio_video if not c.ao_vivo else None
            leitor = Leitor(c.stream_url, c.ao_vivo, c.fps_alvo, fila, self.parar_evento,
                            inicio=inicio_video or e.inicio, reabrir=c.reabrir)
            leitor.start()
            self._loop(detector, rastreador, leitor, fila)
        except Exception as ex:
            e.erro = f"{type(ex).__name__}: {ex}"
            e.motivo_fim = "erro"
            self.parar_evento.set()
        finally:
            e.estado = "encerrado"
            self._avisar_estado(leitor)
            if self.ao_fim:
                self.ao_fim(e)

    def _loop(self, detector, rastreador, leitor, fila):
        c, e = self.cfg, self.estado
        rj = c.rois_json
        logica = None
        graf = self._graf = (_Graficos(c.camera_id, c.janela_sem_movimento_s, c.snapshot, rj.get("rois"))
                             if c.graficos else None)
        if graf is not None:
            graf.start()
        g_veic = GravadorIntervalo(c.pasta_saida, c.camera_id, "veiculos", COLUNAS_VEICULOS, c.intervalo_min)

        def intervalo_fechado(ini, caminho_parados):
            """O arquivo de parados é o último a fechar (espera a fila ganhar braço): hora dos gráficos."""
            if graf is not None:
                fim = ini + timedelta(minutes=c.intervalo_min)
                graf.pedir([g_veic.caminho(ini)], [caminho_parados], caminho_parados.parent / "graficos",
                           f"intervalo_{ini:%H%M}", f"{ini:%d/%m/%Y %H:%M}–{fim:%H:%M}")

        g_par = GravadorIntervalo(c.pasta_saida, c.camera_id, "parados", COLUNAS_PARADOS, c.intervalo_min,
                                  ao_fechar=intervalo_fechado)
        serie = None
        primeiro_ts = None
        recentes = deque()  # horários (relógio) dos últimos quadros processados
        copiar_rois(c.pasta_saida, c.camera_id, rj, c.snapshot)
        escritor = None
        t0 = time.monotonic()
        ultimo_preview = ultimo_estado = 0.0
        ultimo_ts = None
        e.estado = "conectando"
        self._avisar_estado(leitor)
        while True:
            try:
                q = fila.get(timeout=1.0)
            except queue.Empty:
                e.estado = leitor.estado
                self._avisar_estado(leitor)
                if self._deve_parar(e, None):
                    break
                continue
            if q is None:
                if leitor.erro:
                    raise RuntimeError(f"leitura do vídeo: {leitor.erro}")
                if leitor.fim_do_video and not e.motivo_fim:
                    e.motivo_fim = "fim_do_video"
                break
            if logica is None:  # 1o quadro: escala das ROIs e identificador da sessão
                primeiro_ts = q.ts
                h, w = q.imagem.shape[:2]
                rw, rh = rj.get("resolucao") or (w, h)
                escala = (w / rw, h / rh) if rw and rh else (1.0, 1.0)
                rois = [LinhaRoi.de_json(r, escala) for r in rj["rois"]]
                logica = LogicaPassagens(rois, q.ts.strftime("%Y%m%d-%H%M%S"), c.vel_limiar, c.tempo_parado_min,
                                         janela_sem_movimento_s=c.janela_sem_movimento_s)
                serie = SerieParados(g_par, c.camera_id, [r.nome for r in rois if r.tipo == "origem"])
                if c.video_anotado:
                    destino = (Path(c.pasta_saida) / c.camera_id / f"{q.ts:%Y-%m-%d}"
                               / f"anotado_{c.camera_id}_{q.ts:%Y%m%d_%H%M%S}.mp4")
                    destino.parent.mkdir(parents=True, exist_ok=True)
                    escritor = cv2.VideoWriter(str(destino), cv2.VideoWriter_fourcc(*"mp4v"), c.fps_alvo, (w, h))
            e.estado = "rodando"
            xyxy, conf, ids = detector.detectar(q.imagem)
            if detector.aviso != e.aviso_fixo:  # a GPU falhou no meio e a coleta seguiu na CPU
                e.aviso_fixo = detector.aviso
                e.provedor = "GPU (DirectML)" if detector.provedor == "DmlExecutionProvider" else "CPU"
            veiculos, _ = rastreador.atualizar(xyxy, conf, ids, q.t)
            registros, atrib = logica.atualizar(q.t, q.ts, veiculos)
            self._gravar(registros, atrib, g_veic, serie)
            serie.marcar_video(q.ts, logica.movimento_agora)
            serie.gravar_ate(q.ts, logica.menor_segundo_pendente())
            g_veic.manutencao(q.ts, 30)
            g_par.manutencao(q.ts, serie.espera_max.total_seconds() + 30)
            ultimo_ts = q.ts

            e.quadros += 1
            e.decorrido_s = time.monotonic() - t0
            if c.ao_vivo:  # ritmo dos últimos 10 s (o início tem ~6 s de buffer enchendo, sem quadros)
                recentes.append(time.monotonic())
                while recentes[0] < recentes[-1] - 10:
                    recentes.popleft()
                if len(recentes) > 1:
                    e.quadros_s = (len(recentes) - 1) / max(1e-6, recentes[-1] - recentes[0])
            e.horario_video = q.ts
            e.veiculos_na_tela = len(veiculos)
            e.parados_agora = sum(1 for d in veiculos if logica.veiculos.get(d.track_id)
                                  and logica.veiculos[d.track_id].parado)
            e.passagens = Counter(logica.passagens_por_roi)
            if leitor.duracao_video_s:
                e.progresso = min(1.0, q.t / leitor.duracao_video_s)
            agora_m = time.monotonic()
            precisa_preview = self.ao_preview and agora_m - ultimo_preview >= self.preview_s
            if escritor is not None or precisa_preview:
                img = anotar(q.imagem, logica, veiculos, e)
                if escritor is not None:
                    escritor.write(img)
                if precisa_preview:
                    ultimo_preview = agora_m
                    self.ao_preview(img)
            if agora_m - ultimo_estado >= 1.0:
                ultimo_estado = agora_m
                if not c.ao_vivo:  # em arquivo, "quadros/s" = velocidade de processamento
                    e.quadros_s = e.quadros / max(1e-6, e.decorrido_s)
                self._avisar_estado(leitor, g_veic, g_par)
            if self._deve_parar(e, q):
                break

        # ---- encerramento: fecha tudo direito, mesmo parado pelo usuário
        e.estado = "finalizando"
        self.parar_evento.set()
        self._avisar_estado(leitor)
        if logica is not None:
            registros, atrib = logica.encerrar_todos()
            self._gravar(registros, atrib, g_veic, serie)
            serie.gravar_ate(ultimo_ts or agora(), None, forcar=True)
        g_veic.fechar_todos()
        g_par.fechar_todos()  # dispara os gráficos do último intervalo
        if escritor is not None:
            escritor.release()
        if graf is not None:
            if logica is not None and primeiro_ts is not None:  # resumo da execução inteira
                prefixo_id, ini_iso, fim_iso = logica.prefixo_id, iso(primeiro_ts), iso(ultimo_ts)
                fmt_fim = "%H:%M:%S" if ultimo_ts.date() == primeiro_ts.date() else "%d/%m/%Y %H:%M:%S"
                graf.pedir(list(g_veic.arquivos), list(g_par.arquivos),
                           Path(c.pasta_saida) / c.camera_id / f"{primeiro_ts:%Y-%m-%d}" / "graficos",
                           f"sessao_{primeiro_ts:%H%M%S}",
                           f"execução {primeiro_ts:%d/%m/%Y %H:%M:%S}–{ultimo_ts.strftime(fmt_fim)}",
                           filtro_veiculos=lambda r: r["id"].startswith(prefixo_id),
                           filtro_parados=lambda r: ini_iso <= r["horario"] <= fim_iso)
            graf.encerrar()
            e.graficos_gerados = graf.gerados
            if graf.erro:
                e.aviso_fixo = (e.aviso_fixo + " " if e.aviso_fixo else "") + graf.erro
        leitor.join(timeout=5)
        if not e.motivo_fim:
            e.motivo_fim = "parado_pelo_usuario" if self._motivo_usuario else "encerrado"
        fim = agora()
        registrar_sessao(c.pasta_saida, c.camera_id, {
            "camera_id": c.camera_id, "video_id": c.video_id, "fonte": c.fonte, "inicio": iso(e.inicio),
            "fim": iso(fim), "duracao_s": round((fim - e.inicio).total_seconds(), 1),
            "motivo_fim": e.motivo_fim, "quadros_processados": e.quadros, "quadros_s": round(e.quadros_s, 1),
            "modelo": Path(c.modelo).name, "processador": e.provedor, "conf_minima": c.conf_minima,
            "veiculos_registrados": e.registros,
            "completos": e.situacoes["completo"], "sem_destino": e.situacoes["sem_destino"],
            "sem_origem": e.situacoes["sem_origem"], "em_andamento_no_fim": e.situacoes["em_andamento_no_fim"],
            "reconexoes": leitor.reconexoes, "tempo_sem_video_s": round(leitor.tempo_sem_video_s, 1),
            "intervalo_min": c.intervalo_min,
        })

    def _gravar(self, registros, atrib, g_veic, serie):
        for r in registros:
            g_veic.escrever(r.ts_fim, linha_veiculo(r, self.cfg.camera_id, self.cfg.video_id))
            self.estado.registros += 1
            self.estado.situacoes[r.situacao] += 1
        for roi, segundos in atrib:
            serie.adicionar(roi, segundos)

    def _deve_parar(self, e: Estado, q) -> bool:
        c = self.cfg
        if self.parar_evento.is_set():
            return True
        if c.modo_duracao == "minutos":
            decorrido = q.t if (q is not None and not c.ao_vivo) else e.decorrido_s
            if decorrido >= c.minutos * 60:
                e.motivo_fim = "duracao_atingida"
                return True
        if c.modo_duracao == "horario" and c.horario is not None:
            ref = q.ts if (q is not None and not c.ao_vivo) else agora()
            if ref >= c.horario:
                e.motivo_fim = "horario_atingido"
                return True
        return False

    def _avisar_estado(self, leitor, g_veic=None, g_par=None):
        e = self.estado
        if leitor is not None:
            e.reconexoes = leitor.reconexoes
            e.tempo_sem_video_s = leitor.tempo_sem_video_s
            if leitor.estado == "reconectando":
                e.estado = "reconectando"
        if g_veic is not None and g_veic.arquivos:
            e.arquivo_veiculos = g_veic.arquivos[-1]
        if g_par is not None and g_par.arquivos:
            e.arquivo_parados = g_par.arquivos[-1]
        if getattr(self, "_graf", None) is not None:
            e.graficos_gerados = self._graf.gerados
        e.aviso = e.aviso_fixo
        if self.cfg.ao_vivo and e.quadros > 20 and e.quadros_s < min(META_QUADROS_S, self.cfg.fps_alvo * 0.8):
            e.aviso = (e.aviso + " " if e.aviso else "") + (
                f"Processando {e.quadros_s:.1f} quadros/s: abaixo do necessário, veículos rápidos podem ser perdidos.")
        if self.ao_estado:
            self.ao_estado(e)


# ---------------------------------------------------------------- desenho da prévia
_VERDE, _VERMELHO, _BRANCO = (64, 204, 46), (54, 65, 255), (255, 255, 255)


def _texto(img, txt, org, cor=_BRANCO, esc=0.45):
    cv2.putText(img, txt, org, cv2.FONT_HERSHEY_SIMPLEX, esc, (0, 0, 0), 3, cv2.LINE_AA)
    cv2.putText(img, txt, org, cv2.FONT_HERSHEY_SIMPLEX, esc, cor, 1, cv2.LINE_AA)


def anotar(imagem, logica: LogicaPassagens, veiculos, e: Estado):
    img = imagem.copy()
    for r in logica.rois:
        a, b = (tuple(map(int, p)) for p in r.entrada)
        c, d = (tuple(map(int, p)) for p in r.saida)
        cv2.line(img, a, b, _VERDE, 2, cv2.LINE_AA)
        cv2.line(img, c, d, _VERMELHO, 2, cv2.LINE_AA)
        mx, my = (a[0] + b[0] + c[0] + d[0]) // 4, (a[1] + b[1] + c[1] + d[1]) // 4
        _texto(img, f"{r.nome} {e.passagens.get(r.nome, 0)}", (mx - 40, my), _BRANCO, 0.42)
    for det in veiculos:
        v = logica.veiculos.get(det.track_id)
        x1, y1, x2, y2 = map(int, det.xyxy)
        parado = bool(v and v.parado)
        cor = (0, 0, 255) if parado else (255, 200, 0)
        cv2.rectangle(img, (x1, y1), (x2, y2), cor, 2 if parado else 1)
        rotulo = str(v.track_id if v else det.track_id)
        if v and v.origem:
            rotulo += " " + v.origem.roi.direcao
        if parado:
            rotulo += " P"
        _texto(img, rotulo, (x1, max(12, y1 - 4)), cor, 0.42)
    hud = [f"{e.horario_video:%d/%m %H:%M:%S}" if e.horario_video else "",
           f"veiculos {e.veiculos_na_tela}  parados {e.parados_agora}  registros {e.registros}"]
    cv2.rectangle(img, (0, 0), (360, 44), (0, 0, 0), -1)
    for i, ln in enumerate(hud):
        _texto(img, ln, (6, 17 + 19 * i), _BRANCO, 0.5)
    return img
