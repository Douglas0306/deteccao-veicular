"""Janela principal do Contador de Veículos O/D (PySide6).

Abas, na ordem de uso: 1 Fonte do vídeo -> 2 ROIs -> 3 Saída -> 4 Execução.
Cada aba só é liberada quando a anterior está válida; durante a coleta as abas 1 a 3 travam.
"""
import random
import sys

from PySide6.QtCore import QObject, QRegularExpression, Qt, QThread, Signal
from PySide6.QtGui import QRegularExpressionValidator
from PySide6.QtWidgets import (QApplication, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
                               QLineEdit, QMainWindow, QMessageBox, QProgressBar, QPushButton,
                               QTabWidget, QVBoxLayout, QWidget)

from .aba_execucao import AbaExecucao
from .aba_saida import AbaSaida
from .editor_rois import AbaRois
from .fonte import (EXTENSOES_VIDEO, ErroFonte, InfoFonte, capturar_snapshot, resolver_fonte,
                    sugerir_camera_id)
from .imagem import ImagemAjustavel, para_pixmap

TITULO = "Contador de Veículos O/D"


# ---------------------------------------------------------------- trabalho em segundo plano
class _Trabalho(QObject):
    concluido = Signal(object, object)  # InfoFonte, quadro
    falhou = Signal(str)

    def __init__(self, texto: str | None = None, info: InfoFonte | None = None, momento_s=None):
        super().__init__()
        self.texto, self.info, self.momento_s = texto, info, momento_s

    def executar(self):
        try:
            info = self.info or resolver_fonte(self.texto)
            quadro = capturar_snapshot(info, self.momento_s)
            self.concluido.emit(info, quadro)
        except ErroFonte as e:
            self.falhou.emit(str(e))
        except Exception as e:  # qualquer outra falha vira mensagem, sem derrubar a janela
            self.falhou.emit(f"Erro inesperado: {e}")


# ---------------------------------------------------------------- aba 1
# Sugestão inicial do campo Fonte: live da câmera 4 Corners (Coldwater), que já tem ROIs em 01-Config
LINK_SUGERIDO = "https://www.youtube.com/watch?v=1H0iTzv2jiQ"


