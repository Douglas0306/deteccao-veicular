"""Lógica de passagem pelas ROIs de duas linhas, tempo parado e fila por braço.

Não depende de vídeo, modelo ou janela: recebe detecções rastreadas (track_id, caixa, classe,
confiança) com o instante e devolve
  - registros de veículo (CSV 1, uma linha por veículo, quando a trilha termina) e
  - atribuições de segundos parados a uma ROI de origem (CSV 2, "forma A": o segundo parado
    só ganha braço quando o veículo cruza a origem; quem nunca cruza vai para "sem_origem").

Regras:
  - passou pela ROI = o ponto de base cruzou a linha 1 e depois a linha 2 (até max_s entre elas);
  - origem = primeira ROI de origem; destino = primeira ROI de destino depois da origem
    (ou a primeira de destino, se a origem não foi vista);
  - parado = velocidade aparente < limiar (alturas de caixa por segundo) por tempo_min_s seguidos.
"""
import math
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import cv2
import numpy as np

SEM_ORIGEM = "sem_origem"


# ---------------------------------------------------------------- geometria
def _lado(p, q, a) -> float:
    return (q[0] - p[0]) * (a[1] - p[1]) - (q[1] - p[1]) * (a[0] - p[0])


def cruzou(a, b, linha) -> float | None:
    """Se o deslocamento a->b atravessa o segmento `linha`, devolve a fração (0..1) do
    deslocamento onde isso acontece. Ponto exatamente sobre a linha conta como lado positivo,
    para um veículo parado em cima dela não "cruzar" a cada tremida da caixa."""
    p, q = linha
    sa, sb = _lado(p, q, a), _lado(p, q, b)
    if (sa >= 0) == (sb >= 0):
        return None
    if _lado(a, b, p) * _lado(a, b, q) > 0:
        return None  # passou pela reta, mas fora do trecho desenhado
    return sa / (sa - sb)


@dataclass
class LinhaRoi:
    nome: str
    direcao: str
    tipo: str             # origem | destino
    entrada: tuple        # ((x, y), (x, y))
    saida: tuple

    @classmethod
    def de_json(cls, r: dict, escala=(1.0, 1.0)):
        sx, sy = escala
        conv = lambda pts: tuple((x * sx, y * sy) for x, y in pts)  # noqa: E731
        return cls(r["nome"], r["direcao"], r["tipo"], conv(r["linha_entrada"]), conv(r["linha_saida"]))


# ---------------------------------------------------------------- estado por veículo
@dataclass
class Passagem:
    roi: LinhaRoi
    t: float
    ts: datetime


@dataclass
class Veiculo:
    track_id: int
    t_ini: float
    ts_ini: datetime
    t_ult: float = 0.0
    ts_ult: datetime | None = None
    ultimo: tuple | None = None                 # último ponto de base
    altura: float = 1.0
    recentes: list = field(default_factory=list)  # (t, x, y, h) dos últimos ~2 s
    votos: Counter = field(default_factory=Counter)
    soma_conf: float = 0.0
    n_det: int = 0
    linha1: dict = field(default_factory=dict)  # nome da ROI -> t em que cruzou a linha 1
    passagens: list = field(default_factory=list)
    origem: Passagem | None = None
    destino: Passagem | None = None
    lento_desde: float | None = None
    lento_desde_ts: datetime | None = None
    trechos: list = field(default_factory=list)  # [(t_a, t_b, ts_a, ts_b)] parados confirmados
    parado: bool = False
    movendo: bool = False                        # velocidade conhecida e acima do limiar
    fila_atribuida: bool = False

    @property
    def classe(self) -> str:
        return self.votos.most_common(1)[0][0]


@dataclass
class Registro:
    """Uma linha do CSV 1."""
    veiculo_id: str
    track_id: int
    classe: str
    ts_ini: datetime
    ts_fim: datetime
    roi_origem: str
    direcao_origem: str
    ts_origem: datetime | None
    roi_destino: str
    direcao_destino: str
    ts_destino: datetime | None
    tempo_parado_total_s: float
    tempo_parado_antes_origem_s: float
    tempo_movimento_s: float
    n_paradas: int
    confianca_media: float
    situacao: str      # completo | sem_destino | sem_origem | em_andamento_no_fim
    maior_tempo_sem_movimento_s: float = 0.0  # maior janela com o cruzamento livre enquanto esperava
    espera_sem_movimento: bool = False        # essa janela >= janela_sem_movimento_s


def _media(pontos, t_a, t_b):
    sel = [p for p in pontos if t_a <= p[0] <= t_b]
    if not sel:
        return None
    n = len(sel)
    return tuple(sum(p[i] for p in sel) / n for i in range(4))


