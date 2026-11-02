"""Aba 2: desenho das ROIs sobre o snapshot.

Nova ROI = 4 cliques: 2 para a linha 1 (entrada) e 2 para a linha 2 (saída); em seguida o
usuário escolhe direção e tipo entre as combinações ainda livres. Os pontos podem ser
arrastados depois. Roda do mouse = zoom; botão direito arrastando = mover a imagem.
"""
import math

import cv2
from PySide6.QtCore import QLineF, QPointF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QIcon, QPainter, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
                               QGraphicsEllipseItem, QGraphicsItem, QGraphicsLineItem,
                               QGraphicsPolygonItem, QGraphicsScene, QGraphicsSimpleTextItem,
                               QGraphicsView, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
                               QMessageBox, QPushButton, QVBoxLayout, QWidget)

from .imagem import para_pixmap
from .roi_config import DIRECOES, TIPOS, ConfigRois, Roi, pasta_config

COR_ENTRADA = QColor("#2ecc40")   # linha 1
COR_SAIDA = QColor("#ff4136")     # linha 2
CORES = {  # uma cor fixa e bem distinta por combinação, igual em toda câmera
    "cima_origem": "#1f77b4", "baixo_origem": "#ff7f0e", "esquerda_origem": "#9467bd",
    "direita_origem": "#17becf", "cima_destino": "#e377c2", "baixo_destino": "#bcbd22",
    "esquerda_destino": "#8c564b", "direita_destino": "#2ca02c",
}
ROTULO_DIRECAO = {"cima": "Cima ↑", "baixo": "Baixo ↓", "esquerda": "Esquerda ←", "direita": "Direita →"}
ROTULO_TIPO = {"origem": "Origem (entra no cruzamento)", "destino": "Destino (sai do cruzamento)"}


def _caneta(cor, largura, estilo=Qt.SolidLine):
    p = QPen(cor, largura, estilo)
    p.setCosmetic(True)  # espessura constante na tela, com qualquer zoom
    return p


# ---------------------------------------------------------------- itens gráficos
class _Alca(QGraphicsEllipseItem):
    """Ponto arrastável de uma linha."""

    def __init__(self, ao_mover, cor):
        super().__init__(-5, -5, 10, 10)
        self.ao_mover = ao_mover
        self.setBrush(QBrush(cor))
        self.setPen(_caneta(QColor("white"), 1.5))
        self.setFlags(QGraphicsItem.ItemIsMovable | QGraphicsItem.ItemSendsGeometryChanges
                      | QGraphicsItem.ItemIgnoresTransformations)
        self.setCursor(Qt.SizeAllCursor)
        self.setZValue(10)

    def itemChange(self, mudanca, valor):
        if mudanca == QGraphicsItem.ItemPositionChange and self.scene():
            r = self.scene().sceneRect()  # não deixa o ponto sair da imagem
            return QPointF(min(max(valor.x(), r.left()), r.right()), min(max(valor.y(), r.top()), r.bottom()))
        if mudanca == QGraphicsItem.ItemPositionHasChanged:
            self.ao_mover()
        return super().itemChange(mudanca, valor)