class AbaFonte(QWidget):
    fonte_validada = Signal(object, object)  # InfoFonte, quadro
    fonte_invalidada = Signal()

    def __init__(self):
        super().__init__()
        self.info: InfoFonte | None = None
        self.quadro = None
        self._thread = None

        self.campo = QLineEdit(LINK_SUGERIDO)  # já preenchido: basta clicar em Validar ou trocar o link
        self.campo.setPlaceholderText("Cole o link do YouTube (live ou vídeo) ou escolha um arquivo de vídeo")
        self.campo.returnPressed.connect(self.validar)
        self.campo.textEdited.connect(self._mudou_entrada)
        self.bt_arquivo = QPushButton("Arquivo...")
        self.bt_arquivo.clicked.connect(self.escolher_arquivo)
        self.bt_validar = QPushButton("Validar")
        self.bt_validar.setDefault(True)
        self.bt_validar.clicked.connect(self.validar)
        linha = QHBoxLayout()
        linha.addWidget(QLabel("Fonte:"))
        linha.addWidget(self.campo, 1)
        linha.addWidget(self.bt_arquivo)
        linha.addWidget(self.bt_validar)

        self.ocupado = QProgressBar()
        self.ocupado.setRange(0, 0)
        self.ocupado.setMaximumHeight(6)
        self.ocupado.setTextVisible(False)
        self.ocupado.hide()
        self.status = QLabel("Aguardando a fonte do vídeo.")
        self.status.setWordWrap(True)

        # dados da fonte
        self.rotulos = {k: QLabel("—") for k in ("tipo", "titulo", "ao_vivo", "resolucao", "fps", "duracao")}
        self.rotulos["titulo"].setWordWrap(True)
        form = QFormLayout()
        for chave, nome in (("tipo", "Tipo"), ("titulo", "Título"), ("ao_vivo", "Ao vivo"),
                            ("resolucao", "Resolução"), ("fps", "Quadros/s"), ("duracao", "Duração")):
            form.addRow(nome + ":", self.rotulos[chave])
        self.camera_id = QLineEdit()
        self.camera_id.setPlaceholderText("ex.: ginza_sukiyabashi")
        self.camera_id.setValidator(QRegularExpressionValidator(QRegularExpression(r"[a-z0-9_]{1,40}")))
        self.camera_id.setToolTip("Nome curto do local: só letras minúsculas, números e _. "
                                  "Usado nos nomes das pastas e arquivos de saída.")
        self.camera_id.setEnabled(False)
        form.addRow("Identificador:", self.camera_id)
        caixa = QGroupBox("Dados do vídeo")
        caixa.setLayout(form)
        caixa.setFixedWidth(330)

        # snapshot
        self.imagem = ImagemAjustavel("O snapshot do vídeo aparece aqui depois de validar.")
        self.bt_outro = QPushButton("Outro quadro")
        self.bt_outro.setToolTip("Tira outro snapshot (útil se o quadro atual tem um ônibus escondendo a via)")
        self.bt_outro.setEnabled(False)
        self.bt_outro.clicked.connect(self.outro_quadro)
        self.bt_avancar = QPushButton("Avançar para as ROIs  →")
        self.bt_avancar.setEnabled(False)
        botoes = QHBoxLayout()
        botoes.addWidget(self.bt_outro)
        botoes.addStretch(1)
        botoes.addWidget(self.bt_avancar)
        lado_dir = QVBoxLayout()
        lado_dir.addWidget(self.imagem, 1)
        lado_dir.addLayout(botoes)

        corpo = QHBoxLayout()
        esquerda = QVBoxLayout()
        esquerda.addWidget(caixa)
        esquerda.addStretch(1)
        corpo.addLayout(esquerda)
        corpo.addLayout(lado_dir, 1)

        raiz = QVBoxLayout(self)
        raiz.addLayout(linha)
        raiz.addWidget(self.ocupado)
        raiz.addWidget(self.status)
        raiz.addLayout(corpo, 1)

    # ---------------------------------------------------------------- ações
    def escolher_arquivo(self):
        filtro = "Vídeos (" + " ".join(f"*{e}" for e in sorted(EXTENSOES_VIDEO)) + ");;Todos (*.*)"
        caminho, _ = QFileDialog.getOpenFileName(self, "Escolher vídeo", "", filtro)
        if caminho:
            self.campo.setText(caminho.replace("/", "\\"))
            self.validar()

    def validar(self):
        texto = self.campo.text().strip()
        self._limpar()
        self._rodar(_Trabalho(texto=texto), "Validando a fonte..." if texto else "")

    def outro_quadro(self):
        if not self.info:
            return
        momento = None
        if not self.info.ao_vivo and self.info.duracao_s:
            momento = random.uniform(0, max(0.0, self.info.duracao_s - 1))
        self._rodar(_Trabalho(info=self.info, momento_s=momento), "Capturando outro quadro...")

    def _rodar(self, trabalho: _Trabalho, msg: str):
        if self._thread is not None:
            return
        self._ocupado(True, msg)
        self._thread = QThread(self)
        self._trabalho = trabalho
        trabalho.moveToThread(self._thread)
        self._thread.started.connect(trabalho.executar)
        trabalho.concluido.connect(self._ok)
        trabalho.falhou.connect(self._erro)
        trabalho.concluido.connect(self._thread.quit)
        trabalho.falhou.connect(self._thread.quit)
        self._thread.finished.connect(self._fim_thread)
        self._thread.start()

    def _fim_thread(self):
        self._thread.deleteLater()
        self._thread = None

    def _ok(self, info: InfoFonte, quadro):
        novo = self.info is None or info is not self.info
        self.info, self.quadro = info, quadro
        self._ocupado(False)
        r = self.rotulos
        r["tipo"].setText("Arquivo local" if info.tipo == "arquivo" else f"Link ({info.extrator or 'site'})")
        r["titulo"].setText(info.titulo)
        r["ao_vivo"].setText("Sim" if info.ao_vivo else "Não")
        r["resolucao"].setText(f"{info.largura} × {info.altura}")
        r["fps"].setText(f"{info.fps:.1f}" if info.fps else "—")
        r["duracao"].setText("contínua (live)" if info.ao_vivo else _duracao(info.duracao_s))
        if novo:
            self.camera_id.setText(sugerir_camera_id(info))
        self.camera_id.setEnabled(True)
        self.imagem.definir(para_pixmap(quadro))
        self.bt_outro.setEnabled(True)
        self.bt_avancar.setEnabled(True)
        avisos = "  ".join(info.avisos)
        self.status.setText("✔ Fonte válida. Confira o snapshot: ele será usado para mapear as ROIs."
                            + (f"  ⚠ {avisos}" if avisos else ""))
        self.status.setStyleSheet("color:#1b7f2a;")
        self.fonte_validada.emit(info, quadro)

    def _erro(self, msg: str):
        self._ocupado(False)
        self.status.setText("✖ " + msg)
        self.status.setStyleSheet("color:#b3261e;")
        if self.info is None:
            self.fonte_invalidada.emit()

    def _limpar(self):
        self.info, self.quadro = None, None
        for r in self.rotulos.values():
            r.setText("—")
        self.camera_id.clear()
        self.camera_id.setEnabled(False)
        self.imagem.definir(None)
        self.imagem.setText("O snapshot do vídeo aparece aqui depois de validar.")
        self.bt_outro.setEnabled(False)
        self.bt_avancar.setEnabled(False)
        self.fonte_invalidada.emit()

    def _mudou_entrada(self):
        if self.info is not None:
            self._limpar()
            self.status.setText("Fonte alterada: clique em Validar.")
            self.status.setStyleSheet("")

    def _ocupado(self, sim: bool, msg: str = ""):
        self.ocupado.setVisible(sim)
        for w in (self.bt_validar, self.bt_arquivo, self.campo, self.bt_outro):
            w.setEnabled(not sim)
        if sim:
            self.status.setText(msg)
            self.status.setStyleSheet("")
        elif self.info is None:
            self.bt_outro.setEnabled(False)


