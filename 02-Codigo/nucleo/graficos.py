"""Gráficos PNG de um período a partir dos CSVs de veículos e de parados.

Gerados a cada intervalo de arquivo fechado e, no fim da execução, para a sessão inteira:
  1 od_heatmap            origem x destino (veículos completos)
  2 mapa_fluxos           os mesmos fluxos desenhados sobre a imagem, como "canos"   (precisa do snapshot)
  3 parado_x_movimento    proporção e média do tempo parado e em movimento por braço de origem
  4 volume                veículos por minuto e braço de origem
  5 fila                  veículos parados em cada braço (gráfico de faixa)
  6 fila_mapa             fila média e máxima de cada braço sobre a imagem          (precisa do snapshot)
  7 espera_antes_origem   distribuição do tempo parado antes de entrar (boxplot)
  8 espera_livre          veículos que esperaram com o cruzamento livre por >= N s
  9 qualidade             registros por situação (completo, sem destino...)
Com o snapshot da câmera, os gráficos por braço ganham uma "Legenda": mini-mapa com cada braço
na sua cor. Usa matplotlib sem janela (backend Agg), seguro fora da thread da interface.
"""
import csv
import math
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
from matplotlib.patches import Polygon
from matplotlib.ticker import MaxNLocator

BRACOS = ["cima", "direita", "baixo", "esquerda"]
CORES = {"cima": "#1f77b4", "direita": "#17becf", "baixo": "#ff7f0e", "esquerda": "#9467bd",
         "sem_origem": "#9e9e9e"}
SITUACOES = ["completo", "sem_destino", "sem_origem", "em_andamento_no_fim"]


def ler_csv(caminhos) -> list[dict]:
    linhas = []
    for c in caminhos:
        c = Path(c)
        for p in (c, c.with_name(c.name + ".partial")):
            if p.exists():
                with open(p, encoding="utf-8") as f:
                    linhas += list(csv.DictReader(f))
                break
    return linhas


