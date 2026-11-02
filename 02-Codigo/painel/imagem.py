"""Conversão de quadros OpenCV (BGR) para imagens Qt e o quadro que mostra imagens."""
import cv2
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import QLabel, QSizePolicy


def para_pixmap(img_bgr) -> QPixmap:
    rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
    h, w = rgb.shape[:2]
    return QPixmap.fromImage(QImage(rgb.data, w, h, 3 * w, QImage.Format_RGB888).copy())


class ImagemAjustavel(QLabel):
    """Mostra a imagem inteira, mantendo a proporção, em qualquer tamanho de janela."""

    def __init__(self, texto=""):
        super().__init__(texto)
        self._pixmap = None
        self.setAlignment(Qt.AlignCenter)
        self.setMinimumSize(320, 180)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setStyleSheet("background:#202020; color:#aaaaaa;")

    def definir(self, pixmap: QPixmap | None):
        self._pixmap = pixmap
        self._redesenhar()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._redesenhar()

    def _redesenhar(self):
        if self._pixmap is not None:
            self.setPixmap(self._pixmap.scaled(self.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))
