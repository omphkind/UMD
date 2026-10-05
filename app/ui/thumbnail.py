"""Bounded thumbnail decoding; cached pixmaps never retain full-size photos."""
from PySide6.QtCore import QByteArray, QBuffer, QIODevice, Qt
from PySide6.QtGui import QImageReader, QPixmap


def thumbnail_pixmap(data, size):
    if len(data) > 8_000_000:
        return QPixmap()
    buffer = QBuffer()
    buffer.setData(QByteArray(data))
    buffer.open(QIODevice.OpenModeFlag.ReadOnly)
    reader = QImageReader(buffer)
    reader.setAutoTransform(True)
    dimensions = reader.size()
    if not dimensions.isValid() or dimensions.width() * dimensions.height() > 100_000_000:
        return QPixmap()
    reader.setScaledSize(dimensions.scaled(size, Qt.AspectRatioMode.KeepAspectRatio))
    image = reader.read()
    return QPixmap.fromImage(image) if not image.isNull() else QPixmap()