class ItemRoi:
    """Desenho de uma ROI: área, linha 1 (verde), linha 2 (vermelha), seta e nome."""

    def __init__(self, cena: QGraphicsScene, roi: Roi, ao_alterar):
        self.roi, self.cena, self.ao_alterar = roi, cena, ao_alterar
        cor = QColor(CORES[roi.nome])
        self.area = QGraphicsPolygonItem()
        self.area.setData(0, self)
        self.l1 = QGraphicsLineItem()
        self.l2 = QGraphicsLineItem()
        self.seta = [QGraphicsLineItem() for _ in range(3)]
        self.texto = QGraphicsSimpleTextItem(roi.nome)
        self.texto.setFlag(QGraphicsItem.ItemIgnoresTransformations)
        f = QFont()
        f.setBold(True)
        self.texto.setFont(f)
        self.texto.setBrush(QBrush(QColor("white")))
        self.texto.setPen(_caneta(QColor("black"), 0.8))
        self.l1.setPen(_caneta(COR_ENTRADA, 3))
        self.l2.setPen(_caneta(COR_SAIDA, 3))
        for s in self.seta:
            s.setPen(_caneta(QColor("white"), 2))
        self.alcas = []
        for i in range(4):
            a = _Alca(lambda i=i: self._alca_movida(i), COR_ENTRADA if i < 2 else COR_SAIDA)
            self.alcas.append(a)
        for it in [self.area, self.l1, self.l2, *self.seta, self.texto, *self.alcas]:
            cena.addItem(it)
        self._cor = cor
        self._movendo = False
        self.selecionar(False)
        self.atualizar(posicionar_alcas=True)

    def pontos(self):
        return [*self.roi.linha_entrada, *self.roi.linha_saida]

    def _alca_movida(self, i):
        if self._movendo:
            return
        p = self.alcas[i].pos()
        pts = self.pontos()
        pts[i] = (p.x(), p.y())
        self.roi.linha_entrada, self.roi.linha_saida = pts[:2], pts[2:]
        self.atualizar()
        self.ao_alterar(self)

    def atualizar(self, posicionar_alcas=False):
        self.texto.setText(self.roi.nome)
        self._cor = QColor(CORES[self.roi.nome])
        (a1, a2), (b1, b2) = self.roi.linha_entrada, self.roi.linha_saida
        self.area.setPolygon(QPolygonF([QPointF(*p) for p in self.roi.poligono()]))
        self.l1.setLine(QLineF(QPointF(*a1), QPointF(*a2)))
        self.l2.setLine(QLineF(QPointF(*b1), QPointF(*b2)))
        # seta do meio da linha 1 ao meio da linha 2 = sentido de passagem
        m1 = QPointF((a1[0] + a2[0]) / 2, (a1[1] + a2[1]) / 2)
        m2 = QPointF((b1[0] + b2[0]) / 2, (b1[1] + b2[1]) / 2)
        self.seta[0].setLine(QLineF(m1, m2))
        ang = math.atan2(m2.y() - m1.y(), m2.x() - m1.x())
        tam = max(6.0, min(18.0, QLineF(m1, m2).length() * 0.3))
        for k, s in ((1, 1), (2, -1)):
            a = ang + math.pi + s * 0.45
            self.seta[k].setLine(QLineF(m2, m2 + QPointF(math.cos(a) * tam, math.sin(a) * tam)))
        c = self.area.polygon().boundingRect().center()
        self.texto.setPos(c)
        if posicionar_alcas:
            self._movendo = True
            for a, p in zip(self.alcas, self.pontos()):
                a.setPos(QPointF(*p))
            self._movendo = False
        self.selecionar(self._selecionada)

    def selecionar(self, sim: bool):
        self._selecionada = sim
        preenchimento = QColor(self._cor)
        preenchimento.setAlpha(120 if sim else 75)
        self.area.setBrush(QBrush(preenchimento))
        self.area.setPen(_caneta(QColor("white") if sim else self._cor, 2 if sim else 1.5,
                                 Qt.SolidLine if sim else Qt.DashLine))
        for a in self.alcas:
            a.setVisible(sim)

    def remover(self):
        for it in [self.area, self.l1, self.l2, *self.seta, self.texto, *self.alcas]:
            self.cena.removeItem(it)