class LogicaPassagens:
    def __init__(self, rois: list[LinhaRoi], prefixo_id: str, vel_limiar=0.10, tempo_parado_min=2.0,
                 memoria_s=1.5, traj_min_s=1.0, max_entre_linhas_s=15.0, religar_s=2.5,
                 religar_raio_alturas=3.0, janela_sem_movimento_s=5.0):
        self.rois = rois
        # "cruzamento" = menor polígono convexo que contém todas as linhas das ROIs
        pontos = np.array([p for r in rois for p in (*r.entrada, *r.saida)], np.float32)
        self.area_cruzamento = cv2.convexHull(pontos)
        self.janela_sem_movimento_s = janela_sem_movimento_s
        self.movimento_agora = False                 # algum veículo andando no cruzamento neste quadro
        self.linha_do_tempo: deque = deque(maxlen=20000)  # (t, movimento_no_cruzamento) por quadro
        self.origens = [r for r in rois if r.tipo == "origem"]
        self.prefixo_id = prefixo_id
        self.vel_limiar, self.tempo_parado_min = vel_limiar, tempo_parado_min
        self.traj_min_s, self.max_entre_linhas_s = traj_min_s, max_entre_linhas_s
        self.religar_s, self.religar_raio = religar_s, religar_raio_alturas
        self.perda_s = memoria_s + 0.5                     # sumido há mais que isso = perdido
        self.espera_s = max(self.perda_s, religar_s + 0.5)  # quanto esperar uma continuação
        self.veiculos: dict[int, Veiculo] = {}
        self._atrib_extra: list[tuple] = []                # filas descobertas numa religação
        self.descartados = Counter()
        self.religados = 0
        self.passagens_por_roi = Counter()

    # ---------------------------------------------------------- por quadro
    def atualizar(self, t: float, ts: datetime, deteccoes) -> tuple[list[Registro], list[tuple]]:
        """Devolve (registros de veículos encerrados, atribuições de fila).
        Atribuição de fila = (roi_nome, [segundos datetime]) com os segundos parados de um veículo."""
        atrib = []
        movimento = False
        for d in deteccoes:
            v = self.veiculos.get(d.track_id)
            if v is None:
                v = self.veiculos[d.track_id] = Veiculo(d.track_id, t, ts)
            a = self._atualizar(v, t, ts, d)
            if a:
                atrib.append(a)
            if v.movendo and not movimento and self.no_cruzamento(v.ultimo):
                movimento = True
        self.movimento_agora = movimento
        self.linha_do_tempo.append((t, movimento))
        registros = []
        perdidos = sorted((k for k, v in self.veiculos.items() if t - v.t_ult > self.espera_s),
                          key=lambda k: self.veiculos[k].t_ult)
        for k in perdidos:
            v = self.veiculos.pop(k)
            if self._religar(v):
                continue
            r, a = self._encerrar(v, no_fim=False)
            if r:
                registros.append(r)
            if a:
                atrib.append(a)
        atrib += self._atrib_extra
        self._atrib_extra = []
        return registros, atrib

    def encerrar_todos(self) -> tuple[list[Registro], list[tuple]]:
        """Fim da sessão: veículo ainda à vista sai como 'em_andamento_no_fim'."""
        registros, atrib = [], []
        t_fim = max((v.t_ult for v in self.veiculos.values()), default=0.0)
        for k, v in sorted(self.veiculos.items(), key=lambda kv: kv[1].t_ult):
            del self.veiculos[k]
            if t_fim - v.t_ult > self.perda_s and self._religar(v):
                continue
            r, a = self._encerrar(v, no_fim=t_fim - v.t_ult <= self.perda_s)
            if r:
                registros.append(r)
            if a:
                atrib.append(a)
        atrib += self._atrib_extra
        self._atrib_extra = []
        return registros, atrib

    def no_cruzamento(self, ponto) -> bool:
        return cv2.pointPolygonTest(self.area_cruzamento, (float(ponto[0]), float(ponto[1])), False) >= 0

    def maior_janela_sem_movimento(self, t_a: float, t_b: float) -> float:
        """Maior trecho contínuo, entre t_a e t_b, sem nenhum veículo andando no cruzamento."""
        maior, inicio = 0.0, None
        for t, mov in self.linha_do_tempo:
            if t < t_a:
                continue
            if t > t_b:
                break
            if mov:
                inicio = None
            else:
                inicio = t if inicio is None else inicio
                maior = max(maior, t - inicio)
        return maior

    def menor_segundo_pendente(self) -> datetime | None:
        """Segundo parado mais antigo que ainda pode ganhar um braço (veículo sem origem)."""
        cands = []
        for v in self.veiculos.values():
            if v.origem is not None:
                continue
            if v.trechos:
                cands.append(v.trechos[0][2])
            elif v.lento_desde_ts is not None:
                cands.append(v.lento_desde_ts)
        return min(cands) if cands else None

    def _atualizar(self, v: Veiculo, t, ts, d):
        x, y = d.base
        anterior = v.ultimo
        v.t_ult, v.ts_ult, v.ultimo, v.altura = t, ts, (x, y), max(1.0, d.altura)
        v.votos[d.classe] += 1
        v.soma_conf += d.conf
        v.n_det += 1
        v.recentes.append((t, x, y, d.altura))
        while v.recentes and t - v.recentes[0][0] > 2.0:
            v.recentes.pop(0)

        # ---- velocidade aparente e parado
        lento = False
        v.movendo = False
        fim = _media(v.recentes, t - 0.3, t)
        ini = _media(v.recentes, t - 1.3, t - 0.7)
        if fim and ini and fim[0] - ini[0] > 0.3:
            h = max(1.0, (fim[3] + ini[3]) / 2)
            vel = math.hypot(fim[1] - ini[1], fim[2] - ini[2]) / (fim[0] - ini[0]) / h
            lento = vel < self.vel_limiar
            v.movendo = not lento
        if lento:
            if v.lento_desde is None:
                v.lento_desde, v.lento_desde_ts = t, ts
            v.parado = t - v.lento_desde >= self.tempo_parado_min
        else:
            self._fechar_lento(v, t, ts)

        # ---- cruzamento das linhas (podem acontecer as duas no mesmo passo)
        atrib = None
        if anterior is not None:
            eventos = []
            for r in self.rois:
                for qual, linha in ((1, r.entrada), (2, r.saida)):
                    u = cruzou(anterior, (x, y), linha)
                    if u is not None:
                        eventos.append((u, qual, r))
            for u, qual, r in sorted(eventos, key=lambda e: e[0]):
                t_x = v.t_ult - (1 - u) * (t - (v.recentes[-2][0] if len(v.recentes) > 1 else t))
                if qual == 1:
                    v.linha1[r.nome] = t_x
                elif r.nome in v.linha1 and t_x - v.linha1.pop(r.nome) <= self.max_entre_linhas_s:
                    atrib = self._passou(v, r, t_x, ts - timedelta(seconds=t - t_x)) or atrib
        return atrib

    def _passou(self, v: Veiculo, r: LinhaRoi, t, ts):
        p = Passagem(r, t, ts)
        v.passagens.append(p)
        self.passagens_por_roi[r.nome] += 1
        if r.tipo == "origem" and v.origem is None:
            v.origem = p
            if v.destino is not None and v.destino.t <= t:
                v.destino = None  # destino visto antes da origem não é o destino desta travessia
            return self._atribuir_fila(v, r.nome)
        if r.tipo == "destino" and v.destino is None:
            v.destino = p
        return None

    def _fechar_lento(self, v, t, ts):
        if v.lento_desde is not None and t - v.lento_desde >= self.tempo_parado_min:
            v.trechos.append((v.lento_desde, t, v.lento_desde_ts, ts))
        v.lento_desde = v.lento_desde_ts = None
        v.parado = False

    def _atribuir_fila(self, v: Veiculo, roi_nome: str):
        """Segundos inteiros (relógio) em que o veículo estava parado há pelo menos tempo_parado_min,
        antes da origem. Ex.: parou às 14:00:00 e saiu às 14:00:06 -> conta 14:00:02 a 14:00:06
        (cada segundo "olha" os 2 anteriores, como na tabela combinada com o usuário)."""
        if v.fila_atribuida:
            return None
        v.fila_atribuida = True
        limite = v.origem.ts if v.origem else None
        trechos = list(v.trechos)
        if v.lento_desde is not None and v.t_ult - v.lento_desde >= self.tempo_parado_min:
            trechos.append((v.lento_desde, v.t_ult, v.lento_desde_ts, v.ts_ult))
        segundos = set()
        for _, _, ts_a, ts_b in trechos:
            fim = min(ts_b, limite) if limite else ts_b
            s = ts_a + timedelta(seconds=self.tempo_parado_min)
            if s.microsecond:  # primeiro segundo cheio em que já está parado há tempo_parado_min
                s = s.replace(microsecond=0) + timedelta(seconds=1)
            while s <= fim:
                segundos.add(s)
                s += timedelta(seconds=1)
        return (roi_nome, sorted(segundos)) if segundos else None

    # ---------------------------------------------------------- religação
    def _religar(self, a: Veiculo) -> bool:
        """Trilha que some sem destino e "renasce" logo adiante com outro ID = mesmo veículo."""
        if a.destino is not None or a.ultimo is None:
            return False
        fim = _media(a.recentes, a.t_ult - 0.3, a.t_ult)
        ini = _media(a.recentes, a.t_ult - 1.3, a.t_ult - 0.7)
        vx = vy = 0.0
        if fim and ini and fim[0] - ini[0] > 0.3:
            vx, vy = (fim[1] - ini[1]) / (fim[0] - ini[0]), (fim[2] - ini[2]) / (fim[0] - ini[0])
        melhor, melhor_d = None, None
        for b in self.veiculos.values():
            dt = b.t_ini - a.t_ult
            if not 0 < dt <= self.religar_s or (a.origem is not None and b.origem is not None):
                continue
            px, py = a.ultimo[0] + vx * dt, a.ultimo[1] + vy * dt
            bx, by = b.recentes[0][1], b.recentes[0][2]
            dist = math.hypot(bx - px, by - py)
            if dist <= self.religar_raio * max(a.altura, b.altura) and (melhor_d is None or dist < melhor_d):
                melhor, melhor_d = b, dist
        if melhor is None:
            return False
        self._fechar_lento(a, a.t_ult, a.ts_ult)
        b = melhor
        if a.origem is None and b.origem is not None and a.trechos:
            # a parte antiga ficou parada antes de a nova cruzar a origem: é fila desse braço
            antigo = Veiculo(a.track_id, a.t_ini, a.ts_ini, trechos=a.trechos, origem=b.origem)
            extra = self._atribuir_fila(antigo, b.origem.roi.nome)
            if extra:
                self._atrib_extra.append(extra)
        b.track_id = a.track_id
        b.t_ini, b.ts_ini = a.t_ini, a.ts_ini
        b.votos = a.votos + b.votos
        b.soma_conf += a.soma_conf
        b.n_det += a.n_det
        b.passagens = a.passagens + b.passagens
        b.origem = a.origem or b.origem
        b.destino = b.destino if b.destino and (not b.origem or b.destino.t > b.origem.t) else a.destino
        b.trechos = a.trechos + b.trechos
        b.fila_atribuida = a.fila_atribuida or b.fila_atribuida
        for nome, tl in a.linha1.items():  # cruzou a linha 1 antes de sumir: vale para a continuação
            b.linha1.setdefault(nome, tl)
        self.religados += 1
        return True

    # ---------------------------------------------------------- fim da trilha
    def _encerrar(self, v: Veiculo, no_fim: bool):
        self._fechar_lento(v, v.t_ult, v.ts_ult)
        if v.t_ult - v.t_ini < self.traj_min_s:
            self.descartados["curta"] += 1
            return None, None
        atrib = None
        if v.origem is None and v.trechos and not v.fila_atribuida:
            atrib = self._atribuir_fila(v, SEM_ORIGEM)  # parou e nunca entrou por um braço
        if v.origem is None and v.destino is None:
            self.descartados["sem_roi"] += 1
            return None, atrib
        parado_total = sum(b - a for a, b, _, _ in v.trechos)
        antes = 0.0
        limite = v.origem.t if v.origem is not None else float("inf")
        if v.origem is not None:
            antes = sum(max(0.0, min(b, v.origem.t) - a) for a, b, _, _ in v.trechos if a < v.origem.t)
        # enquanto esperava (antes da origem), quanto tempo seguido o cruzamento ficou sem ninguém andando
        livre = max((self.maior_janela_sem_movimento(a, min(b, limite)) for a, b, _, _ in v.trechos if a < limite),
                    default=0.0)
        duracao = v.t_ult - v.t_ini
        if v.origem and v.destino:
            situacao = "completo"
        elif no_fim:
            situacao = "em_andamento_no_fim"
        else:
            situacao = "sem_destino" if v.origem else "sem_origem"
        o, d = v.origem, v.destino
        r = Registro(
            veiculo_id=f"{self.prefixo_id}-{v.track_id:06d}", track_id=v.track_id, classe=v.classe,
            ts_ini=v.ts_ini, ts_fim=v.ts_ult,
            roi_origem=o.roi.nome if o else "", direcao_origem=o.roi.direcao if o else "",
            ts_origem=o.ts if o else None,
            roi_destino=d.roi.nome if d else "", direcao_destino=d.roi.direcao if d else "",
            ts_destino=d.ts if d else None,
            tempo_parado_total_s=round(parado_total, 1), tempo_parado_antes_origem_s=round(antes, 1),
            tempo_movimento_s=round(max(0.0, duracao - parado_total), 1), n_paradas=len(v.trechos),
            confianca_media=round(v.soma_conf / v.n_det, 3), situacao=situacao,
            maior_tempo_sem_movimento_s=round(livre, 1),
            espera_sem_movimento=livre >= self.janela_sem_movimento_s,
        )
        return r, atrib
