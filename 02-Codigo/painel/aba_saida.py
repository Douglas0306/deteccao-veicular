"""Aba 3: pasta de saída, intervalo dos arquivos, duração da coleta e opções."""
import os
import threading
from datetime import datetime, timedelta
from pathlib import Path

from PySide6.QtCore import QTime, Signal
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout,
                               QGroupBox, QHBoxLayout, QLabel, QLineEdit, QPushButton, QRadioButton,
                               QSpinBox, QTimeEdit, QVBoxLayout, QWidget)

from nucleo.detector import gpu_disponivel, gpu_principal, listar_gpus

from .caminhos import modelo_padrao, pasta_modelos, pasta_saida_padrao

INTERVALOS = (5, 10, 15, 30, 60)
# modelos prontos (arquivo dentro da pasta de modelos): só aparecem os que existem
MODELOS_PRONTOS = (
    ("Dia (modelo atual, vehicle_stage2_full_v5)", "vehicle_stage2_full_v5_best.onnx"),
    ("Noite (treinado com imagens noturnas)", "vehicle_stage4_noite_luz_best.onnx"),
)


class AbaSaida(QWidget):
    estado_mudou = Signal(bool)  # True = configuração válida
    _gpu_encontrada = Signal(str)

    def __init__(self):
        super().__init__()
        self.ao_vivo = True
        self._gpu_encontrada.connect(self._mostrar_gpu)

        # ---- pasta
        self.pasta = QLineEdit(str(pasta_saida_padrao()))
        self.pasta.textChanged.connect(self.validar)
        bt_pasta = QPushButton("Escolher...")
        bt_pasta.clicked.connect(self._escolher_pasta)
        linha_pasta = QHBoxLayout()
        linha_pasta.addWidget(self.pasta, 1)
        linha_pasta.addWidget(bt_pasta)
        self.exemplo = QLabel()
        self.exemplo.setWordWrap(True)
        self.exemplo.setStyleSheet("color:#777;")

        self.intervalo = QComboBox()
        for m in INTERVALOS:
            self.intervalo.addItem(f"a cada {m} minutos" if m < 60 else "a cada 60 minutos (hora cheia)", m)
        self.intervalo.setCurrentIndex(INTERVALOS.index(15))
        self.intervalo.currentIndexChanged.connect(self.validar)

        caixa_saida = QGroupBox("Arquivos")
        f1 = QFormLayout(caixa_saida)
        f1.addRow("Pasta de saída:", linha_pasta)
        f1.addRow("Novo arquivo CSV:", self.intervalo)
        f1.addRow("", self.exemplo)

        # ---- duração
        self.r_parar = QRadioButton("Até clicar em Parar")
        self.r_minutos = QRadioButton("Por")
        self.r_horario = QRadioButton("Até o horário")
        self.grupo = QButtonGroup(self)
        for i, r in enumerate((self.r_parar, self.r_minutos, self.r_horario)):
            self.grupo.addButton(r, i)
        self.r_parar.setChecked(True)
        self.minutos = QSpinBox()
        self.minutos.setRange(1, 7 * 24 * 60)
        self.minutos.setValue(60)
        self.minutos.setSuffix(" minutos")
        self.horario = QTimeEdit(QTime.currentTime().addSecs(3600))
        self.horario.setDisplayFormat("HH:mm")
        self.nota_horario = QLabel()
        self.nota_horario.setStyleSheet("color:#777;")
        self.grupo.idToggled.connect(lambda *_: self.validar())
        self.minutos.valueChanged.connect(self.validar)
        self.horario.timeChanged.connect(self.validar)
        caixa_dur = QGroupBox("Duração da coleta")
        v = QVBoxLayout(caixa_dur)
        v.addWidget(self.r_parar)
        h = QHBoxLayout()
        h.addWidget(self.r_minutos)
        h.addWidget(self.minutos)
        h.addStretch(1)
        v.addLayout(h)
        h = QHBoxLayout()
        h.addWidget(self.r_horario)
        h.addWidget(self.horario)
        h.addWidget(self.nota_horario)
        h.addStretch(1)
        v.addLayout(h)
        self.nota_duracao = QLabel()
        self.nota_duracao.setStyleSheet("color:#777;")
        self.nota_duracao.setWordWrap(True)
        v.addWidget(self.nota_duracao)

        # ---- opções
        self.anotado = QCheckBox("Gravar vídeo anotado para auditoria (ocupa muito disco: ~1,5 GB por hora)")
        self.graficos = QCheckBox("Gerar gráficos PNG a cada arquivo fechado e um resumo ao fim da execução "
                                  "(pasta graficos, junto dos CSVs)")
        self.graficos.setChecked(True)
        caixa_op = QGroupBox("Opções")
        v_op = QVBoxLayout(caixa_op)
        v_op.addWidget(self.graficos)
        v_op.addWidget(self.anotado)

        # ---- avançado
        self.modelo = QLineEdit(str(modelo_padrao()))
        self.modelo.textChanged.connect(self.validar)
        bt_modelo = QPushButton("Escolher...")
        bt_modelo.clicked.connect(self._escolher_modelo)
        linha_modelo = QHBoxLayout()
        linha_modelo.addWidget(self.modelo, 1)
        linha_modelo.addWidget(bt_modelo)
        self.modelo_pre = QComboBox()
        for rotulo, arquivo in MODELOS_PRONTOS:
            if (pasta_modelos() / arquivo).is_file():
                self.modelo_pre.addItem(rotulo, str(pasta_modelos() / arquivo))
        self.modelo_pre.addItem("Outro arquivo...", None)
        self.modelo_pre.setToolTip("Dia: modelo usado até aqui. Noite: o mesmo modelo treinado também com imagens noturnas\n"
                                   "(melhor à noite, um pouco pior de dia). Os dois recebem a imagem normal, sem tratamento.")
        self.modelo_pre.activated.connect(self._modelo_escolhido)
        self.imgsz = QComboBox()
        for s in (640, 960, 1280):
            self.imgsz.addItem(f"{s} px", s)
        self.imgsz.setCurrentIndex(1)
        self.imgsz.setToolTip("Cada quadro é redimensionado para este tamanho antes da detecção, qualquer que "
                              "seja a resolução do vídeo.\nMaior enxerga veículos pequenos/distantes, mas é mais "
                              "lento. O modelo da equipe foi treinado em 960.")
        self.dispositivo = QComboBox()
        self.dispositivo.addItem("CPU", "cpu")
        self.dispositivo.setToolTip("GPU usa o DirectML do Windows (NVIDIA, AMD ou Intel), sem instalar nada.\n"
                                    "Se a GPU falhar, a coleta continua na CPU e avisa.")
        self.nota_gpu = QLabel("procurando placa de vídeo...")
        self.nota_gpu.setStyleSheet("color:#777;")
        linha_disp = QHBoxLayout()
        linha_disp.addWidget(self.dispositivo, 1)
        linha_disp.addWidget(self.nota_gpu)
        self.conf = QSpinBox()
        self.conf.setRange(5, 95)
        self.conf.setSingleStep(5)
        self.conf.setValue(25)
        self.conf.setSuffix(" %")
        self.conf.setToolTip("Só detecções com pelo menos esta confiança viram um veículo novo.\n"
                             "Aumente se objetos (bancos, placas) estiverem sendo tomados por veículos;\n"
                             "diminua se veículos de verdade estiverem sendo perdidos.")
        self.fps = QSpinBox()
        self.fps.setRange(2, 15)
        self.fps.setValue(6)
        self.fps.setSuffix(" quadros/s")
        self.vel = QSpinBox()
        self.vel.setRange(2, 50)
        self.vel.setValue(10)
        self.vel.setSuffix(" % do próprio tamanho por segundo")
        self.vel.setToolTip("A velocidade é medida em relação ao tamanho do veículo na imagem, para valer igual\n"
                            "perto e longe da câmera (longe, o veículo parece menor e anda menos pixels).\n"
                            "10% = desloca menos de um décimo do próprio tamanho por segundo: praticamente parado.")
        self.tmin = QDoubleSpinBox()
        self.tmin.setRange(1.0, 10.0)
        self.tmin.setSingleStep(0.5)
        self.tmin.setValue(2.0)
        self.tmin.setSuffix(" s")
        self.janela = QSpinBox()
        self.janela.setRange(2, 60)
        self.janela.setValue(5)
        self.janela.setSuffix(" s")
        self.janela.setToolTip("CSV de veículos: espera_sem_movimento = verdadeiro se, enquanto o veículo esperava\n"
                               "antes da origem, o cruzamento ficou pelo menos este tempo sem nenhum veículo andando.")
        caixa_av = QGroupBox("Avançado")
        f2 = QFormLayout(caixa_av)
        f2.addRow("Modelo:", self.modelo_pre)
        f2.addRow("Arquivo do modelo (.onnx):", linha_modelo)
        f2.addRow("Processar com:", linha_disp)
        f2.addRow("Resolução de entrada do modelo:", self.imgsz)
        f2.addRow("Confiança mínima da detecção:", self.conf)
        f2.addRow("Quadros analisados por segundo:", self.fps)
        f2.addRow("Parado = anda menos de:", self.vel)
        f2.addRow("…por pelo menos:", self.tmin)
        f2.addRow("Espera com cruzamento livre por pelo menos:", self.janela)
        threading.Thread(target=self._procurar_gpu, daemon=True).start()

        self.status = QLabel()
        self.status.setWordWrap(True)
        self.bt_avancar = QPushButton("Avançar para a execução  →")

        raiz = QVBoxLayout(self)
        for w in (caixa_saida, caixa_dur, caixa_op, caixa_av):
            raiz.addWidget(w)
        raiz.addStretch(1)
        raiz.addWidget(self.status)
        h = QHBoxLayout()
        h.addStretch(1)
        h.addWidget(self.bt_avancar)
        raiz.addLayout(h)
        self.camera_id = "camera"
        self.validar()

    # ---------------------------------------------------------- fonte
    def definir_fonte(self, ao_vivo: bool, camera_id: str):
        self.ao_vivo, self.camera_id = ao_vivo, camera_id or "camera"
        self.r_horario.setEnabled(ao_vivo)
        self.horario.setEnabled(ao_vivo)
        if not ao_vivo and self.r_horario.isChecked():
            self.r_parar.setChecked(True)
        self.validar()

    # ---------------------------------------------------------- ações
    def _escolher_pasta(self):
        d = QFileDialog.getExistingDirectory(self, "Pasta de saída", self.pasta.text())
        if d:
            self.pasta.setText(d.replace("/", "\\"))

    def _modelo_escolhido(self, indice: int):
        caminho = self.modelo_pre.itemData(indice)
        if caminho:
            self.modelo.setText(caminho.replace("/", "\\"))
        else:
            self._escolher_modelo()

    def _escolher_modelo(self):
        f, _ = QFileDialog.getOpenFileName(self, "Modelo de detecção", str(pasta_modelos()), "ONNX (*.onnx)")
        if f:
            self.modelo.setText(f.replace("/", "\\"))

    def horario_fim(self) -> datetime:
        t = self.horario.time()
        agora = datetime.now().astimezone()
        fim = agora.replace(hour=t.hour(), minute=t.minute(), second=0, microsecond=0)
        return fim if fim > agora else fim + timedelta(days=1)

    def valores(self) -> dict:
        modo = {0: "parar", 1: "minutos", 2: "horario"}[self.grupo.checkedId()]
        return {
            "pasta_saida": self.pasta.text().strip(), "intervalo_min": self.intervalo.currentData(),
            "modo_duracao": modo, "minutos": self.minutos.value(),
            "horario": self.horario_fim() if modo == "horario" else None,
            "video_anotado": self.anotado.isChecked(), "graficos": self.graficos.isChecked(),
            "modelo": self.modelo.text().strip(),
            "imgsz": self.imgsz.currentData(), "fps_alvo": float(self.fps.value()),
            "vel_limiar": self.vel.value() / 100, "tempo_parado_min": self.tmin.value(),
            "conf_minima": self.conf.value() / 100, "dispositivo": self.dispositivo.currentData(),
            "janela_sem_movimento_s": float(self.janela.value()),
        }

    # ---------------------------------------------------------- GPU
    def _procurar_gpu(self):
        """Em segundo plano: consultar o Windows leva ~1 s."""
        nome = gpu_principal(listar_gpus()) if gpu_disponivel() else None
        self._gpu_encontrada.emit(nome or "")

    def _mostrar_gpu(self, nome: str):
        if nome:
            self.dispositivo.addItem(f"GPU — {nome}", "gpu")
            self.dispositivo.setCurrentIndex(1)  # GPU por padrão quando existe
            self.nota_gpu.setText("")
        else:
            self.nota_gpu.setText("nenhuma GPU compatível encontrada")

    def resumo(self) -> str:
        v = self.valores()
        dur = {"parar": "até clicar em Parar", "minutos": f"por {v['minutos']} min",
               "horario": f"até {v['horario']:%d/%m %H:%M}" if v["horario"] else ""}[v["modo_duracao"]]
        return f"{dur} · arquivos a cada {v['intervalo_min']} min · {v['pasta_saida']}"

    def validar(self, *_):
        v = self.valores()
        erros = []
        pasta = Path(v["pasta_saida"]) if v["pasta_saida"] else None
        if pasta is None or not pasta.is_absolute():
            erros.append("escolha a pasta de saída (caminho completo)")
        else:
            # a pasta só é criada ao iniciar; aqui basta a parte que já existe aceitar gravação
            existente = next((p for p in (pasta, *pasta.parents) if p.exists()), None)
            if existente is None or not existente.is_dir() or not os.access(existente, os.W_OK):
                erros.append("sem permissão para gravar na pasta de saída")
        if not Path(v["modelo"]).is_file():
            erros.append("arquivo do modelo não encontrado")
        m = v["intervalo_min"]
        agora = datetime.now()
        ini = agora.replace(minute=agora.minute // m * m, second=0, microsecond=0)
        dia = f"{ini:%Y-%m-%d}"
        self.exemplo.setText(f"Ex.: {pasta or '...'}\\{self.camera_id}\\{dia}\\veiculos_{self.camera_id}_"
                             f"{ini:%Y%m%d_%H%M}.csv e parados_..._{ini:%H%M}.csv (horário do computador)")
        if v["modo_duracao"] == "horario":
            amanha = v["horario"].date() != datetime.now().date()
            self.nota_horario.setText("(amanhã)" if amanha else "(hoje)")
        else:
            self.nota_horario.setText("")
        self.nota_duracao.setText("" if self.ao_vivo else
                                  "Arquivo ou vídeo gravado: termina no fim do vídeo; “Por N minutos” "
                                  "conta minutos do vídeo.")
        self.status.setText("✖ " + "; ".join(erros) if erros else "")
        self.status.setStyleSheet("color:#b3261e;" if erros else "")
        self.valida = not erros
        self.bt_avancar.setEnabled(self.valida)
        self.estado_mudou.emit(self.valida)
        return not erros
