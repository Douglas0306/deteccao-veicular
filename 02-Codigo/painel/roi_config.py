"""ROIs desenhadas pelo usuário: modelo de dados e JSON por câmera (sem dependência de Qt).

Cada ROI = duas linhas livres (1 = entrada, 2 = saída) + direção + tipo. O veículo passa pela
ROI quando cruza a linha 1 e depois a linha 2. Cada combinação direção x tipo aparece uma vez,
então há no máximo 8 ROIs por vídeo.
"""
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .caminhos import pasta_config

DIRECOES = ("cima", "baixo", "esquerda", "direita")
TIPOS = ("origem", "destino")
VERSAO_JSON = 1
MIN_COMPRIMENTO_PX = 8


Ponto = tuple[float, float]


@dataclass
class Roi:
    direcao: str
    tipo: str
    linha_entrada: list[Ponto]   # [(x, y), (x, y)] em pixels do snapshot
    linha_saida: list[Ponto]

    @property
    def nome(self) -> str:
        return f"{self.direcao}_{self.tipo}"

    def inverter(self):
        self.linha_entrada, self.linha_saida = self.linha_saida, self.linha_entrada

    def poligono(self) -> list[Ponto]:
        """Área entre as linhas, sem cruzar a si mesma (as pontas são ligadas na ordem certa)."""
        a1, a2 = self.linha_entrada
        b1, b2 = self.linha_saida
        if _segmentos_cruzam(a1, b1, a2, b2):  # ligar a1-b1 e a2-b2 formaria um "laço"
            b1, b2 = b2, b1
        return [a1, a2, b2, b1]

    def problemas(self) -> list[str]:
        erros = []
        for nome, (p, q) in (("linha 1", self.linha_entrada), ("linha 2", self.linha_saida)):
            if _dist(p, q) < MIN_COMPRIMENTO_PX:
                erros.append(f"a {nome} é curta demais")
        if _segmentos_cruzam(*self.linha_entrada, *self.linha_saida):
            erros.append("as linhas 1 e 2 se cruzam; a área entre elas fica indefinida")
        return erros

    def para_json(self) -> dict:
        return {"nome": self.nome, "direcao": self.direcao, "tipo": self.tipo,
                "linha_entrada": [[round(x, 1), round(y, 1)] for x, y in self.linha_entrada],
                "linha_saida": [[round(x, 1), round(y, 1)] for x, y in self.linha_saida]}


