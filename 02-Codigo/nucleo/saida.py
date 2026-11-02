"""Arquivos de saída da coleta: CSVs por intervalo do relógio, série de parados e sessões.

Pastas: {raiz}/{camera_id}/{AAAA-MM-DD}/
  veiculos_{camera_id}_{AAAAMMDD}_{HHMM}.csv   CSV 1, uma linha por veículo
  parados_{camera_id}_{AAAAMMDD}_{HHMM}.csv    CSV 2, parados por segundo em cada ROI de origem
{raiz}/{camera_id}/sessoes_{camera_id}.csv     uma linha por execução (início, fim, quedas...)
{raiz}/{camera_id}/rois_{camera_id}.json       cópia das ROIs usadas

Enquanto o intervalo está aberto o arquivo se chama *.csv.partial (flush a cada 10 s); ao fechar
é renomeado para *.csv. Os intervalos seguem o relógio (com 15 min: 14:00, 14:15, 14:30...).
"""
import csv
import json
import os
import shutil
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

COLUNAS_VEICULOS = [
    "id", "data", "hora", "camera_id", "video_id", "classe", "hora_primeira_deteccao", "hora_ultima_deteccao",
    "roi_origem", "direcao_origem", "hora_origem", "roi_destino", "direcao_destino", "hora_destino",
    "tempo_parado_total_s", "tempo_parado_antes_origem_s", "tempo_movimento_s", "n_paradas",
    "espera_sem_movimento", "maior_tempo_sem_movimento_s", "situacao", "confianca_media",
]
COLUNAS_PARADOS = ["horario", "data", "hora", "camera_id", "roi", "veiculos_parados",
                   "movimento_no_cruzamento", "parado_em_outro_roi"]
# TODO (pedido do usuário): incluir pedestres nas condições de movimento/conflito quando o modelo os detectar.
COLUNAS_SESSOES = [
    "camera_id", "video_id", "fonte", "inicio", "fim", "duracao_s", "motivo_fim", "quadros_processados",
    "quadros_s", "modelo", "processador", "conf_minima", "veiculos_registrados", "completos", "sem_destino", "sem_origem",
    "em_andamento_no_fim", "reconexoes", "tempo_sem_video_s", "intervalo_min",
]
INTERVALOS_MIN = (5, 10, 15, 30, 60)


def iso(ts: datetime | None) -> str:
    return ts.isoformat(timespec="seconds") if ts else ""


def data_br(ts: datetime | None) -> str:
    return f"{ts:%d/%m/%Y}" if ts else ""


def hora_br(ts: datetime | None) -> str:
    return f"{ts:%H:%M:%S}" if ts else ""


def booleano(v: bool) -> str:
    """Como o Excel em português escreve booleanos (VERDADEIRO / FALSO)."""
    return "VERDADEIRO" if v else "FALSO"


def rotulo_roi(nome: str) -> str:
    """'cima_origem' -> 'cima' (no CSV de parados todas as ROIs são de origem)."""
    return nome[:-len("_origem")] if nome.endswith("_origem") else nome


