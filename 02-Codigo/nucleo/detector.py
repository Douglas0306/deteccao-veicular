"""Detecção (YOLO em ONNX Runtime, sem PyTorch) + rastreamento (ByteTrack).

O modelo é exportado uma vez com a Ultralytics (ferramentas/exportar_modelo.py) e aqui
roda só com onnxruntime, que é o que vai dentro do .exe. Os nomes das classes vêm dos
metadados do próprio .onnx, então trocar de modelo (ex.: o da equipe, classe única
"vehicle", ou um COCO) é só trocar o arquivo.
"""
import ast
from dataclasses import dataclass

import cv2
import numpy as np
import onnxruntime as ort
import supervision as sv
from trackers import ByteTrackTracker

# nome da classe no modelo -> classe gravada no CSV (classes sem tradução são ignoradas)
NOME_PARA_CLASSE = {
    "person": "pedestre", "bicycle": "bicicleta", "car": "carro", "motorcycle": "moto",
    "bus": "pesado", "truck": "pesado", "vehicle": "veiculo",
}
CLASSES_VEICULO = ("moto", "carro", "pesado", "bicicleta", "veiculo")


@dataclass
class Deteccao:
    track_id: int
    xyxy: tuple          # caixa no quadro inteiro
    conf: float
    classe: str          # classe deste quadro (a final sai do voto na trilha)

    @property
    def base(self) -> tuple:
        """Centro da borda inferior: o ponto que toca o asfalto."""
        x1, _, x2, y2 = self.xyxy
        return ((x1 + x2) / 2.0, y2)

    @property
    def altura(self) -> float:
        return self.xyxy[3] - self.xyxy[1]


def _nomes_do_modelo(sessao) -> dict[int, str]:
    meta = sessao.get_modelmeta().custom_metadata_map
    if "names" in meta:
        return {int(k): str(v) for k, v in ast.literal_eval(meta["names"]).items()}
    raise ValueError("o modelo ONNX não traz os nomes das classes (exporte com ferramentas/exportar_modelo.py)")


def gpu_disponivel() -> bool:
    """DirectML (vem no onnxruntime-directml): usa a GPU pelo DirectX 12 do Windows, como um jogo,
    com placa NVIDIA, AMD ou Intel, sem instalar CUDA nem drivers extras."""
    return "DmlExecutionProvider" in ort.get_available_providers()


def _powershell() -> str:
    """PowerShell pelo PATH ou, se o PATH do Windows estiver incompleto (sem System32), pela pasta do sistema."""
    import os
    import shutil
    achado = shutil.which("powershell")
    if achado:
        return achado
    return os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "WindowsPowerShell", "v1.0",
                        "powershell.exe")