def _duracao(s):
    if not s:
        return "—"
    s = int(round(s))
    return f"{s // 3600:d}:{s % 3600 // 60:02d}:{s % 60:02d}"


# ---------------------------------------------------------------- janela
class JanelaPrincipal(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(TITULO)
        self.resize(1200, 720)
        self.abas = QTabWidget()
        self.aba_fonte = AbaFonte()
        self.aba_rois = AbaRois(self._camera_id)
        self.aba_saida = AbaSaida()
        self.aba_exec = AbaExecucao(self._contexto)
        for aba, nome in ((self.aba_fonte, "1. Fonte do vídeo"), (self.aba_rois, "2. ROIs"),
                          (self.aba_saida, "3. Saída"), (self.aba_exec, "4. Execução")):
            self.abas.addTab(aba, nome)
        self.setCentralWidget(self.abas)
        self.aba_fonte.fonte_validada.connect(self._fonte_validada)
        self.aba_fonte.fonte_invalidada.connect(self._recalcular)
        self.aba_fonte.camera_id.textChanged.connect(lambda *_: self._fonte_para_saida())
        self.aba_fonte.bt_avancar.clicked.connect(lambda: self.abas.setCurrentIndex(1))
        self.aba_rois.estado_mudou.connect(lambda *_: self._recalcular())
        self.aba_rois.bt_avancar.clicked.connect(lambda: self.abas.setCurrentIndex(2))
        self.aba_saida.estado_mudou.connect(lambda *_: self._recalcular())
        self.aba_saida.bt_avancar.clicked.connect(lambda: self.abas.setCurrentIndex(3))
        self.aba_exec.rodando_mudou.connect(lambda *_: self._recalcular())
        self.abas.currentChanged.connect(self._aba_mudou)
        self._recalcular()
        self.statusBar().showMessage("1. Informe a fonte do vídeo")

    # ---------------------------------------------------------- ligação entre as abas
    def _camera_id(self) -> str:
        return self.aba_fonte.camera_id.text().strip()

    def _contexto(self):
        return (self.aba_fonte.info, self.aba_rois.cfg.caminho(), self._camera_id(), self.aba_saida.valores())

    def _fonte_validada(self, info, quadro):
        self.aba_rois.definir_fonte(info, quadro)
        self._fonte_para_saida()
        self._recalcular()

    def _fonte_para_saida(self):
        if self.aba_fonte.info is not None:
            self.aba_saida.definir_fonte(self.aba_fonte.info.ao_vivo, self._camera_id())

    def _recalcular(self):
        """Libera as abas em cadeia: fonte válida -> ROIs salvas -> saída válida."""
        rodando = self.aba_exec.rodando()
        ok = [True, self.aba_fonte.info is not None, False, False]
        ok[2] = ok[1] and self.aba_rois.pronto()
        ok[3] = ok[2] and self.aba_saida.valida  # (isEnabled() mente quando a aba está travada)
        for i, liberada in enumerate(ok):
            self.abas.setTabEnabled(i, (i == 3) if rodando else liberada)
        self.aba_saida.bt_avancar.setVisible(ok[2])
        if ok[3]:
            self.aba_exec.atualizar_resumo(f"<b>{self._camera_id()}</b> · {len(self.aba_rois.cfg.rois)} ROIs · "
                                           + self.aba_saida.resumo())
        self.aba_exec.bt_iniciar.setEnabled(ok[3] and not rodando)
        if not ok[self.abas.currentIndex()] and not rodando:
            self.abas.setCurrentIndex(max(i for i, v in enumerate(ok) if v))

    def _aba_mudou(self, i):
        mensagens = ("1. Informe a fonte do vídeo", "2. Desenhe as ROIs sobre o snapshot e salve",
                     "3. Escolha a pasta de saída, o intervalo dos arquivos e a duração",
                     "4. Inicie a coleta")
        self.statusBar().showMessage(mensagens[i])
        if i == 1:
            self.aba_rois.vista.ajustar()
        if i == 3:
            self._recalcular()

    def closeEvent(self, e):
        if self.aba_fonte._thread is not None:
            QMessageBox.information(self, TITULO, "Aguarde a validação terminar.")
            e.ignore()
            return
        if self.aba_exec.rodando():
            resp = QMessageBox.question(self, TITULO, "A coleta está rodando. Parar (os arquivos serão "
                                        "fechados corretamente) e sair?")
            if resp != QMessageBox.Yes:
                e.ignore()
                return
            self.aba_exec.parar_e_aguardar()
        r = self.aba_rois
        if r.cfg is not None and r.cfg.rois and not r.salvo:
            resp = QMessageBox.question(self, TITULO, "Há ROIs não salvas. Sair mesmo assim?")
            if resp != QMessageBox.Yes:
                e.ignore()
                return
        super().closeEvent(e)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(TITULO)
    janela = JanelaPrincipal()
    janela.show()
    sys.exit(app.exec())