def inicio_intervalo(ts: datetime, minutos: int) -> datetime:
    return ts.replace(minute=ts.minute // minutos * minutos, second=0, microsecond=0)


class _Arquivo:
    def __init__(self, final: Path, colunas):
        self.final = final
        self.parcial = final.with_name(final.name + ".partial")
        final.parent.mkdir(parents=True, exist_ok=True)
        if final.exists():  # mesmo intervalo gravado por outra sessão: continua o arquivo
            os.replace(final, self.parcial)
        novo = not self.parcial.exists()
        self.f = open(self.parcial, "a", newline="", encoding="utf-8")
        self.w = csv.writer(self.f)
        if novo:
            self.w.writerow(colunas)

    def fechar(self):
        self.f.close()
        os.replace(self.parcial, self.final)


class GravadorIntervalo:
    def __init__(self, raiz, camera_id: str, prefixo: str, colunas: list, intervalo_min: int,
                 flush_s: float = 10.0, ao_fechar=None):
        if intervalo_min not in INTERVALOS_MIN:
            raise ValueError(f"intervalo deve ser um de {INTERVALOS_MIN}")
        self.raiz, self.camera_id, self.prefixo = Path(raiz), camera_id, prefixo
        self.colunas, self.minutos, self.flush_s = colunas, intervalo_min, flush_s
        self.ao_fechar = ao_fechar  # callable(inicio_do_intervalo, caminho_final)
        self.abertos: dict[datetime, _Arquivo] = {}
        self.arquivos: list[str] = []
        self.linhas = 0
        self._ultimo_flush = time.monotonic()

    def caminho(self, ini: datetime) -> Path:
        nome = f"{self.prefixo}_{self.camera_id}_{ini:%Y%m%d}_{ini:%H%M}.csv"
        return self.raiz / self.camera_id / f"{ini:%Y-%m-%d}" / nome

    def escrever(self, ts: datetime, linha: list):
        ini = inicio_intervalo(ts, self.minutos)
        arq = self.abertos.get(ini)
        if arq is None:
            arq = self.abertos[ini] = _Arquivo(self.caminho(ini), self.colunas)
            if str(arq.final) not in self.arquivos:
                self.arquivos.append(str(arq.final))
        arq.w.writerow(linha)
        self.linhas += 1

    def _fechar(self, ini):
        arq = self.abertos.pop(ini)
        arq.fechar()
        if self.ao_fechar:
            self.ao_fechar(ini, arq.final)

    def manutencao(self, agora: datetime, carencia_s: float):
        """Fecha intervalos já vencidos (mais a carência) e faz o flush periódico."""
        limite = agora - timedelta(seconds=carencia_s)
        for ini in sorted(i for i in self.abertos if i + timedelta(minutes=self.minutos) <= limite):
            self._fechar(ini)
        if time.monotonic() - self._ultimo_flush >= self.flush_s:
            for a in self.abertos.values():
                a.f.flush()
            self._ultimo_flush = time.monotonic()

    def fechar_todos(self):
        for ini in sorted(self.abertos):
            self._fechar(ini)


def linha_veiculo(r, camera_id: str, video_id: str) -> list:
    referencia = r.ts_origem or r.ts_ini  # data/hora da linha: passagem pela origem (ou 1a detecção)
    return [r.veiculo_id, data_br(referencia), hora_br(referencia), camera_id, video_id, r.classe,
            iso(r.ts_ini), iso(r.ts_fim), r.roi_origem, r.direcao_origem, iso(r.ts_origem), r.roi_destino,
            r.direcao_destino, iso(r.ts_destino), f"{r.tempo_parado_total_s:.1f}",
            f"{r.tempo_parado_antes_origem_s:.1f}", f"{r.tempo_movimento_s:.1f}", r.n_paradas,
            booleano(r.espera_sem_movimento), f"{r.maior_tempo_sem_movimento_s:.1f}", r.situacao,
            f"{r.confianca_media:.3f}"]


class SerieParados:
    """Veículos parados por segundo em cada ROI de origem (forma A), com duas condições:
      movimento_no_cruzamento: algum veículo andando dentro do cruzamento naquele segundo;
      parado_em_outro_roi: algum outro braço também com veículo parado naquele segundo.

    Um segundo só é gravado quando nenhum veículo ainda pode ser atribuído a ele (ou, no
    máximo, depois de espera_max_s). Segundos sem vídeo (stream fora do ar) não são gravados,
    para não parecerem "zero parados"."""

    def __init__(self, gravador: GravadorIntervalo, camera_id: str, rois_origem: list[str],
                 espera_max_s: float = 300.0):
        self.g, self.camera_id, self.rois = gravador, camera_id, list(rois_origem)
        self.espera_max = timedelta(seconds=espera_max_s)
        self.contagem: Counter = Counter()        # (segundo, roi) -> veículos
        self.com_video: set[datetime] = set()
        self.movimento: dict[datetime, bool] = {}  # segundo -> houve veículo andando no cruzamento
        self.ultimo: datetime | None = None       # último segundo gravado
        self.atrasados = 0                        # segundos que chegaram depois de gravados

    def marcar_video(self, ts: datetime, movimento_no_cruzamento: bool = False):
        s = ts.replace(microsecond=0)
        if self.ultimo is None or s > self.ultimo:
            self.com_video.add(s)
            self.movimento[s] = self.movimento.get(s, False) or movimento_no_cruzamento

    def adicionar(self, roi: str, segundos: list[datetime]):
        for s in segundos:
            if self.ultimo is not None and s <= self.ultimo:
                self.atrasados += 1
            else:
                self.contagem[(s, roi)] += 1

    def gravar_ate(self, agora: datetime, menor_pendente: datetime | None, forcar=False):
        limite = agora.replace(microsecond=0) - timedelta(seconds=3)  # parado só se confirma após 2 s
        if menor_pendente is not None and not forcar:
            limite = min(limite, max(menor_pendente.replace(microsecond=0), agora - self.espera_max))
        if forcar:
            limite = agora.replace(microsecond=0) + timedelta(seconds=1)
        for s in sorted(x for x in self.com_video if x < limite):
            n_roi = {roi: self.contagem.pop((s, roi), 0) for roi in self.rois}
            sem_origem = self.contagem.pop((s, "sem_origem"), 0)
            mov = booleano(self.movimento.pop(s, False))
            base = [iso(s), data_br(s), hora_br(s), self.camera_id]
            for roi, n in n_roi.items():
                outro = any(m > 0 for r, m in n_roi.items() if r != roi)
                self.g.escrever(s, base + [rotulo_roi(roi), n, mov, booleano(outro)])
            if sem_origem:
                self.g.escrever(s, base + ["sem_origem", sem_origem, mov, booleano(any(n_roi.values()))])
            self.com_video.discard(s)
            self.ultimo = s


def registrar_sessao(raiz, camera_id: str, dados: dict) -> Path:
    caminho = Path(raiz) / camera_id / f"sessoes_{camera_id}.csv"
    caminho.parent.mkdir(parents=True, exist_ok=True)
    if caminho.exists():  # arquivo de uma versão anterior, com menos colunas: reescreve o cabeçalho
        with open(caminho, encoding="utf-8") as f:
            leitor = csv.DictReader(f)
            antigas, linhas = leitor.fieldnames or [], list(leitor)
        if antigas != COLUNAS_SESSOES:
            with open(caminho, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, COLUNAS_SESSOES, extrasaction="ignore")
                w.writeheader()
                w.writerows(linhas)
    novo = not caminho.exists()
    with open(caminho, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, COLUNAS_SESSOES, extrasaction="ignore")
        if novo:
            w.writeheader()
        w.writerow(dados)
    return caminho


def copiar_rois(raiz, camera_id: str, rois_json: dict, snapshot=None) -> Path:
    """Cópia das ROIs (e do snapshot) usadas na coleta: a pasta de saída fica completa por si só."""
    caminho = Path(raiz) / camera_id / f"rois_{camera_id}.json"
    caminho.parent.mkdir(parents=True, exist_ok=True)
    caminho.write_text(json.dumps(rois_json, ensure_ascii=False, indent=2), encoding="utf-8")
    if snapshot and Path(snapshot).exists():
        shutil.copyfile(snapshot, caminho.parent / f"snapshot_{camera_id}.jpg")
    return caminho