def listar_gpus() -> list[str]:
    """Nomes das placas de vídeo do Windows (só para mostrar ao usuário)."""
    import subprocess
    try:
        r = subprocess.run([_powershell(), "-NoProfile", "-Command",
                            "(Get-CimInstance Win32_VideoController).Name"], capture_output=True, text=True,
                           timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        nomes = [n.strip() for n in r.stdout.splitlines() if n.strip()]
    except Exception:
        return []
    return [n for n in nomes if "basic" not in n.lower() and "remote" not in n.lower()]


def gpu_principal(nomes: list[str]) -> str | None:
    """A que o DirectML escolhe com "high_performance": a dedicada, se houver."""
    dedicadas = [n for n in nomes if any(k in n.lower() for k in ("nvidia", "amd", "radeon", "arc"))]
    return (dedicadas or nomes or [None])[0]


class DetectorONNX:
    def __init__(self, caminho_modelo, imgsz=640, conf_min=0.10, iou_nms=0.50,
                 recorte=None, threads=0, dispositivo="cpu"):
        self.aviso = ""
        self.sessao = None
        self._gpu = False
        if dispositivo == "gpu":
            if gpu_disponivel():
                try:
                    self._abrir(caminho_modelo, threads, gpu=True)
                except Exception as e:  # placa sem DirectX 12 etc.
                    self.aviso = f"GPU indisponível ({type(e).__name__}); usando a CPU."
                    self.sessao = None
            else:
                self.aviso = "Esta instalação não tem suporte a GPU; usando a CPU."
        if self.sessao is None:
            self._abrir(caminho_modelo, threads, gpu=False)
        self.provedor = self.sessao.get_providers()[0]
        self.entrada = self.sessao.get_inputs()[0].name
        forma = self.sessao.get_inputs()[0].shape  # [1, 3, H, W]; H/W texto se dinâmico
        self.dinamico = not isinstance(forma[2], int)
        self.imgsz, self.conf_min, self.iou_nms = imgsz, conf_min, iou_nms
        self.recorte = recorte  # (x0, y0, x1, y1) ou None = quadro inteiro

        self.nomes = _nomes_do_modelo(self.sessao)
        uteis = {i: NOME_PARA_CLASSE[n] for i, n in self.nomes.items() if n in NOME_PARA_CLASSE}
        if not uteis:
            raise ValueError(f"o modelo não tem classes de veículo conhecidas: {self.nomes}")
        self.classe_de = uteis                                   # id do modelo -> classe do CSV
        self.ids_uteis = np.array(sorted(uteis))
        self.id_pessoa = next((i for i, c in uteis.items() if c == "pedestre"), None)
        self.ids_duas_rodas = [i for i, c in uteis.items() if c in ("moto", "bicicleta")]

    def _abrir(self, caminho_modelo, threads, gpu: bool):
        opcoes = ort.SessionOptions()
        if threads:
            opcoes.intra_op_num_threads = threads
        if gpu:
            # exigências do DirectML; "high_performance" escolhe a placa dedicada (a integrada
            # da Intel chegou a ser mais lenta que a CPU nos testes)
            opcoes.enable_mem_pattern = False
            opcoes.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
            provedores = [("DmlExecutionProvider", {"performance_preference": "high_performance",
                                                    "device_filter": "gpu"}), "CPUExecutionProvider"]
        else:
            provedores = ["CPUExecutionProvider"]
        self.sessao = ort.InferenceSession(str(caminho_modelo), opcoes, providers=provedores)
        # Sem quadro de teste aqui: o DirectML otimiza para o 1o tamanho de entrada que recebe;
        # um teste de 320x320 deixava os quadros reais (960x544) 10x mais lentos (157 ms x 15 ms).
        self._modelo, self._threads, self._gpu = caminho_modelo, threads, gpu
        self._forma = None

    def _inferir(self, blob):
        """Roda o modelo. Na GPU: se falhar (driver antigo etc.) volta para a CPU; se o tamanho
        da entrada mudar (ex.: live voltou em outra resolução), recria a sessão para esse tamanho."""
        if self._gpu and self._forma is not None and blob.shape != self._forma:
            self._abrir(self._modelo, self._threads, gpu=True)
        try:
            saida = self.sessao.run(None, {self.entrada: blob})[0]
        except Exception as e:
            if not self._gpu:
                raise
            self.aviso = f"GPU falhou ({type(e).__name__}); continuando na CPU."
            self._abrir(self._modelo, self._threads, gpu=False)
            self.provedor = self.sessao.get_providers()[0]
            saida = self.sessao.run(None, {self.entrada: blob})[0]
        self._forma = blob.shape
        return saida

    def _preparar(self, img):
        h, w = img.shape[:2]
        r = self.imgsz / max(h, w)
        nw, nh = round(w * r), round(h * r)
        if self.dinamico:  # retângulo múltiplo de 32: menos pixels de enchimento, mais rápido
            lw, lh = -(-nw // 32) * 32, -(-nh // 32) * 32
        else:
            lw = lh = self.imgsz
        px, py = (lw - nw) // 2, (lh - nh) // 2
        tela = np.full((lh, lw, 3), 114, np.uint8)
        tela[py:py + nh, px:px + nw] = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_LINEAR)
        blob = cv2.dnn.blobFromImage(tela, 1 / 255.0, swapRB=True)
        return blob, r, px, py

    def detectar(self, quadro):
        """Retorna (xyxy Nx4, conf N, id_classe N) no sistema de coordenadas do quadro inteiro."""
        x0, y0 = 0, 0
        img = quadro
        if self.recorte:
            x0, y0, x1, y1 = self.recorte
            img = quadro[y0:y1, x0:x1]
        blob, r, px, py = self._preparar(img)
        pred = self._inferir(blob)[0].T  # (N, 4 + n_classes)
        notas = pred[:, 4 + self.ids_uteis]
        melhor = notas.argmax(1)
        conf = notas[np.arange(len(notas)), melhor]
        ok = conf >= self.conf_min
        if not ok.any():
            return np.empty((0, 4), np.float32), np.empty(0, np.float32), np.empty(0, int)
        cx, cy, bw, bh = pred[ok, :4].T
        conf, ids = conf[ok], self.ids_uteis[melhor[ok]]
        caixas = np.stack([(cx - bw / 2 - px) / r + x0, (cy - bh / 2 - py) / r + y0,
                           (cx + bw / 2 - px) / r + x0, (cy + bh / 2 - py) / r + y0], 1)
        # NMS sem distinguir carro/ônibus/caminhão (o mesmo veículo às vezes sai com duas
        # classes); pessoas formam um grupo à parte para não apagarem bicicletas e motos.
        grupo = (ids != (self.id_pessoa if self.id_pessoa is not None else -1)).astype(int)
        xywh = np.column_stack([caixas[:, :2], caixas[:, 2:] - caixas[:, :2]])
        manter = cv2.dnn.NMSBoxesBatched(xywh.tolist(), conf.tolist(), grupo.tolist(),
                                         self.conf_min, self.iou_nms)
        manter = np.array(manter, int).reshape(-1)
        return caixas[manter].astype(np.float32), conf[manter].astype(np.float32), ids[manter]


def remover_pessoa_sobre_veiculo(xyxy, conf, ids, id_pessoa, ids_duas_rodas, fracao=0.30):
    """Descarta pessoa cuja caixa está coberta por uma moto ou bicicleta (é o condutor)."""
    if id_pessoa is None or not ids_duas_rodas:
        return xyxy, conf, ids
    pessoas = np.where(ids == id_pessoa)[0]
    duas_rodas = np.where(np.isin(ids, ids_duas_rodas))[0]
    if len(pessoas) == 0 or len(duas_rodas) == 0:
        return xyxy, conf, ids
    p, v = xyxy[pessoas][:, None, :], xyxy[duas_rodas][None, :, :]
    iw = np.clip(np.minimum(p[..., 2], v[..., 2]) - np.maximum(p[..., 0], v[..., 0]), 0, None)
    ih = np.clip(np.minimum(p[..., 3], v[..., 3]) - np.maximum(p[..., 1], v[..., 1]), 0, None)
    area_p = ((p[..., 2] - p[..., 0]) * (p[..., 3] - p[..., 1])).clip(1e-6)
    cobertas = ((iw * ih) / area_p).max(1) >= fracao
    manter = np.ones(len(ids), bool)
    manter[pessoas[cobertas]] = False
    return xyxy[manter], conf[manter], ids[manter]


class Rastreador:
    """Dois ByteTrack: veículos e pedestres, para uma pessoa nunca herdar o ID de um carro."""

    def __init__(self, cfg_rast: dict, fps: float, detector: DetectorONNX, fracao_pessoa=0.30):
        parametros = dict(
            lost_track_buffer=round(cfg_rast["memoria_s"] * 30),  # o trackers conta em quadros de 30 fps
            frame_rate=fps,
            track_activation_threshold=cfg_rast["conf_ativacao"],
            high_conf_det_threshold=cfg_rast["conf_alta"],
            minimum_iou_threshold=cfg_rast["iou_min"],
            minimum_consecutive_frames=cfg_rast["quadros_confirmacao"],
        )
        self.veiculos = ByteTrackTracker(**parametros)
        self.pessoas = ByteTrackTracker(**parametros)
        self.classe_de = detector.classe_de
        self.id_pessoa = detector.id_pessoa
        self.ids_duas_rodas = detector.ids_duas_rodas
        self.fracao_pessoa = fracao_pessoa

    def atualizar(self, xyxy, conf, ids, t: float) -> tuple[list, list]:
        xyxy, conf, ids = remover_pessoa_sobre_veiculo(xyxy, conf, ids, self.id_pessoa,
                                                       self.ids_duas_rodas, self.fracao_pessoa)
        eh_pessoa = ids == (self.id_pessoa if self.id_pessoa is not None else -1)
        saida = []
        for rastreador, mascara in ((self.veiculos, ~eh_pessoa), (self.pessoas, eh_pessoa)):
            det = sv.Detections(xyxy=xyxy[mascara], confidence=conf[mascara],
                                class_id=ids[mascara].astype(int))
            res = rastreador.update(det, timestamp=t)
            lista = []
            for caixa, c, k, tid in zip(res.xyxy, res.confidence, res.class_id, res.tracker_id):
                if tid < 0:  # trilha ainda não confirmada
                    continue
                lista.append(Deteccao(int(tid), tuple(float(v) for v in caixa), float(c),
                                      self.classe_de[int(k)]))
            saida.append(lista)
        return saida[0], saida[1]