@dataclass
class ConfigRois:
    camera_id: str
    fonte: str = ""
    video_id: str = ""
    titulo: str = ""
    resolucao: tuple[int, int] = (0, 0)
    rois: list[Roi] = field(default_factory=list)

    # -------------------------------------------------------- regras
    def combinacoes_livres(self, exceto: Roi | None = None) -> list[tuple[str, str]]:
        usadas = {(r.direcao, r.tipo) for r in self.rois if r is not exceto}
        return [(d, t) for d in DIRECOES for t in TIPOS if (d, t) not in usadas]

    def adicionar(self, roi: Roi):
        if (roi.direcao, roi.tipo) not in self.combinacoes_livres():
            raise ValueError(f"já existe a ROI {roi.nome}")
        self.rois.append(roi)
        self.ordenar()

    def ordenar(self):
        self.rois.sort(key=lambda r: (TIPOS.index(r.tipo), DIRECOES.index(r.direcao)))

    def alertas_sentido(self) -> dict[str, str]:
        """ROIs cuja seta parece invertida: origem deveria apontar para o centro do cruzamento,
        destino para fora. Só alerta (pode haver casos em que faça sentido)."""
        if len(self.rois) < 2:
            return {}
        pts = [p for r in self.rois for p in (*r.linha_entrada, *r.linha_saida)]
        cx, cy = sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)
        alertas = {}
        for r in self.rois:
            (a1, a2), (b1, b2) = r.linha_entrada, r.linha_saida
            m1 = ((a1[0] + a2[0]) / 2, (a1[1] + a2[1]) / 2)
            m2 = ((b1[0] + b2[0]) / 2, (b1[1] + b2[1]) / 2)
            seta = (m2[0] - m1[0], m2[1] - m1[1])
            meio = ((m1[0] + m2[0]) / 2, (m1[1] + m2[1]) / 2)
            ao_centro = (cx - meio[0], cy - meio[1])
            if _dist((0, 0), seta) < 2 or _dist((0, 0), ao_centro) < 2:
                continue
            produto = seta[0] * ao_centro[0] + seta[1] * ao_centro[1]
            if r.tipo == "origem" and produto < 0:
                alertas[r.nome] = "é origem, mas a seta aponta para fora do cruzamento"
            elif r.tipo == "destino" and produto > 0:
                alertas[r.nome] = "é destino, mas a seta aponta para dentro do cruzamento"
        return alertas

    def problemas(self) -> list[str]:
        erros = [f"{r.nome}: {p}" for r in self.rois for p in r.problemas()]
        if not any(r.tipo == "origem" for r in self.rois):
            erros.append("é preciso pelo menos uma ROI de origem")
        if not any(r.tipo == "destino" for r in self.rois):
            erros.append("é preciso pelo menos uma ROI de destino")
        return erros

    # -------------------------------------------------------- arquivo
    def caminho(self, pasta: Path | None = None) -> Path:
        return (pasta or pasta_config()) / f"{self.camera_id}.json"

    def salvar(self, pasta: Path | None = None, snapshot_jpg: bytes | None = None) -> Path:
        destino = self.caminho(pasta)
        destino.parent.mkdir(parents=True, exist_ok=True)
        nome_snap = f"{self.camera_id}_snapshot.jpg"
        if snapshot_jpg is not None:
            (destino.parent / nome_snap).write_bytes(snapshot_jpg)
        dados = {
            "versao": VERSAO_JSON, "camera_id": self.camera_id, "fonte": self.fonte,
            "video_id": self.video_id, "titulo": self.titulo, "resolucao": list(self.resolucao),
            "snapshot": nome_snap, "salvo_em": datetime.now().astimezone().isoformat(timespec="seconds"),
            "rois": [r.para_json() for r in self.rois],
        }
        tmp = destino.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(dados, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(destino)  # grava inteiro ou não grava: nunca deixa um JSON pela metade
        return destino

    @classmethod
    def carregar(cls, caminho, resolucao_atual: tuple[int, int] | None = None) -> "ConfigRois":
        """Lê o JSON; se o vídeo atual tem outra resolução, reescala as linhas."""
        dados = json.loads(Path(caminho).read_text(encoding="utf-8"))
        if dados.get("versao") != VERSAO_JSON or "rois" not in dados:
            raise ValueError("arquivo não é um JSON de ROIs desta aplicação")
        cfg = cls(camera_id=dados["camera_id"], fonte=dados.get("fonte", ""),
                  video_id=dados.get("video_id", ""), titulo=dados.get("titulo", ""),
                  resolucao=tuple(dados.get("resolucao") or (0, 0)))
        sx = sy = 1.0
        if resolucao_atual and all(cfg.resolucao) and tuple(resolucao_atual) != cfg.resolucao:
            sx, sy = resolucao_atual[0] / cfg.resolucao[0], resolucao_atual[1] / cfg.resolucao[1]
            cfg.resolucao = tuple(resolucao_atual)
        for r in dados["rois"]:
            if r["direcao"] not in DIRECOES or r["tipo"] not in TIPOS:
                raise ValueError(f"ROI com direção/tipo inválido: {r.get('nome')}")
            cfg.adicionar(Roi(r["direcao"], r["tipo"],
                              [(x * sx, y * sy) for x, y in r["linha_entrada"]],
                              [(x * sx, y * sy) for x, y in r["linha_saida"]]))
        return cfg


def _dist(p, q) -> float:
    return ((p[0] - q[0]) ** 2 + (p[1] - q[1]) ** 2) ** 0.5


def _lado(a, b, c) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _segmentos_cruzam(p1, p2, q1, q2) -> bool:
    """Cruzamento próprio entre os segmentos p1-p2 e q1-q2 (encostar na ponta não conta)."""
    d1, d2 = _lado(q1, q2, p1), _lado(q1, q2, p2)
    d3, d4 = _lado(p1, p2, q1), _lado(p1, p2, q2)
    return d1 * d2 < 0 and d3 * d4 < 0