def _num(v, padrao=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return padrao


def _figura(titulo, subtitulo, largura=9, altura=5):
    fig = Figure(figsize=(largura, altura), dpi=110)
    FigureCanvasAgg(fig)
    fig.suptitle(titulo, fontsize=13, fontweight="bold", x=0.01, ha="left")
    fig.text(0.01, 0.905, subtitulo, fontsize=9, color="#555")
    return fig


def _vazio(ax, msg="sem dados neste período"):
    ax.text(0.5, 0.5, msg, ha="center", va="center", fontsize=12, color="#888", transform=ax.transAxes)
    ax.set_xticks([])
    ax.set_yticks([])


# ---------------------------------------------------------------- imagem da câmera
class Cena:
    """Snapshot da câmera + ROIs: onde fica cada braço na imagem."""

    def __init__(self, snapshot, rois: list[dict]):
        import cv2
        img = cv2.imread(str(snapshot))
        if img is None:
            raise FileNotFoundError(snapshot)
        self.img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        self.altura, self.largura = self.img.shape[:2]
        self.rois = rois
        self.centro_braco = {}
        for b in BRACOS:
            pts = [p for r in rois if r["direcao"] == b for p in (*r["linha_entrada"], *r["linha_saida"])]
            if pts:
                self.centro_braco[b] = np.mean(pts, 0)
        self.ponto = {(r["direcao"], r["tipo"]): np.mean([*r["linha_entrada"], *r["linha_saida"]], 0) for r in rois}
        self.meio = np.mean(list(self.centro_braco.values()), 0) if self.centro_braco else \
            np.array([self.largura / 2, self.altura / 2])
        # eixo de cada rua na imagem (perspectiva incluída), apontando PARA DENTRO do cruzamento:
        # a seta de uma ROI de origem já aponta para dentro; a de destino aponta para fora (inverte)
        self.eixo = {}
        for b in self.centro_braco:
            vetores = []
            for r in rois:
                if r["direcao"] != b:
                    continue
                seta = np.mean(r["linha_saida"], 0) - np.mean(r["linha_entrada"], 0)
                n = np.linalg.norm(seta)
                if n > 1e-6:
                    vetores.append(seta / n if r["tipo"] == "origem" else -seta / n)
            eixo = np.mean(vetores, 0) if vetores else self.meio - self.centro_braco[b]
            self.eixo[b] = eixo / (np.linalg.norm(eixo) + 1e-9)

    def fundo(self, ax, escurecer=0.45):
        ax.imshow((self.img * (1 - escurecer)).astype(np.uint8))
        ax.set_xlim(0, self.largura)
        ax.set_ylim(self.altura, 0)
        ax.axis("off")

    def rotulo_braco(self, ax, b, fonte=10, afastar=0.0):
        c = self.centro_braco[b]
        d = c - self.meio
        pos = c + d / (np.linalg.norm(d) + 1e-6) * afastar * self.largura
        ax.text(pos[0], pos[1], b, color="white", fontsize=fonte, fontweight="bold", ha="center", va="center",
                bbox=dict(boxstyle="round,pad=0.25", fc=CORES[b], ec="none"), zorder=20)


def _legenda(fig, cena: "Cena"):
    """Mini-mapa no canto direito: cada ROI pintada na cor do seu braço."""
    ax = fig.add_axes([0.805, 0.16, 0.185, 0.62])
    cena.fundo(ax, escurecer=0.35)
    for r in cena.rois:
        (a1, a2), (b1, b2) = r["linha_entrada"], r["linha_saida"]
        ax.fill([a1[0], a2[0], b2[0], b1[0]], [a1[1], a2[1], b2[1], b1[1]], color=CORES[r["direcao"]],
                alpha=0.75, lw=0)
    for b in cena.centro_braco:
        cena.rotulo_braco(ax, b, fonte=6.5, afastar=0.07)
    ax.set_title("Legenda", fontsize=9, fontweight="bold", color="#333", pad=4)


def _salvar(fig, destino: Path, cena: "Cena | None" = None, ajustar=True):
    """Salva o PNG; com a cena, reserva o canto direito para a legenda."""
    destino.parent.mkdir(parents=True, exist_ok=True)
    if ajustar:
        fig.tight_layout(rect=(0, 0, 0.79 if cena is not None else 1, 0.88))
    if cena is not None:
        _legenda(fig, cena)
    fig.savefig(destino)
    return destino


def _pontos_por_pixel(fig, ax, cena: "Cena") -> float:
    """Quantos pontos de desenho (unidade da espessura de linha) cabem em 1 pixel da imagem."""
    pos = ax.get_position()
    largura_pt = fig.get_figwidth() * pos.width * 72
    altura_pt = fig.get_figheight() * pos.height * 72
    return min(largura_pt / cena.largura, altura_pt / cena.altura)


def _cruzar_retas(p, u, q, v):
    """Ponto onde a reta p + t*u encontra q + s*v; devolve (ponto, t, s) ou None se paralelas."""
    det = u[0] * v[1] - u[1] * v[0]
    if abs(det) < 1e-9:
        return None
    dq = q - p
    t = (dq[0] * v[1] - dq[1] * v[0]) / det
    s = (dq[0] * u[1] - dq[1] * u[0]) / det
    return p + t * u, t, s


OPOSTO = {"cima": "baixo", "baixo": "cima", "esquerda": "direita", "direita": "esquerda"}


def _tracar_corredores(cena: "Cena", fluxos, larg: dict, folga: float) -> dict:
    """Traçado dos canos em "corredores": cada par de braços opostos (cima-baixo, esquerda-direita)
    é um corredor ao longo da rua. Dentro dele cada trecho de fluxo ganha uma faixa paralela ao
    eixo: os dois sentidos em lados opostos (o lado vem das ROIs, então respeita a mão de direção),
    quem segue reto mais perto do centro, as conversões ao lado, com espaço entre elas. Uma
    conversão corre na sua faixa do corredor de origem e passa para a sua faixa do corredor de
    destino no ponto em que as duas faixas se cruzam."""
    corredores = {}
    for a, b in (("cima", "baixo"), ("esquerda", "direita")):
        presentes = [x for x in (a, b) if x in cena.centro_braco]
        if not presentes:
            continue
        # direção do corredor: de a para b (eixo de a aponta para dentro = rumo a b; o de b, ao contrário)
        vets = ([cena.eixo[a]] if a in cena.eixo else []) + ([-cena.eixo[b]] if b in cena.eixo else [])
        dire = np.mean(vets, 0)
        dire = dire / (np.linalg.norm(dire) + 1e-9)
        centro = np.mean([cena.centro_braco[x] for x in presentes], 0)
        perp = np.array([-dire[1], dire[0]])
        corredores[a] = corredores[b] = (centro, dire, perp, (a, b))

    def lado(corr, rumo):
        """Lado do corredor (+1/-1) de quem anda rumo ao braço `rumo`: o da ROI de destino desse
        braço (ou o oposto ao da ROI de origem dele)."""
        centro, _, perp, _ = corr
        if (rumo, "destino") in cena.ponto:
            v = np.dot(cena.ponto[(rumo, "destino")] - centro, perp)
        elif (rumo, "origem") in cena.ponto:
            v = -np.dot(cena.ponto[(rumo, "origem")] - centro, perp)
        else:
            v = 1.0
        return 1.0 if v >= 0 else -1.0

    # trechos: (corredor, rumo, fluxo, é_reto)
    faixas = defaultdict(list)
    for o, d, _ in fluxos:
        if o not in corredores or d not in corredores:
            continue
        reto = corredores[o][3] == corredores[d][3]
        if reto:
            faixas[(corredores[o][3], d)].append(((o, d), True))
        else:
            faixas[(corredores[o][3], OPOSTO[o])].append(((o, d), False))  # entra rumo ao outro lado
            faixas[(corredores[d][3], d)].append(((o, d), False))          # sai rumo ao destino
    deslocamento = {}  # (corredor, fluxo) -> distância do eixo do corredor
    for (nome, rumo), itens in faixas.items():
        corr = corredores[nome[0]]
        s = lado(corr, rumo)
        itens.sort(key=lambda it: (not it[1], -larg[it[0]]))  # retos primeiro (perto do centro)
        pos = folga / 2
        for k, _ in itens:
            deslocamento[(nome, k)] = s * (pos + larg[k] / 2)
            pos += larg[k] + folga

    def ponto_no_eixo(corr, desl, braco):
        """Ponto da faixa (eixo + deslocamento) na altura do braço."""
        centro, dire, perp, _ = corr
        t = np.dot(cena.centro_braco[braco] - centro, dire)
        return centro + dire * t + perp * desl

    tracos = {}
    for o, d, _ in fluxos:
        if o not in corredores or d not in corredores:
            continue
        k = (o, d)
        co, cd = corredores[o], corredores[d]
        if co[3] == cd[3]:
            desl = deslocamento[(co[3], k)]
            tracos[k] = [tuple(ponto_no_eixo(co, desl, o)), tuple(ponto_no_eixo(co, desl, d))]
            continue
        do, dd = deslocamento[(co[3], k)], deslocamento[(cd[3], k)]
        inicio, fim = ponto_no_eixo(co, do, o), ponto_no_eixo(cd, dd, d)
        cruz = _cruzar_retas(co[0] + co[2] * do, co[1], cd[0] + cd[2] * dd, cd[1])
        tracos[k] = [tuple(inicio), tuple(cruz[0]), tuple(fim)] if cruz else [tuple(inicio), tuple(fim)]
    return tracos


def _mapa_fluxos(fig, ax, cena: "Cena", od: Counter) -> bool:
    """Fluxos origem -> destino como canos que seguem a direção de cada rua na imagem (o eixo
    tirado das ROIs): o cano entra pelo eixo da rua de origem e, numa conversão, faz a curva onde
    esse eixo encontra o da rua de destino; seguindo reto, é uma reta só. Grossura proporcional
    ao número de veículos; canos lado a lado (sem se sobrepor) nas saídas e nas chegadas."""
    fluxos = [(o, d, n) for (o, d), n in od.items()
              if o != d and o in cena.centro_braco and d in cena.centro_braco]
    if not fluxos:
        return False
    minimo = 2 if any(n >= 2 for _, _, n in fluxos) else 1  # fluxos de 1 veículo só poluem
    omitidos = sum(n for _, _, n in fluxos if n < minimo)
    fluxos = [f for f in fluxos if f[2] >= minimo]
    cena.fundo(ax, escurecer=0.5)
    maior = max(n for _, _, n in fluxos)
    W = cena.largura
    w_min, w_max = 0.005 * W, 0.022 * W       # mais finos que antes: precisam caber lado a lado
    larg = {(o, d): w_min + (w_max - w_min) * n / maior for o, d, n in fluxos}
    folga = 0.007 * W                           # espaço entre um cano e o vizinho
    tracos = _tracar_corredores(cena, fluxos, larg, folga)
    if not tracos:
        return False

    pt_px = _pontos_por_pixel(fig, ax, cena)
    for o, d, n in sorted(fluxos, key=lambda f: -f[2]):  # grossos primeiro, finos por cima
        k = (o, d)
        if k not in tracos:
            continue
        pts = tracos[k]
        w = larg[k]
        # ponta da seta: triângulo no fim do último trecho
        (x1, y1), (x2, y2) = pts[-2], pts[-1]
        comp = math.hypot(x2 - x1, y2 - y1) or 1.0
        ux, uy = (x2 - x1) / comp, (y2 - y1) / comp
        cab = min(0.7 * w + 0.012 * cena.largura, comp * 0.5)   # ponta proporcional, mas contida
        base = (x2 - ux * cab, y2 - uy * cab)
        linha = pts[:-1] + [base]
        xs, ys = zip(*linha)
        lw = w * pt_px
        ax.plot(xs, ys, color="#111", lw=lw + 2.2, solid_joinstyle="miter", solid_capstyle="butt",
                alpha=0.75, zorder=5)                                    # contorno do cano
        ax.plot(xs, ys, color=CORES[o], lw=lw, solid_joinstyle="miter", solid_capstyle="butt", zorder=6)
        ax.plot(xs, ys, color="white", lw=max(0.6, lw * 0.22), alpha=0.35, solid_joinstyle="miter",
                solid_capstyle="butt", zorder=7)                         # reflexo: aparência de tubo
        meia = 0.5 * w + 0.006 * cena.largura
        px, py = -uy, ux
        tri = [(x2, y2), (base[0] + px * meia, base[1] + py * meia), (base[0] - px * meia, base[1] - py * meia)]
        ax.add_patch(Polygon(tri, closed=True, fc=CORES[o], ec="#111", lw=1.0, zorder=8))
        # número no 1o trecho, perto da origem: mostra como cada braço se divide e não amontoa no centro
        (sx1, sy1), (sx2, sy2) = pts[0], pts[1]
        f = 0.5 if math.hypot(sx2 - sx1, sy2 - sy1) < 0.12 * cena.largura else 0.35
        ax.text(sx1 + (sx2 - sx1) * f, sy1 + (sy2 - sy1) * f, str(n), color="white", fontsize=8.5,
                fontweight="bold", ha="center", va="center", zorder=12,
                bbox=dict(boxstyle="round,pad=0.25", fc=CORES[o], ec="white", lw=0.8))
    for b in cena.centro_braco:
        cena.rotulo_braco(ax, b, fonte=10, afastar=0.09)
    if omitidos:
        ax.text(0.01, 0.02, f"fluxos com menos de {minimo} veículos omitidos ({omitidos} veículos)",
                transform=ax.transAxes, color="white", fontsize=8, zorder=20)
    return True


def _mapa_fila(ax, cena: "Cena", parados: list[dict]) -> bool:
    """Bolinha com fila média / máxima em cada braço e, atrás dela, a fila desenhada como
    carrinhos: cheios até a média, vazados até a máxima."""
    por_braco = defaultdict(list)
    for p in parados:
        if p["roi"] in cena.centro_braco:
            por_braco[p["roi"]].append(int(p["veiculos_parados"]))
    if not por_braco:
        return False
    cena.fundo(ax, escurecer=0.5)
    W = cena.largura
    comp, larg_c, passo = 0.040 * W, 0.024 * W, 0.050 * W   # tamanho do "carrinho" e espaçamento
    raio = 0.035 * W
    for b, vals in por_braco.items():
        c = cena.ponto.get((b, "origem"), cena.centro_braco[b])
        media, maximo = float(np.mean(vals)), max(vals)
        # a fila se forma rua acima: ao longo do eixo da rua (tirado das ROIs), para fora do cruzamento
        ux, uy = -cena.eixo[b]
        px, py = -uy, ux
        mostrados = min(maximo, 10)
        for i in range(mostrados):
            centro = c + np.array([ux, uy]) * (raio + passo * (i + 0.6))
            cheio = min(1.0, max(0.0, media - i))       # fração "ocupada" deste lugar da fila

            def retangulo(frac):
                f0, f1 = -comp / 2, -comp / 2 + comp * frac
                return [(centro[0] + ux * f0 + px * larg_c / 2, centro[1] + uy * f0 + py * larg_c / 2),
                        (centro[0] + ux * f1 + px * larg_c / 2, centro[1] + uy * f1 + py * larg_c / 2),
                        (centro[0] + ux * f1 - px * larg_c / 2, centro[1] + uy * f1 - py * larg_c / 2),
                        (centro[0] + ux * f0 - px * larg_c / 2, centro[1] + uy * f0 - py * larg_c / 2)]
            ax.add_patch(Polygon(retangulo(1.0), closed=True, fc="none", ec=CORES[b], lw=1.6, zorder=6))
            if cheio > 0:
                ax.add_patch(Polygon(retangulo(cheio), closed=True, fc=CORES[b], ec="none", alpha=0.9, zorder=7))
        if maximo > mostrados:
            fim = c + np.array([ux, uy]) * (raio + passo * (mostrados + 0.6))
            ax.text(fim[0], fim[1], f"+{maximo - mostrados}", color="white", fontsize=8, ha="center", va="center",
                    zorder=8)
        ax.add_patch(Polygon([(c[0] + raio * math.cos(t), c[1] + raio * math.sin(t))
                              for t in np.linspace(0, 2 * math.pi, 40)], closed=True,
                             fc=CORES[b], ec="white", lw=1.5, alpha=0.95, zorder=9))
        ax.text(c[0], c[1], f"{b}\n{media:.1f} / {maximo}", color="white", fontsize=9, fontweight="bold",
                ha="center", va="center", zorder=10)
    ax.text(0.01, 0.02, "■ cheio = fila média    □ vazado = até a fila máxima    número = média / máxima",
            transform=ax.transAxes, color="white", fontsize=8.5, zorder=20)
    return True


def _tamanho_periodo(segundos_totais: float) -> int:
    """Segundos por barra: ~20 a 40 barras qualquer que seja a duração (5 min ou 1 dia)."""
    for passo in (10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600):
        if segundos_totais / passo <= 40:
            return passo
    return 3600


def _grafico_faixa(ax, parados: list[dict], bracos: list[str]) -> bool:
    """Barras empilhadas por período ligadas por faixas suaves entre uma barra e a próxima."""
    import numpy as np
    pontos = [(datetime.fromisoformat(p["horario"]), p["roi"], int(p["veiculos_parados"]))
              for p in parados if p["roi"] in bracos]
    if not pontos:
        return False
    t0 = min(t for t, _, _ in pontos)
    total = (max(t for t, _, _ in pontos) - t0).total_seconds() + 1
    passo = _tamanho_periodo(total)
    n_per = int(total // passo) + 1
    soma = {b: np.zeros(n_per) for b in bracos}
    cont = {b: np.zeros(n_per) for b in bracos}
    for t, b, n in pontos:
        k = int((t - t0).total_seconds() // passo)
        soma[b][k] += n
        cont[b][k] += 1
    media = {b: np.divide(soma[b], cont[b], out=np.zeros(n_per), where=cont[b] > 0) for b in bracos}
    base = np.zeros(n_per)
    meia = 0.32                     # meia largura da barra (períodos = 1 unidade)
    u = np.linspace(0, 1, 24)
    suave = 3 * u ** 2 - 2 * u ** 3  # curva em S entre uma barra e a próxima
    for b in bracos:
        topo = base + media[b]
        cor = CORES[b]
        ax.bar(range(n_per), media[b], bottom=base, width=2 * meia, color=cor, label=b, zorder=3)
        for k in range(n_per - 1):
            x = k + meia + u * (1 - 2 * meia)
            ax.fill_between(x, base[k] + (base[k + 1] - base[k]) * suave, topo[k] + (topo[k + 1] - topo[k]) * suave,
                            color=cor, alpha=0.35, lw=0, zorder=2)
        base = topo
    rotulos = [t0 + timedelta(seconds=k * passo) for k in range(n_per)]
    salto = max(1, n_per // 12)
    ax.set_xticks(range(0, n_per, salto), [f"{r:%H:%M:%S}" if passo < 60 else f"{r:%H:%M}"
                                           for r in rotulos[::salto]], rotation=30, ha="right")
    ax.set_xlim(-0.6, n_per - 0.4)
    ax.set_ylim(0, max(float(base.max()), 0.5) * 1.15)  # base = soma de todos os braços
    ax.set_ylabel(f"veículos parados (média a cada {passo // 60} min)" if passo >= 60 else
                  f"veículos parados (média a cada {passo} s)")
    ax.legend(loc="upper left", fontsize=8, ncol=len(bracos))
    ax.grid(alpha=0.3, axis="y", zorder=0)
    return True


def gerar(veiculos: list[dict], parados: list[dict], pasta: Path, prefixo: str, camera_id: str,
          periodo: str, janela_livre_s: float = 5.0, cena: "Cena | None" = None) -> list[Path]:
    """Gera os gráficos (9 com a cena da câmera, 7 sem ela); devolve os caminhos.
    Período vazio gera o gráfico com "sem dados"."""
    sub = f"{camera_id} · {periodo}"
    feitos = []
    bracos_vistos = [b for b in BRACOS if any(v.get("direcao_origem") == b or v.get("direcao_destino") == b
                                              for v in veiculos) or any(p.get("roi") == b for p in parados)]
    bracos = bracos_vistos or BRACOS
    completos = [v for v in veiculos if v.get("situacao") == "completo"]
    com_origem = [v for v in veiculos if v.get("direcao_origem")]

    # 1 heatmap origem x destino
    fig = _figura("Matriz origem × destino", f"{sub} · {len(completos)} veículos com origem e destino")
    ax = fig.add_subplot(111)
    if completos:
        m = [[sum(1 for v in completos if v["direcao_origem"] == o and v["direcao_destino"] == d)
              for d in bracos] for o in bracos]
        im = ax.imshow(m, cmap="Blues")
        maximo = max(max(r) for r in m) or 1
        for i, r in enumerate(m):
            for j, n in enumerate(r):
                ax.text(j, i, str(n), ha="center", va="center", fontsize=12,
                        color="white" if n > maximo * 0.6 else "#222")
        ax.set_xticks(range(len(bracos)), bracos)
        ax.set_yticks(range(len(bracos)), bracos)
        for rotulos in (ax.get_xticklabels(), ax.get_yticklabels()):  # nomes na cor do braço
            for t, b in zip(rotulos, bracos):
                t.set_color(CORES[b])
                t.set_fontweight("bold")
        ax.set_xlabel("destino")
        ax.set_ylabel("origem")
        fig.colorbar(im, ax=ax, shrink=0.8, label="veículos")
    else:
        _vazio(ax)
    feitos.append(_salvar(fig, pasta / f"{prefixo}_1_od_heatmap.png", cena))

    # 2 mapa de fluxos (os mesmos números da matriz, desenhados como canos sobre a imagem)
    if cena is not None:
        fig = _figura("Fluxos origem → destino no cruzamento", sub, largura=10, altura=6.4)
        ax = fig.add_axes([0.01, 0.01, 0.98, 0.86])
        od = Counter((v["direcao_origem"], v["direcao_destino"]) for v in completos)
        if not _mapa_fluxos(fig, ax, cena, od):
            _vazio(ax)
        feitos.append(_salvar(fig, pasta / f"{prefixo}_2_mapa_fluxos.png", ajustar=False))

    # 3 tempo parado x em movimento por braço de origem
    fig = _figura("Tempo parado × em movimento por braço de origem",
                  f"{sub} · tempo de cada veículo desde que apareceu até sumir da imagem", largura=11)
    if com_origem:
        ax1, ax2 = fig.add_subplot(121), fig.add_subplot(122)
        grupos = {b: [v for v in com_origem if v["direcao_origem"] == b] for b in bracos}
        nomes = [b for b in bracos if grupos[b]]
        tot_p = [sum(_num(v["tempo_parado_total_s"]) for v in grupos[b]) for b in nomes]
        tot_m = [sum(_num(v["tempo_movimento_s"]) for v in grupos[b]) for b in nomes]
        y = list(range(len(nomes)))
        # proporção do tempo: o que mais chama a atenção é quanto do tempo o braço passa parado
        pct_p = [100 * p / (p + m) if p + m else 0 for p, m in zip(tot_p, tot_m)]
        ax1.barh(y, pct_p, color="#d62728", label="parado")
        ax1.barh(y, [100 - p for p in pct_p], left=pct_p, color="#2ca02c", label="em movimento")
        for i, p in enumerate(pct_p):
            if p >= 8:
                ax1.text(p / 2, i, f"{p:.0f}%", ha="center", va="center", color="white", fontweight="bold")
            if 100 - p >= 8:
                ax1.text(p + (100 - p) / 2, i, f"{100 - p:.0f}%", ha="center", va="center", color="white",
                         fontweight="bold")
        ax1.set_yticks(y, [f"{b}\n(n={len(grupos[b])})" for b in nomes])
        ax1.invert_yaxis()
        ax1.set_xlim(0, 100)
        ax1.set_xlabel("% do tempo")
        ax1.set_title("Proporção do tempo", fontsize=10)
        ax1.legend(loc="upper center", bbox_to_anchor=(0.5, -0.14), ncol=2, fontsize=8, frameon=False)
        # média por veículo, lado a lado
        med_p = [p / len(grupos[b]) for p, b in zip(tot_p, nomes)]
        med_m = [m / len(grupos[b]) for m, b in zip(tot_m, nomes)]
        alt = 0.38
        ax2.barh([i - alt / 2 for i in y], med_p, height=alt, color="#d62728", label="parado")
        ax2.barh([i + alt / 2 for i in y], med_m, height=alt, color="#2ca02c", label="em movimento")
        for i in y:
            ax2.text(med_p[i], i - alt / 2, f" {med_p[i]:.1f} s", va="center", fontsize=8)
            ax2.text(med_m[i], i + alt / 2, f" {med_m[i]:.1f} s", va="center", fontsize=8)
        ax2.set_yticks(y, nomes)
        ax2.invert_yaxis()
        ax2.set_xlabel("segundos (média por veículo)")
        ax2.set_title("Média por veículo", fontsize=10)
        ax2.set_xlim(0, max(med_p + med_m) * 1.25 or 1)
        ax2.grid(alpha=0.3, axis="x")
    else:
        _vazio(fig.add_subplot(111))
    feitos.append(_salvar(fig, pasta / f"{prefixo}_3_parado_x_movimento.png", cena))

    # 4 volume por minuto e braço de origem
    fig = _figura("Veículos por minuto e braço de origem", f"{sub} · pela hora em que passaram na origem")
    ax = fig.add_subplot(111)
    if com_origem:
        por_min = defaultdict(Counter)
        for v in com_origem:
            t = datetime.fromisoformat(v["hora_origem"]).replace(second=0)
            por_min[v["direcao_origem"]][t] += 1
        minutos = sorted({t for c in por_min.values() for t in c})
        for b in bracos:
            if por_min[b]:
                ax.plot(minutos, [por_min[b].get(t, 0) for t in minutos], marker="o", ms=3,
                        color=CORES[b], label=f"de {b}")
        ax.set_ylabel("veículos / min")
        ax.yaxis.set_major_locator(MaxNLocator(integer=True))
        ax.legend(loc="upper left", fontsize=8)
        fig.autofmt_xdate()
        ax.grid(alpha=0.3)
    else:
        _vazio(ax)
    feitos.append(_salvar(fig, pasta / f"{prefixo}_4_volume.png", cena))

    # 5 fila: gráfico de faixa (como o "ribbon chart" do Power BI)
    fig = _figura("Fila: veículos parados em cada braço",
                  f"{sub} · média de veículos parados (há ≥ 2 s, antes de entrar) por período", largura=11)
    ax = fig.add_subplot(111)
    if parados and _grafico_faixa(ax, parados, bracos):
        pass
    else:
        _vazio(ax)
    feitos.append(_salvar(fig, pasta / f"{prefixo}_5_fila.png", cena))

    # 6 fila no mapa
    if cena is not None:
        fig = _figura("Fila média e máxima em cada braço",
                      f"{sub} · veículos parados há ≥ 2 s antes de entrar no cruzamento", largura=10, altura=6.4)
        ax = fig.add_axes([0.01, 0.01, 0.98, 0.86])
        if not _mapa_fila(ax, cena, parados):
            _vazio(ax)
        feitos.append(_salvar(fig, pasta / f"{prefixo}_6_fila_mapa.png", ajustar=False))

    # 7 distribuição da espera antes da origem
    fig = _figura("Tempo de espera antes de entrar no cruzamento",
                  f"{sub} · só veículos que pararam (s)")
    ax = fig.add_subplot(111)
    dados = {b: [_num(v["tempo_parado_antes_origem_s"]) for v in com_origem
                 if v["direcao_origem"] == b and _num(v["tempo_parado_antes_origem_s"]) > 0] for b in bracos}
    nomes = [b for b in bracos if dados[b]]
    if nomes:
        caixa = ax.boxplot([dados[b] for b in nomes], patch_artist=True, showmeans=True)
        for patch, b in zip(caixa["boxes"], nomes):
            patch.set_facecolor(CORES[b])
            patch.set_alpha(0.6)
        ax.set_xticks(range(1, len(nomes) + 1), [f"de {b}\n(n={len(dados[b])})" for b in nomes])
        ax.set_ylabel("segundos parado")
        ax.grid(alpha=0.3, axis="y")
    else:
        _vazio(ax, "nenhum veículo parou antes da origem neste período")
    feitos.append(_salvar(fig, pasta / f"{prefixo}_7_espera_antes_origem.png", cena))

    # 8 espera com cruzamento livre
    fig = _figura(f"Espera com o cruzamento livre por ≥ {janela_livre_s:g} s",
                  f"{sub} · veículos que pararam antes da origem")
    ax = fig.add_subplot(111)
    pararam = [v for v in com_origem if _num(v["tempo_parado_antes_origem_s"]) > 0]
    nomes = [b for b in bracos if any(v["direcao_origem"] == b for v in pararam)]
    if nomes:
        sim = [sum(1 for v in pararam if v["direcao_origem"] == b and v["espera_sem_movimento"] == "VERDADEIRO")
               for b in nomes]
        nao = [sum(1 for v in pararam if v["direcao_origem"] == b) - s for b, s in zip(nomes, sim)]
        x = range(len(nomes))
        ax.bar(x, sim, color="#d62728", label="esperou com o cruzamento livre")
        ax.bar(x, nao, bottom=sim, color="#bdbdbd", label="havia movimento no cruzamento")
        for i, b in enumerate(nomes):
            livres = [_num(v["maior_tempo_sem_movimento_s"]) for v in pararam if v["direcao_origem"] == b]
            ax.text(i, sim[i] + nao[i], f"maior janela média\n{sum(livres) / len(livres):.1f} s",
                    ha="center", va="bottom", fontsize=8)
        ax.set_xticks(list(x), [f"de {b}" for b in nomes])
        ax.set_ylabel("veículos")
        ax.legend(loc="upper right", fontsize=8)
        ax.set_ylim(0, max(s + n for s, n in zip(sim, nao)) * 1.35)
    else:
        _vazio(ax, "nenhum veículo parou antes da origem neste período")
    feitos.append(_salvar(fig, pasta / f"{prefixo}_8_espera_livre.png", cena))

    # 9 qualidade da coleta (não é por braço: sem legenda)
    fig = _figura("Qualidade da coleta: registros por situação", f"{sub} · {len(veiculos)} veículos gravados")
    ax = fig.add_subplot(111)
    if veiculos:
        cont = Counter(v.get("situacao", "") for v in veiculos)
        nomes = [s for s in SITUACOES if cont[s]]
        cores = {"completo": "#2ca02c", "sem_destino": "#ff7f0e", "sem_origem": "#9e9e9e",
                 "em_andamento_no_fim": "#1f77b4"}
        barras = ax.bar(range(len(nomes)), [cont[s] for s in nomes], color=[cores[s] for s in nomes])
        for b, s in zip(barras, nomes):
            ax.text(b.get_x() + b.get_width() / 2, b.get_height(),
                    f"{cont[s]} ({100 * cont[s] / len(veiculos):.0f}%)", ha="center", va="bottom", fontsize=9)
        ax.set_xticks(range(len(nomes)), [s.replace("_", " ") for s in nomes])
        ax.set_ylabel("veículos")
    else:
        _vazio(ax)
    feitos.append(_salvar(fig, pasta / f"{prefixo}_9_qualidade.png"))
    return feitos