# ---------------------------------------------------------------- vista
class VistaRois(QGraphicsView):
    desenho_concluido = Signal(list)   # 4 pontos
    roi_clicada = Signal(object)       # ItemRoi ou None
    roi_alterada = Signal(object)
    dica = Signal(str)

    def __init__(self):
        super().__init__()
        self.cena = QGraphicsScene(self)
        self.setScene(self.cena)
        self.setRenderHint(QPainter.Antialiasing)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setBackgroundBrush(QBrush(QColor("#202020")))
        self.setMouseTracking(True)
        self.fundo = None
        self.itens: list[ItemRoi] = []
        self.desenhando = False
        self._pts: list[QPointF] = []
        self._provisorios = []
        self._guia = None
        self._pan = None

    # ---------------------------------------------------------- imagem e zoom
    def definir_fundo(self, pixmap: QPixmap):
        if self.fundo is None:
            self.fundo = self.cena.addPixmap(pixmap)
            self.fundo.setZValue(-10)
        else:
            self.fundo.setPixmap(pixmap)
        self.cena.setSceneRect(self.fundo.boundingRect())
        self.ajustar()

    def ajustar(self):
        if self.fundo is not None:
            self.fitInView(self.fundo, Qt.KeepAspectRatio)

    def wheelEvent(self, e):
        if self.fundo is None:
            return
        f = 1.15 if e.angleDelta().y() > 0 else 1 / 1.15
        escala = self.transform().m11() * f
        minimo = min(self.viewport().width() / self.cena.width(), self.viewport().height() / self.cena.height())
        if minimo * 0.9 <= escala <= 12:
            self.scale(f, f)

    # ---------------------------------------------------------- ROIs
    def mostrar(self, rois: list[Roi]):
        for it in self.itens:
            it.remover()
        self.itens = [ItemRoi(self.cena, r, lambda it: self.roi_alterada.emit(it)) for r in rois]

    def selecionar(self, roi: Roi | None):
        for it in self.itens:
            it.selecionar(it.roi is roi)

    # ---------------------------------------------------------- desenho
    def iniciar_desenho(self):
        self.cancelar_desenho()
        self.desenhando = True
        self.selecionar(None)
        self.setCursor(Qt.CrossCursor)
        self.dica.emit("Linha 1 (entrada): clique o 1º ponto.  Esc cancela.")

    def cancelar_desenho(self):
        for it in self._provisorios + ([self._guia] if self._guia else []):
            self.cena.removeItem(it)
        self._provisorios, self._guia, self._pts = [], None, []
        if self.desenhando:
            self.dica.emit("Desenho cancelado.")
        self.desenhando = False
        self.unsetCursor()

    def _na_imagem(self, p: QPointF) -> QPointF:
        r = self.cena.sceneRect()
        return QPointF(min(max(p.x(), r.left()), r.right()), min(max(p.y(), r.top()), r.bottom()))

    def mousePressEvent(self, e):
        if e.button() == Qt.RightButton:
            self._pan = e.position()
            self.setCursor(Qt.ClosedHandCursor)
            return
        if e.button() == Qt.LeftButton and self.desenhando and self.fundo is not None:
            p = self._na_imagem(self.mapToScene(e.position().toPoint()))
            self._pts.append(p)
            cor = COR_ENTRADA if len(self._pts) <= 2 else COR_SAIDA
            ponto = self.cena.addEllipse(-4, -4, 8, 8, _caneta(QColor("white"), 1), QBrush(cor))
            ponto.setFlag(QGraphicsItem.ItemIgnoresTransformations)
            ponto.setPos(p)
            self._provisorios.append(ponto)
            if len(self._pts) in (2, 4):
                self._provisorios.append(self.cena.addLine(QLineF(self._pts[-2], self._pts[-1]), _caneta(cor, 3)))
            mensagens = {1: "Linha 1 (entrada): clique o 2º ponto.",
                         2: "Linha 2 (saída): clique o 1º ponto.",
                         3: "Linha 2 (saída): clique o 2º ponto."}
            if len(self._pts) == 4:
                pts = [(q.x(), q.y()) for q in self._pts]
                self.cancelar_desenho()
                self.desenho_concluido.emit(pts)
            else:
                self.dica.emit(mensagens[len(self._pts)] + "  Esc cancela.")
            return
        if e.button() == Qt.LeftButton and not self.desenhando:
            sob = self.items(e.position().toPoint())
            if any(isinstance(i, _Alca) for i in sob):
                super().mousePressEvent(e)  # arrastar ponto
                return
            # clique na área ou numa das linhas seleciona a ROI (a de cima, se houver sobreposição)
            dono = next((it for i in sob for it in self.itens if i in (it.area, it.l1, it.l2)), None)
            self.roi_clicada.emit(dono)
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._pan is not None:
            d = e.position() - self._pan
            self._pan = e.position()
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - int(d.x()))
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - int(d.y()))
            return
        if self.desenhando and len(self._pts) % 2 == 1:  # linha "elástica" até o mouse
            p = self._na_imagem(self.mapToScene(e.position().toPoint()))
            cor = COR_ENTRADA if len(self._pts) == 1 else COR_SAIDA
            if self._guia is None:
                self._guia = self.cena.addLine(QLineF(self._pts[-1], p), _caneta(cor, 2, Qt.DashLine))
            self._guia.setLine(QLineF(self._pts[-1], p))
        elif self._guia is not None:
            self.cena.removeItem(self._guia)
            self._guia = None
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.RightButton and self._pan is not None:
            self._pan = None
            self.setCursor(Qt.CrossCursor) if self.desenhando else self.unsetCursor()
            return
        super().mouseReleaseEvent(e)

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape and self.desenhando:
            self.cancelar_desenho()
            return
        super().keyPressEvent(e)


