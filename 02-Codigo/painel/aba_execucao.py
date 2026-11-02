"""Aba 4: iniciar/parar a coleta, prévia ao vivo e estado."""
import copy
import json
import os
from datetime import datetime

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (QFormLayout, QGroupBox, QHBoxLayout, QLabel, QMessageBox, QProgressBar,
                               QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from nucleo.leitor import inicio_pelo_nome
from nucleo.motor import ConfigColeta, Motor

from .fonte import resolver_fonte
from .imagem import ImagemAjustavel, para_pixmap

ESTADOS = {"iniciando": "Iniciando...", "conectando": "Conectando ao vídeo...", "rodando": "● Coletando",
           "reconectando": "⟳ Stream caiu: reconectando...", "finalizando": "Fechando arquivos...",
           "encerrado": "Encerrado"}
MOTIVOS = {"parado_pelo_usuario": "parado pelo usuário", "duracao_atingida": "duração atingida",
           "horario_atingido": "horário atingido", "fim_do_video": "fim do vídeo", "erro": "erro"}


class _Ponte(QObject):
    """Leva os avisos das threads do motor para a thread da janela."""
    preview = Signal(object)
    estado = Signal(object)
    fim = Signal(object)


def _hms(s: float) -> str:
    s = int(s)
    return f"{s // 3600:d}:{s % 3600 // 60:02d}:{s % 60:02d}"


class AbaExecucao(QWidget):
    rodando_mudou = Signal(bool)

    def __init__(self, obter_contexto):
        """obter_contexto() -> (info da fonte, caminho do JSON de ROIs, camera_id, valores da aba 3)."""
        super().__init__()
        self.obter_contexto = obter_contexto
        self.motor: Motor | None = None
        self._ativo = False
        self.ponte = _Ponte()
        self.ponte.preview.connect(lambda img: self.imagem.definir(para_pixmap(img)))
        self.ponte.estado.connect(self._mostrar_estado)
        self.ponte.fim.connect(self._terminou)

        self.resumo = QLabel()
        self.resumo.setWordWrap(True)
        self.bt_iniciar = QPushButton("▶  Iniciar")
        self.bt_parar = QPushButton("■  Parar")
        self.bt_parar.setEnabled(False)
        self.bt_pasta = QPushButton("Abrir pasta de saída")
        for b in (self.bt_iniciar, self.bt_parar):
            b.setMinimumHeight(32)
        self.bt_iniciar.clicked.connect(self.iniciar)
        self.bt_parar.clicked.connect(self.parar)
        self.bt_pasta.clicked.connect(self._abrir_pasta)
        topo = QHBoxLayout()
        topo.addWidget(self.bt_iniciar)
        topo.addWidget(self.bt_parar)
        topo.addWidget(self.resumo, 1)
        topo.addWidget(self.bt_pasta)

        self.imagem = ImagemAjustavel("A prévia do vídeo anotado aparece aqui durante a coleta.")

        self.campos = {}
        form = QFormLayout()
        for chave, nome in (("estado", "Estado"), ("inicio", "Início"), ("decorrido", "Decorrido"),
                            ("horario", "Horário do quadro"), ("processador", "Processando em"),
                            ("qps", "Quadros/s"), ("tela", "Veículos na tela"),
                            ("parados", "Parados agora"), ("registros", "Veículos gravados"),
                            ("situacoes", "  completos / sem destino / sem origem"),
                            ("reconexoes", "Reconexões"), ("sem_video", "Tempo sem vídeo")):
            self.campos[chave] = QLabel("—")
            form.addRow(nome + ":", self.campos[chave])
        self.progresso = QProgressBar()
        self.progresso.setRange(0, 1000)
        self.progresso.setTextVisible(False)
        self.progresso.hide()
        form.addRow("", self.progresso)
        caixa = QGroupBox("Coleta")
        caixa.setLayout(form)

        self.tabela = QTableWidget(0, 2)
        self.tabela.setHorizontalHeaderLabels(["ROI", "Passagens"])
        self.tabela.verticalHeader().hide()
        self.tabela.horizontalHeader().setStretchLastSection(True)
        self.tabela.setEditTriggers(QTableWidget.NoEditTriggers)
        caixa_rois = QGroupBox("Passagens por ROI")
        QVBoxLayout(caixa_rois).addWidget(self.tabela)

        self.arquivos = QLabel()
        self.arquivos.setWordWrap(True)
        self.arquivos.setStyleSheet("color:#777;")
        self.aviso = QLabel()
        self.aviso.setWordWrap(True)

        lado = QVBoxLayout()
        lado.addWidget(caixa)
        lado.addWidget(caixa_rois, 1)
        lado.addWidget(self.arquivos)
        lado.addWidget(self.aviso)
        painel = QWidget()
        painel.setLayout(lado)
        painel.setFixedWidth(340)

        corpo = QHBoxLayout()
        corpo.addWidget(self.imagem, 1)
        corpo.addWidget(painel)
        raiz = QVBoxLayout(self)
        raiz.addLayout(topo)
        raiz.addLayout(corpo, 1)

    # ---------------------------------------------------------- ações
    def atualizar_resumo(self, texto: str):
        if not self.rodando():
            self.resumo.setText(texto)

    def rodando(self) -> bool:
        # indicador próprio: o motor avisa o fim ainda de dentro da sua thread
        return self._ativo

    def iniciar(self):
        info, caminho_rois, camera_id, v = self.obter_contexto()
        try:
            rois = json.loads(caminho_rois.read_text(encoding="utf-8"))
        except Exception as e:
            QMessageBox.warning(self, "Iniciar", f"Não foi possível ler as ROIs salvas:\n{e}")
            return
        inicio_video = None
        if info.tipo == "arquivo":
            inicio_video = inicio_pelo_nome(info.entrada, datetime.now().astimezone().tzinfo)
        entrada = info.entrada
        snapshot = caminho_rois.parent / rois.get("snapshot", "")
        v = dict(v, snapshot=str(snapshot) if rois.get("snapshot") and snapshot.exists() else None)
        cfg = ConfigColeta(
            camera_id=camera_id, video_id=info.video_id, fonte=entrada, stream_url=info.stream_url,
            ao_vivo=info.ao_vivo, rois_json=rois, inicio_video=inicio_video,
            reabrir=(lambda: resolver_fonte(entrada).stream_url) if info.tipo == "link" else None, **v)
        self.tabela.setRowCount(0)
        for k in self.campos.values():
            k.setText("—")
        self.aviso.setText("")
        self.progresso.setVisible(not info.ao_vivo)
        self.progresso.setValue(0)
        self.motor = Motor(cfg, ao_preview=self.ponte.preview.emit,
                           ao_estado=lambda e: self.ponte.estado.emit(copy.deepcopy(e)),
                           ao_fim=lambda e: self.ponte.fim.emit(copy.deepcopy(e)))
        self._ativo = True
        self.motor.iniciar()
        self.bt_iniciar.setEnabled(False)
        self.bt_parar.setEnabled(True)
        self.rodando_mudou.emit(True)

    def parar(self):
        if self.rodando():
            self.bt_parar.setEnabled(False)
            self.campos["estado"].setText(ESTADOS["finalizando"])
            self.motor.parar()

    def parar_e_aguardar(self):
        if self.motor is not None and self.motor.rodando():
            self.motor.parar()
            self.motor.aguardar(30)

    def _abrir_pasta(self):
        _, _, camera_id, v = self.obter_contexto()
        pasta = os.path.join(v["pasta_saida"], camera_id)
        os.makedirs(pasta, exist_ok=True)
        os.startfile(pasta)

    # ---------------------------------------------------------- estado
    def _mostrar_estado(self, e):
        c = self.campos
        c["estado"].setText(ESTADOS.get(e.estado, e.estado))
        c["estado"].setStyleSheet("color:#1b7f2a; font-weight:bold;" if e.estado == "rodando" else
                                  "color:#a15c00; font-weight:bold;" if e.estado == "reconectando" else "")
        c["inicio"].setText(f"{e.inicio:%d/%m/%Y %H:%M:%S}" if e.inicio else "—")
        c["decorrido"].setText(_hms(e.decorrido_s))
        c["horario"].setText(f"{e.horario_video:%d/%m %H:%M:%S}" if e.horario_video else "—")
        c["processador"].setText(e.provedor or "—")
        c["qps"].setText(f"{e.quadros_s:.1f}")
        c["tela"].setText(str(e.veiculos_na_tela))
        c["parados"].setText(str(e.parados_agora))
        c["registros"].setText(str(e.registros))
        s = e.situacoes
        c["situacoes"].setText(f"{s['completo']} / {s['sem_destino']} / {s['sem_origem']}")
        c["reconexoes"].setText(str(e.reconexoes))
        c["sem_video"].setText(_hms(e.tempo_sem_video_s))
        if e.progresso is not None:
            self.progresso.setValue(int(e.progresso * 1000))
        nomes = sorted(e.passagens) if e.passagens else []
        self.tabela.setRowCount(len(nomes))
        for i, n in enumerate(nomes):
            self.tabela.setItem(i, 0, QTableWidgetItem(n))
            item = QTableWidgetItem(str(e.passagens[n]))
            item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.tabela.setItem(i, 1, item)
        arqs = [os.path.basename(a) for a in (e.arquivo_veiculos, e.arquivo_parados) if a]
        self.arquivos.setText("Gravando: " + ", ".join(arqs) if arqs else "")
        if e.erro:
            self.aviso.setText("✖ " + e.erro)
            self.aviso.setStyleSheet("color:#b3261e;")
        elif e.aviso:
            self.aviso.setText("⚠ " + e.aviso)
            self.aviso.setStyleSheet("color:#a15c00;")
        else:
            self.aviso.setText("")

    def _terminou(self, e):
        self._ativo = False
        self._mostrar_estado(e)
        self.bt_iniciar.setEnabled(True)
        self.bt_parar.setEnabled(False)
        self.rodando_mudou.emit(False)
        s = e.situacoes
        msg = (f"Coleta encerrada ({MOTIVOS.get(e.motivo_fim, e.motivo_fim)}).\n\n"
               f"Duração: {_hms(e.decorrido_s)}\nVeículos gravados: {e.registros} "
               f"({s['completo']} completos, {s['sem_destino']} sem destino, {s['sem_origem']} sem origem)\n"
               f"Reconexões: {e.reconexoes}\nGráficos gerados: {e.graficos_gerados}")
        if e.erro:
            QMessageBox.warning(self, "Coleta", msg + f"\n\nErro: {e.erro}")
        else:
            QMessageBox.information(self, "Coleta", msg)
