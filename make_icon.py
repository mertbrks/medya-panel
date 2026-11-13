"""medya_panel.ico üretir: panelin cam balonu gibi (koyu cam, parlama, ışıklı kenar, yeşil halka, oynat)."""
import io
from PIL import Image
from PySide6.QtCore import QBuffer, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QGuiApplication, QImage, QLinearGradient, QPainter, QPainterPath, QPen, QRadialGradient

GREEN = QColor("#1ED760")


def render(s):
    img = QImage(s, s, QImage.Format_ARGB32); img.fill(Qt.transparent)
    p = QPainter(img); p.setRenderHint(QPainter.Antialiasing)
    u = s / 256
    body = QRectF(10 * u, 10 * u, 236 * u, 236 * u)
    c = body.center()
    # cam gövde: koyu, altta hafif yeşil ışıma
    g = QRadialGradient(QPointF(c.x(), c.y() + 70 * u), 170 * u)
    g.setColorAt(0, QColor(30, 70, 45, 245)); g.setColorAt(0.6, QColor(24, 26, 28, 240)); g.setColorAt(1, QColor(14, 14, 16, 235))
    p.setPen(Qt.NoPen); p.setBrush(g); p.drawEllipse(body)
    # üst parlama
    sheen = QPainterPath(); sheen.addEllipse(QRectF(body.x() + 22 * u, body.y() + 8 * u, body.width() - 44 * u, body.height() * 0.52))
    sg = QLinearGradient(body.topLeft(), QPointF(body.x(), body.y() + body.height() * 0.4))
    sg.setColorAt(0, QColor(255, 255, 255, 70)); sg.setColorAt(1, QColor(255, 255, 255, 0))
    p.fillPath(sheen, sg)
    # ışıklı kenar
    rim = QLinearGradient(body.topLeft(), body.bottomRight())
    rim.setColorAt(0, QColor(255, 255, 255, 150)); rim.setColorAt(0.5, QColor(255, 255, 255, 25)); rim.setColorAt(1, QColor(255, 255, 255, 90))
    p.setBrush(Qt.NoBrush); p.setPen(QPen(rim, max(1.0, 3 * u))); p.drawEllipse(body.adjusted(1.5 * u, 1.5 * u, -1.5 * u, -1.5 * u))
    # ilerleme halkası
    ring = body.adjusted(30 * u, 30 * u, -30 * u, -30 * u)
    w = max(1.6, 14 * u)
    p.setPen(QPen(QColor(255, 255, 255, 45), w)); p.drawEllipse(ring)
    p.setPen(QPen(GREEN, w, Qt.SolidLine, Qt.RoundCap)); p.drawArc(ring, 90 * 16, -int(360 * 16 * 0.68))
    if s <= 24:   # küçük boyutta yalnızca yeşil üçgen, daire karışıklık yapıyor
        tri = QPainterPath(); tri.moveTo(c.x() - 30 * u, c.y() - 46 * u); tri.lineTo(c.x() + 50 * u, c.y()); tri.lineTo(c.x() - 30 * u, c.y() + 46 * u); tri.closeSubpath()
        p.setPen(Qt.NoPen); p.setBrush(GREEN); p.drawPath(tri); p.end()
        return to_pil(img)
    # ortada yeşil oynat düğmesi
    r = 58 * u
    p.setPen(Qt.NoPen); p.setBrush(GREEN); p.drawEllipse(c, r, r)
    tri = QPainterPath(); tri.moveTo(c.x() - 18 * u, c.y() - 28 * u); tri.lineTo(c.x() + 30 * u, c.y()); tri.lineTo(c.x() - 18 * u, c.y() + 28 * u); tri.closeSubpath()
    p.setBrush(QColor("#000000")); p.drawPath(tri)
    p.end()
    return to_pil(img)


def to_pil(img):
    buf = QBuffer(); buf.open(QBuffer.ReadWrite); img.save(buf, "PNG")
    return Image.open(io.BytesIO(bytes(buf.data())))


if __name__ == "__main__":
    app = QGuiApplication([])
    sizes = [256, 64, 48, 32, 24, 16]
    imgs = [render(s) for s in sizes]   # her boyut ayrı çizilir, küçükler bulanıklaşmasın
    imgs[0].save("medya_panel.ico", sizes=[(s, s) for s in sizes], append_images=imgs[1:])
    imgs[0].save("medya_panel_icon.png")