# ---------------------------------------------------------------- diálogo direção/tipo
class DialogoRoi(QDialog):
    def __init__(self, livres: list[tuple[str, str]], atual: tuple[str, str] | None = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Direção e tipo da ROI")
        self.livres = livres
        self.direcao = QComboBox()
        self.tipo = QComboBox()
        for d in DIRECOES:
            if any(l[0] == d for l in livres):
                self.direcao.addItem(ROTULO_DIRECAO[d], d)
        self.direcao.currentIndexChanged.connect(self._atualizar_tipos)
        form = QFormLayout(self)
        form.addRow("Direção do movimento na tela:", self.direcao)
        form.addRow("Tipo:", self.tipo)
        nota = QLabel("Só aparecem as combinações ainda não usadas neste vídeo.")
        nota.setStyleSheet("color:#666;")
        form.addRow(nota)
        botoes = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        botoes.accepted.connect(self.accept)
        botoes.rejected.connect(self.reject)
        form.addRow(botoes)
        if atual:
            self.direcao.setCurrentIndex(max(0, self.direcao.findData(atual[0])))
        self._atualizar_tipos()
        if atual:
            self.tipo.setCurrentIndex(max(0, self.tipo.findData(atual[1])))

    def _atualizar_tipos(self):
        d = self.direcao.currentData()
        self.tipo.clear()
        for t in TIPOS:
            if (d, t) in self.livres:
                self.tipo.addItem(ROTULO_TIPO[t], t)

    def escolha(self) -> tuple[str, str]:
        return self.direcao.currentData(), self.tipo.currentData()


# ---------------------------------------------------------------- aba
class AbaRois(QWidget):
    estado_mudou = Signal(bool)  # True = ROIs válidas e salvas

    def __init__(self, obter_camera_id):
        super().__init__()
        self.obter_camera_id = obter_camera_id
        self.cfg: ConfigRois | None = None
        self.info = None
        self.quadro = None
        self.salvo = False
        self.selecionada: Roi | None = None
        self.perguntar_ao_carregar = True  # desligado nos testes automáticos

        self.vista = VistaRois()
        self.vista.desenho_concluido.connect(self._desenho_concluido)
        self.vista.roi_clicada.connect(lambda it: self._selecionar(it.roi if it else None))
        self.vista.roi_alterada.connect(lambda it: self._alterou())
        self.vista.dica.connect(lambda t: self.dica.setText(t))

        self.dica = QLabel("Clique em <b>Nova ROI</b> para começar.")
        self.dica.setWordWrap(True)
        instrucoes = QLabel(
            "<b>Como desenhar</b><br>"
            "1. <b>Nova ROI</b><br>"
            "2. Clique 2 pontos: <span style='color:#1b9e2f'><b>linha 1 (entrada)</b></span><br>"
            "3. Clique 2 pontos: <span style='color:#d62728'><b>linha 2 (saída)</b></span><br>"
            "4. Escolha direção e tipo<br><br>"
            "O veículo passa pela ROI ao cruzar a linha 1 e depois a 2 (sentido da seta).<br>"
            "Arraste os pontos da ROI selecionada para ajustar.<br>"
            "<i>Roda do mouse: zoom · botão direito: mover · Esc: cancelar</i>")
        instrucoes.setWordWrap(True)
        # cor do texto fixa junto com o fundo: no tema escuro do Windows o texto padrão é branco
        instrucoes.setStyleSheet("background:#f4f4f4; color:#1e1e1e; padding:6px; border-radius:4px;")

        self.lista = QListWidget()
        self.lista.currentRowChanged.connect(self._linha_lista)
        self.lista.itemDoubleClicked.connect(lambda *_: self.alterar_tipo())

        self.bt_nova = QPushButton("Nova ROI")
        self.bt_nova.clicked.connect(self.nova)
        self.bt_tipo = QPushButton("Alterar direção/tipo")
        self.bt_tipo.clicked.connect(self.alterar_tipo)
        self.bt_inverter = QPushButton("Inverter sentido")
        self.bt_inverter.setToolTip("Troca a linha 1 com a linha 2")
        self.bt_inverter.clicked.connect(self.inverter)
        self.bt_apagar = QPushButton("Apagar")
        self.bt_apagar.clicked.connect(self.apagar)
        self.bt_ajustar = QPushButton("Ajustar à tela")
        self.bt_ajustar.clicked.connect(self.vista.ajustar)
        self.bt_carregar = QPushButton("Carregar...")
        self.bt_carregar.clicked.connect(self.carregar_de_arquivo)
        self.bt_salvar = QPushButton("Salvar")
        self.bt_salvar.clicked.connect(self.salvar)
        self.bt_avancar = QPushButton("Avançar para a saída  →")
        self.bt_avancar.setEnabled(False)
        self.estado = QLabel("")
        self.estado.setWordWrap(True)

        lado = QVBoxLayout()
        lado.addWidget(instrucoes)
        lado.addWidget(self.bt_nova)
        lado.addWidget(QLabel("ROIs deste vídeo (máx. 8):"))
        lado.addWidget(self.lista, 1)
        for linha in ((self.bt_tipo, self.bt_inverter), (self.bt_apagar, self.bt_ajustar),
                      (self.bt_carregar, self.bt_salvar)):
            h = QHBoxLayout()
            for b in linha:
                h.addWidget(b)
            lado.addLayout(h)
        lado.addWidget(self.estado)
        lado.addWidget(self.bt_avancar)
        painel = QWidget()
        painel.setLayout(lado)
        painel.setFixedWidth(300)

        esquerda = QVBoxLayout()
        esquerda.addWidget(self.dica)
        esquerda.addWidget(self.vista, 1)
        raiz = QHBoxLayout(self)
        raiz.addLayout(esquerda, 1)
        raiz.addWidget(painel)
        self._atualizar_botoes()

    # ---------------------------------------------------------- fonte
    def definir_fonte(self, info, quadro):
        """Chamado quando a aba 1 valida (ou troca o snapshot)."""
        mesma_fonte = self.info is not None and self.info.entrada == info.entrada
        self.info, self.quadro = info, quadro
        self.vista.definir_fundo(para_pixmap(quadro))
        if mesma_fonte and self.cfg is not None:
            return  # só trocou o snapshot: as ROIs continuam
        self.cfg = ConfigRois(camera_id=self.obter_camera_id(), fonte=info.entrada,
                              video_id=info.video_id, titulo=info.titulo,
                              resolucao=(info.largura, info.altura))
        self.salvo = False
        self.selecionada = None
        self._redesenhar()
        self.dica.setText("Clique em <b>Nova ROI</b> para começar.")
        self.estado.setText("")
        existente = self.cfg.caminho()
        if existente.exists() and self.perguntar_ao_carregar:
            r = QMessageBox.question(self, "ROIs salvas",
                                     f"Já existem ROIs salvas para “{self.cfg.camera_id}”.\nCarregar?")
            if r == QMessageBox.Yes:
                self._carregar(existente)
        self._atualizar_botoes()

    # ---------------------------------------------------------- ações
    def nova(self):
        if self.cfg is None:
            return
        if not self.cfg.combinacoes_livres():
            QMessageBox.information(self, "ROIs", "As 8 combinações de direção e tipo já foram usadas.")
            return
        self.vista.iniciar_desenho()
        self.vista.setFocus()

    def _desenho_concluido(self, pts):
        roi = Roi("cima", "origem", pts[:2], pts[2:])
        problemas = roi.problemas()
        if problemas:
            self.dica.setText("✖ ROI descartada: " + "; ".join(problemas) + ". Clique em Nova ROI.")
            return
        dlg = DialogoRoi(self.cfg.combinacoes_livres(), parent=self)
        if dlg.exec() != QDialog.Accepted:
            self.dica.setText("ROI descartada.")
            return
        roi.direcao, roi.tipo = dlg.escolha()
        self.cfg.adicionar(roi)
        self.dica.setText(f"✔ ROI {roi.nome} criada. Arraste os pontos para ajustar.")
        self._alterou(selecionar=roi)

    def alterar_tipo(self):
        roi = self.selecionada
        if roi is None:
            return
        dlg = DialogoRoi(self.cfg.combinacoes_livres(exceto=roi), (roi.direcao, roi.tipo), self)
        if dlg.exec() == QDialog.Accepted:
            roi.direcao, roi.tipo = dlg.escolha()
            self.cfg.ordenar()
            self._alterou(selecionar=roi)

    def inverter(self):
        if self.selecionada is not None:
            self.selecionada.inverter()
            self._alterou(selecionar=self.selecionada)

    def apagar(self):
        roi = self.selecionada
        if roi is None:
            return
        if QMessageBox.question(self, "Apagar", f"Apagar a ROI {roi.nome}?") == QMessageBox.Yes:
            self.cfg.rois.remove(roi)
            self._alterou(selecionar=None)

    def salvar(self) -> bool:
        if self.cfg is None:
            return False
        camera_id = self.obter_camera_id()
        if not camera_id:
            QMessageBox.warning(self, "Salvar", "Preencha o identificador da câmera na aba 1.")
            return False
        problemas = self.cfg.problemas()
        if problemas:
            QMessageBox.warning(self, "Salvar", "Corrija antes de salvar:\n• " + "\n• ".join(problemas))
            return False
        alertas = self.cfg.alertas_sentido()
        if alertas:
            texto = "\n".join(f"• {n}: {m}" for n, m in alertas.items())
            r = QMessageBox.question(self, "Conferir sentido",
                                     f"Estas ROIs parecem com o sentido trocado:\n{texto}\n\n"
                                     "Use “Inverter sentido” ou “Alterar direção/tipo” se for engano.\n"
                                     "Salvar mesmo assim?")
            if r != QMessageBox.Yes:
                return False
        self.cfg.camera_id = camera_id
        destino = self.cfg.caminho()
        if destino.exists() and not self.salvo:
            r = QMessageBox.question(self, "Salvar", f"{destino.name} já existe. Substituir?")
            if r != QMessageBox.Yes:
                return False
        ok, jpg = cv2.imencode(".jpg", self.quadro, [cv2.IMWRITE_JPEG_QUALITY, 92])
        caminho = self.cfg.salvar(snapshot_jpg=jpg.tobytes() if ok else None)
        self.salvo = True
        self.estado.setToolTip(str(caminho))
        self._atualizar_botoes(f"✔ Salvo: {caminho.parent.name}\\{caminho.name}")
        return True

    def carregar_de_arquivo(self):
        caminho, _ = QFileDialog.getOpenFileName(self, "Carregar ROIs", str(pasta_config()), "ROIs (*.json)")
        if caminho:
            self._carregar(caminho)

    def _carregar(self, caminho):
        try:
            cfg = ConfigRois.carregar(caminho, (self.info.largura, self.info.altura))
        except Exception as e:
            QMessageBox.warning(self, "Carregar", f"Não foi possível carregar:\n{e}")
            return
        cfg.fonte, cfg.video_id, cfg.titulo = self.info.entrada, self.info.video_id, self.info.titulo
        cfg.camera_id = self.obter_camera_id() or cfg.camera_id
        self.cfg = cfg
        self.salvo = True
        self._redesenhar()
        self.dica.setText(f"✔ {len(cfg.rois)} ROIs carregadas.")
        self._atualizar_botoes()

    # ---------------------------------------------------------- estado
    def _alterou(self, selecionar=...):
        self.salvo = False
        if selecionar is not ...:
            self.selecionada = selecionar
            self._redesenhar()
        else:
            self._atualizar_lista()
        self._atualizar_botoes()

    def _redesenhar(self):
        self.vista.mostrar(self.cfg.rois if self.cfg else [])
        if self.selecionada not in (self.cfg.rois if self.cfg else []):
            self.selecionada = None
        self.vista.selecionar(self.selecionada)
        self._atualizar_lista()

    def _atualizar_lista(self):
        self.lista.blockSignals(True)
        self.lista.clear()
        alertas = self.cfg.alertas_sentido() if self.cfg else {}
        for r in (self.cfg.rois if self.cfg else []):
            amostra = QPixmap(14, 14)
            amostra.fill(QColor(CORES[r.nome]))
            texto = f"{r.nome}   ({ROTULO_DIRECAO[r.direcao].split()[-1]} {r.tipo})"
            it = QListWidgetItem(QIcon(amostra), ("⚠ " + texto) if r.nome in alertas else texto)
            if r.nome in alertas:
                it.setToolTip(f"Conferir: {alertas[r.nome]}")
            self.lista.addItem(it)
            if r is self.selecionada:
                self.lista.setCurrentItem(it)
        self.lista.blockSignals(False)

    def _linha_lista(self, linha):
        if self.cfg and 0 <= linha < len(self.cfg.rois):
            self._selecionar(self.cfg.rois[linha])

    def _selecionar(self, roi):
        self.selecionada = roi
        self.vista.selecionar(roi)
        self._atualizar_lista()
        self._atualizar_botoes()

    def pronto(self) -> bool:
        return self.cfg is not None and self.salvo and not self.cfg.problemas()

    def _atualizar_botoes(self, msg: str | None = None):
        tem = self.selecionada is not None
        for b in (self.bt_tipo, self.bt_inverter, self.bt_apagar):
            b.setEnabled(tem)
        self.bt_nova.setEnabled(self.cfg is not None and bool(self.cfg.combinacoes_livres()))
        self.bt_salvar.setEnabled(self.cfg is not None and bool(self.cfg.rois))
        problemas = self.cfg.problemas() if self.cfg else ["sem fonte"]
        pronto = self.salvo and not problemas
        self.bt_avancar.setEnabled(pronto)
        alertas = self.cfg.alertas_sentido() if self.cfg else {}
        if alertas and not msg:
            self.estado.setText(f"⚠ Conferir o sentido de: {', '.join(alertas)}"
                                + ("" if self.salvo else "\n● Alterações não salvas."))
            self.estado.setStyleSheet("color:#a15c00;")
        elif msg:
            self.estado.setText(msg)
            self.estado.setStyleSheet("color:#1b7f2a;")
        elif self.cfg and self.cfg.rois and not self.salvo:
            self.estado.setText("● Alterações não salvas." + (f"\nFalta: {problemas[0]}" if problemas else ""))
            self.estado.setStyleSheet("color:#a15c00;")
        elif not pronto:
            self.estado.setText("")
        self.estado_mudou.emit(pronto)
