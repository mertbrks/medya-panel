"""Medya Panel: ekran köşesinde taşınabilir, tıklayınca büyüyen medya + ses kontrolü."""
import asyncio, ctypes, sys, threading, time

from PySide6.QtCore import QSizeF, QEasingCurve, QVariantAnimation, QObject, QPoint, QPointF, QRectF, QSettings, Qt, QTimer, Signal
from PySide6.QtGui import QCursor, QTransform, QColor, QFont, QFontMetrics, QImage, QPainter, QPainterPath, QPen, QPixmap, QLinearGradient
from PySide6.QtWidgets import QApplication, QMenu, QWidget
from pycaw.pycaw import AudioUtilities
from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as Manager
from winrt.windows.storage.streams import Buffer, InputStreamOptions

PLAYING = 4
APP_NAMES = {"msedge": "Edge", "chrome": "Chrome", "firefox": "Firefox", "spotify": "Spotify", "vlc": "VLC"}


def app_name(aumid):
    name = aumid.split("!")[-1].removesuffix(".exe").split(".")[-1]
    return APP_NAMES.get(name.lower(), name[:1].upper() + name[1:])


class Media(QObject):
    """Windows medya oturumunu arka plandaki asyncio döngüsünde izler."""
    updated = Signal(dict)

    def __init__(self):
        super().__init__()
        self.loop = asyncio.new_event_loop()
        self.session = None
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        asyncio.run_coroutine_threadsafe(self.poll(), self.loop)

    async def thumb(self, ref):
        try:
            stream = await ref.open_read_async()
            buf = Buffer(stream.size)
            await stream.read_async(buf, stream.size, InputStreamOptions.READ_AHEAD)
            return bytes(memoryview(buf))
        except Exception:
            return None

    async def poll(self):
        manager = await Manager.request_async()
        key, art = None, None
        while True:
            info = {}
            try:
                s = self.session = manager.get_current_session()
                if s:
                    p = await s.try_get_media_properties_async()
                    tl, pb = s.get_timeline_properties(), s.get_playback_info()
                    if (p.title, p.artist) != key:
                        key, art = (p.title, p.artist), (await self.thumb(p.thumbnail) if p.thumbnail else None)
                    info = dict(title=p.title, artist=p.artist, app=app_name(s.source_app_user_model_id), art=art,
                                playing=pb.playback_status == PLAYING, pos=tl.position.total_seconds(),
                                end=tl.end_time.total_seconds(), stamp=tl.last_updated_time.timestamp())
            except Exception:
                pass
            self.updated.emit(info)
            await asyncio.sleep(0.5)

    def do(self, name, *args):
        async def run():
            if self.session:
                await getattr(self.session, name)(*args)
        asyncio.run_coroutine_threadsafe(run(), self.loop)


class Volume:
    def __init__(self):
        self.ep = AudioUtilities.GetSpeakers().EndpointVolume

    def get(self):
        return self.ep.GetMasterVolumeLevelScalar()

    def set(self, v):
        self.ep.SetMasterVolumeLevelScalar(max(0.0, min(1.0, v)), None)
        if v > 0 and self.muted():
            self.ep.SetMute(0, None)

    def muted(self):
        return bool(self.ep.GetMute())

    def toggle_mute(self):
        self.ep.SetMute(0 if self.muted() else 1, None)


# --- görünüm ---
M = 14                      # gölge payı
BUBBLE = 54
PAD = 42                    # serbest balon üzerine gelince iki yana bu kadar uzar (hap olur)
W, H = 340, 156             # açık kart boyutu (gölge hariç)
DW, DL = 52, 228            # kenar damlası penceresi (uzamış hali sığsın; saydam yerler tıklamayı geçirir)
DROP_L, DROP_D = 96, 34     # duran damla: kenar boyunca uzunluk, derinlik
BTN_GAP = 36                # damla uzayınca düğmeler arası
SNAP = 56                   # kenara bu kadar yakın bırakılırsa damla olur
GRIP = 24                   # uzayınca düğmelerin yanında açılan tutma yeri (çizgili)
# Spotify paleti
GREEN = QColor("#1ED760")
BG, LINE, TXT, DIM = QColor("#121212"), QColor(255, 255, 255, 20), QColor("#FFFFFF"), QColor("#B3B3B3")
ICON = "Segoe Fluent Icons"
G = dict(prev="", play="", pause="", next="", vol="", mute="", note="", min="")


def fmt(sec):
    sec = int(max(sec, 0))
    return f"{sec // 60}:{sec % 60:02d}"


class Panel(QWidget):
    def __init__(self):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setMouseTracking(True)
        self.setWindowTitle("Medya Panel")
        self.media, self.vol = Media(), Volume()
        self.media.updated.connect(self.on_info)
        self.info, self.art, self.accent = {}, None, GREEN
        self.expanded, self.press, self.dragging, self.hover, self.grab_slider = False, None, False, None, None
        self.vol_flash, self.t, self.done = 0.0, 1.0, None
        self.anim = QVariantAnimation(self)
        self.anim.valueChanged.connect(lambda v: (setattr(self, "t", v), self.update()))
        self.anim.finished.connect(lambda: self.done and self.done())
        self.settings = QSettings("mertbrks", "medya-panel")
        scr = QApplication.primaryScreen().availableGeometry()
        default = QPoint(scr.right() - BUBBLE - 2 * M - 2 * PAD - 16, scr.bottom() - BUBBLE - 2 * M - 16)
        self.bubble_pos = self.settings.value("pos", default)
        self.edge = self.settings.value("edge", "") or None   # None: serbest balon
        self.grow, self.backdrop, self.last_grab = 0.0, None, 0.0
        self.morph = None                       # balon -> damla geçişi sürerken: (balonun çemberi, damla penceresinin yeri)
        self.morph_t = 0.0
        self.morph_anim = QVariantAnimation(self, startValue=0.0, endValue=1.0, duration=420)
        self.morph_anim.valueChanged.connect(lambda v: (setattr(self, "morph_t", v), self.update()))
        self.morph_anim.finished.connect(self.morph_done)
        self.grow_anim = QVariantAnimation(self, duration=260, easingCurve=QEasingCurve.OutCubic)
        self.grow_anim.valueChanged.connect(lambda v: (setattr(self, "grow", v), self.update()))
        self.pop, self.slide, self.hl = 1.0, 0.0, 1.0
        self.drag_off, self.drag_delta = QPoint(), QPoint()
        self.pop_anim = QVariantAnimation(self, duration=320, easingCurve=QEasingCurve.OutBack)
        self.pop_anim.valueChanged.connect(lambda v: (setattr(self, "pop", v), self.update()))
        self.slide_anim = QVariantAnimation(self, startValue=1.0, endValue=0.0, duration=260, easingCurve=QEasingCurve.OutCubic)
        self.slide_anim.valueChanged.connect(self.on_slide)
        self.hl_anim = QVariantAnimation(self, startValue=0.0, endValue=1.0, duration=140, easingCurve=QEasingCurve.OutCubic)
        self.hl_anim.valueChanged.connect(lambda v: (setattr(self, "hl", v), self.update()))
        self.set_mode(False)
        self.tick = QTimer(self, timeout=self.on_tick, interval=200)
        self.tick.start()

    def showEvent(self, e):
        # cam için arkadaki ekranı yakalarken panelin kendisi görüntüye girmesin (WDA_EXCLUDEFROMCAPTURE)
        ctypes.windll.user32.SetWindowDisplayAffinity(int(self.winId()), 0x11)
        QTimer.singleShot(50, self.refresh_backdrop)
        super().showEvent(e)

    def on_tick(self):
        self.refresh_backdrop()
        self.update()

    def refresh_backdrop(self):
        """Cam efekti: pencerenin arkasındaki ekranı yakala ve bulanıklaştır."""
        scr = self.screen() or QApplication.primaryScreen()
        g, sg = self.geometry(), scr.geometry()
        img = scr.grabWindow(0, g.x() - sg.x(), g.y() - sg.y(), g.width(), g.height()).toImage()
        if img.isNull():
            return
        small = img.scaled(max(1, img.width() // 14), max(1, img.height() // 14), Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        mid = small.scaled(max(1, img.width() // 4), max(1, img.height() // 4), Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        self.backdrop = QPixmap.fromImage(mid)
        self.last_grab = time.time()

    def glass(self, p, path, tint, rim_path=None):
        p.save()
        p.setClipPath(path, Qt.IntersectClip)
        if self.backdrop:
            tf0 = p.transform(); p.resetTransform()
            p.drawPixmap(QRectF(self.rect()), self.backdrop, QRectF(self.backdrop.rect()))
            p.setTransform(tf0)
        p.fillPath(path, tint)
        box = path.boundingRect()
        sheen = QLinearGradient(box.topLeft(), box.bottomLeft())
        sheen.setColorAt(0, QColor(255, 255, 255, 34)); sheen.setColorAt(0.45, QColor(255, 255, 255, 6)); sheen.setColorAt(1, QColor(255, 255, 255, 0))
        p.fillPath(path, sheen)
        p.restore()
        rim = QLinearGradient(box.topLeft(), box.bottomRight())
        rim.setColorAt(0, QColor(255, 255, 255, 90)); rim.setColorAt(0.5, QColor(255, 255, 255, 18)); rim.setColorAt(1, QColor(255, 255, 255, 50))
        p.setBrush(Qt.NoBrush); p.setPen(QPen(rim, 1)); p.drawPath(rim_path or path)

    # kenar damlası
    def collapsed_size(self):
        if not self.edge:
            return BUBBLE + 2 * M + 2 * PAD + GRIP, BUBBLE + 2 * M
        return (DW, DL) if self.edge in ("left", "right") else (DL, DW)

    def shape_rect(self):
        """Kapalı haldeki görünen şeklin pencere içi yeri."""
        return QRectF(M + PAD, M, BUBBLE, BUBBLE) if not self.edge else QRectF(0, 0, *self.collapsed_size())

    def dock(self, center):
        """Ekranın en yakın kenarına yapış ya da serbest balon ol; bubble_pos'u ayarlar."""
        scr = (self.screen() or QApplication.primaryScreen()).availableGeometry()
        x, y = center.x(), center.y()
        d, edge = min((x - scr.left(), "left"), (scr.right() - x, "right"), (y - scr.top(), "top"), (scr.bottom() - y, "bottom"))
        self.edge = edge if d < SNAP else None
        w, h = self.collapsed_size()
        if not self.edge:
            self.bubble_pos = self.clamp(QPoint(x - w // 2, y - h // 2), w, h, M + PAD)
        else:
            along_y = max(scr.top(), min(y - h // 2, scr.bottom() + 1 - h))
            along_x = max(scr.left(), min(x - w // 2, scr.right() + 1 - w))
            self.bubble_pos = {"left": QPoint(scr.left(), along_y), "right": QPoint(scr.right() + 1 - w, along_y),
                               "top": QPoint(along_x, scr.top()), "bottom": QPoint(along_x, scr.bottom() + 1 - h)}[self.edge]
        self.settings.setValue("pos", self.bubble_pos); self.settings.setValue("edge", self.edge or "")

    def drop_transform(self):
        """Sağ kenar için çizilen damlayı asıl kenara çevirir."""
        w, h = self.collapsed_size()
        return {"right": QTransform(), "left": QTransform(-1, 0, 0, 1, w, 0),
                "bottom": QTransform(0, 1, 1, 0, 0, 0), "top": QTransform(0, -1, 1, 0, 0, h)}[self.edge]

    def paint_drop(self, p, k=1.0, art=True):
        w, g = DW, self.grow                        # sağ kenar düzleminde
        L = DROP_L + 124 * g                         # fare gelince damla kenar boyunca uzar...
        d = (DROP_D + 12 * g) * k                   # ...ve biraz şişer; k: geçişte kabarma
        py, y0 = DL / 2, (DL - L) / 2
        path = QPainterPath(); path.moveTo(w + 1, y0)
        # kenardan dikey çıkıp yuvarlak bir göbeğe açılan S kavis (sıvı damlası gibi)
        a = L / 2
        path.cubicTo(w, y0 + a * 0.5, w - d, y0 + a * (0.2 - 0.12 * g), w - d, py)
        path.cubicTo(w - d, py + a * (0.8 + 0.12 * g), w, py + a * 0.5, w + 1, y0 + L)
        rim = QPainterPath(path)             # ışıklı kenar yalnızca kavisli tarafta, ekran kenarında değil
        path.closeSubpath()
        tf = self.drop_transform()
        self.glass(p, tf.map(path), QColor(18, 18, 18, 150), tf.map(rim))
        cx = w - (DROP_D + 12 * g) * 0.52
        c = tf.map(QPointF(cx, py))
        rad = 8.5
        if not art:
            return c, rad
        # uzayınca: kapak söner, yerine önceki / oynat / sonraki düğmeleri belirir
        show = max(0.0, (g - 0.35) / 0.65)
        self.paint_buttons(p, self.drop_buttons(tf, cx, py - 12), show)
        self.paint_grip(p, self.grip_point(), show)
        if g > 0.5:
            return
        p.save(); p.setOpacity(1 - g * 2)
        inner = QRectF(c.x() - rad, c.y() - rad, rad * 2, rad * 2)
        self.draw_art(p, inner, rad)
        i = self.info
        if i and not i["playing"]:
            p.setPen(Qt.NoPen); p.setBrush(QColor(0, 0, 0, 120)); p.drawEllipse(inner)
            p.save(); p.translate(c); p.scale(0.6, 0.6); self.icon(p, QPointF(1, 0), "play", TXT); p.restore()
        ring = inner.adjusted(-2.5, -2.5, 2.5, 2.5)
        self.draw_ring(p, ring, 2)
        if time.time() < self.vol_flash:
            p.setPen(Qt.NoPen); p.setBrush(QColor(0, 0, 0, 190)); p.drawEllipse(ring)
            f = QFont("Segoe UI Variable Display"); f.setPixelSize(11); f.setBold(True)
            p.setFont(f); p.setPen(TXT)
            p.drawText(ring, Qt.AlignCenter, "×" if self.vol.muted() else f"{round(self.vol.get() * 100)}")
        p.restore()

    def paint_buttons(self, p, items, show):
        """Üzerine gelince çıkan önceki / oynat / sonraki düğmeleri (damla ve balon ortak)."""
        if show <= 0:
            return
        p.save(); p.setOpacity(show)
        playing = bool(self.info and self.info["playing"])
        for c, name, kind in items:
            if name == "dplay":
                r_ = 13 + (1.5 * self.hl if self.hover == name else 0)
                p.setPen(Qt.NoPen); p.setBrush(QColor("#3BE477") if self.hover == name else self.accent)
                p.drawEllipse(c, r_, r_)
                if time.time() < self.vol_flash:  # tekerlekle ses ayarlanırken yüzdeyi göster
                    f = QFont("Segoe UI Variable Display"); f.setPixelSize(11); f.setBold(True)
                    p.setFont(f); p.setPen(QColor("#000000"))
                    p.drawText(QRectF(c.x() - 13, c.y() - 13, 26, 26), Qt.AlignCenter, "×" if self.vol.muted() else f"{round(self.vol.get() * 100)}")
                else:
                    self.icon(p, c, "pause" if playing else "play", QColor("#000000"))
            else:
                if self.hover == name:
                    p.setPen(Qt.NoPen); p.setBrush(QColor(255, 255, 255, int(26 * self.hl))); p.drawEllipse(c, 13, 13)
                self.icon(p, c, kind, TXT if self.hover == name else QColor("#D9D9D9"))
        p.restore()

    def grip_point(self):
        """Tutma yerinin pencere içi merkezi: son düğmeden sonra, eksen boyunca."""
        if self.edge:
            cx = DW - (DROP_D + 12) * 0.52
            return self.drop_transform().map(QPointF(cx + 6, DL / 2 + BTN_GAP + 20))
        return self.shape_rect().center() + QPointF(PAD + 28, 0)

    def paint_grip(self, p, c, show):
        """Üç ince kısa çizgi (duraklat ikonuyla karışmasın); balonda dikey, damlada kenara dik."""
        if show <= 0:
            return
        along_x = not self.edge or self.edge in ("top", "bottom")   # şeklin uzandığı yön
        p.save(); p.setOpacity(show)
        p.setPen(QPen(TXT if self.hover == "grip" else QColor(255, 255, 255, 110), 1.2, Qt.SolidLine, Qt.RoundCap))
        for o in (-3.5, 0, 3.5):
            if along_x:
                p.drawLine(QPointF(c.x() + o, c.y() - 5), QPointF(c.x() + o, c.y() + 5))
            else:
                p.drawLine(QPointF(c.x() - 5, c.y() + o), QPointF(c.x() + 5, c.y() + o))
        p.restore()

    def bubble_buttons(self):
        c = self.shape_rect().center()
        return [(c - QPointF(PAD - 2, 0), "dprev", "prev"), (c, "dplay", "play"), (c + QPointF(PAD - 2, 0), "dnext", "next")]

    def drop_buttons(self, tf=None, cx=None, py=DL / 2 - 12):
        """Uzamış damladaki düğmeler: [(pencere içi merkez, ad, ikon)]."""
        tf = tf or self.drop_transform()
        cx = cx if cx is not None else DW - (DROP_D + 12) * 0.52
        return [(tf.map(QPointF(cx, py - BTN_GAP)), "dprev", "prev"), (tf.map(QPointF(cx, py)), "dplay", "play"),
                (tf.map(QPointF(cx, py + BTN_GAP)), "dnext", "next")]

    def start_morph(self, from_circle):
        """Serbest balon kenara bırakıldı: balon kenara akıp damlaya karışır."""
        drop_pos = self.bubble_pos
        dw, dh = self.collapsed_size()
        area = QRectF(QPointF(drop_pos), QSizeF(dw, dh)).united(from_circle.adjusted(-M, -M, M, M)).toAlignedRect()
        self.morph = (from_circle.translated(-QPointF(area.topLeft())), QPointF(drop_pos - area.topLeft()))
        self.setFixedSize(area.size()); self.move(area.topLeft())
        self.refresh_backdrop()
        self.morph_anim.stop(); self.morph_anim.start()

    def morph_done(self):
        self.morph = None
        self.layout(False)
        self.settle_hover()
        self.refresh_backdrop()

    def paint_morph(self, p):
        circle, off = self.morph
        t = self.morph_t
        e = QEasingCurve(QEasingCurve.InOutCubic).valueForProgress(t)
        k = QEasingCurve(QEasingCurve.OutBack).valueForProgress(max(0.0, (t - 0.25) / 0.75))  # damla biraz geç kabarır, hafif yaylanır
        p.save(); p.translate(off)
        c, rad = self.paint_drop(p, k, art=False)
        p.restore()
        c = c + off
        # balon kenara doğru akarken küçülür, cam gövdesi damlaya karışarak söner
        cc = circle.center() + (c - circle.center()) * e
        br = circle.width() / 2 + (rad + 2.5 - circle.width() / 2) * e
        ball = QPainterPath(); ball.addEllipse(cc, br, br)
        p.save(); p.setOpacity(1 - e); self.glass(p, ball, QColor(18, 18, 18, 140)); p.restore()
        ar = (circle.width() / 2 - 5) + (rad - (circle.width() / 2 - 5)) * e
        self.draw_art(p, QRectF(cc.x() - ar, cc.y() - ar, ar * 2, ar * 2), ar)
        self.draw_ring(p, QRectF(cc.x() - ar - 2.5, cc.y() - ar - 2.5, ar * 2 + 5, ar * 2 + 5), 2.5 - 0.5 * e)

    def draw_ring(self, p, ring, width):
        i = self.info
        p.setBrush(Qt.NoBrush); p.setPen(QPen(QColor(255, 255, 255, 40), width)); p.drawEllipse(ring)
        if i and i["end"]:
            p.setPen(QPen(self.accent, width, Qt.SolidLine, Qt.RoundCap))
            p.drawArc(ring, 90 * 16, -int(360 * 16 * self.position() / i["end"]))

    def enterEvent(self, e):
        self.grow_to(1.0); super().enterEvent(e)

    def on_slide(self, v):
        self.slide = v
        if self.dragging and self.press:
            self.drag_place()

    def drag_place(self):
        _, origin, _ = self.press
        self.move(origin + self.drag_delta + self.drag_off * self.slide)

    def settle_hover(self):
        """Bırakınca imleç hâlâ üstündeyse düğmeler yeniden açılsın."""
        if not self.expanded and self.geometry().contains(QCursor.pos()):
            self.grow_to(1.0)

    def grow_to(self, v):
        a = self.grow_anim
        a.stop(); a.setStartValue(self.grow); a.setEndValue(v); a.start()

    # durum
    def on_info(self, info):
        if info.get("art") is not (self.info or {}).get("art"):
            img = QImage.fromData(info["art"]) if info.get("art") else QImage()
            self.art = None if img.isNull() else QPixmap.fromImage(img)
        self.info = info
        self.update()

    def position(self):
        i = self.info
        if not i or not i["end"]:
            return 0.0
        pos = i["pos"] + (time.time() - i["stamp"] if i["playing"] else 0)
        return min(pos, i["end"])

    def clamp(self, p, w, h, mx=M):
        scr = (self.screen() or QApplication.primaryScreen()).availableGeometry()
        return QPoint(max(scr.left() - mx, min(p.x(), scr.right() - w + mx)), max(scr.top() - M, min(p.y(), scr.bottom() - h + M)))

    def set_mode(self, expanded):
        if not expanded and self.expanded:
            self.animate(0.0, lambda: self.layout(False))  # önce kart balona küçülsün
            return
        self.layout(expanded)
        if expanded:
            self.t = 0.0
            self.animate(1.0)

    def animate(self, end, done=None):
        a = self.anim
        a.stop()
        self.done = done
        a.setStartValue(self.t); a.setEndValue(end)
        a.setDuration(320 if end else 220)
        curve = QEasingCurve(QEasingCurve.OutBack if end else QEasingCurve.InCubic)
        curve.setOvershoot(0.6)  # taşma gölge payı (M) içinde kalsın
        a.setEasingCurve(curve)
        a.start()

    def layout(self, expanded):
        self.expanded = expanded
        w, h = (W + 2 * M, H + 2 * M) if expanded else self.collapsed_size()
        if expanded:
            # kartı balonun bulunduğu köşeye doğru aç
            scr = (self.screen() or QApplication.primaryScreen()).availableGeometry()
            bx, by = self.bubble_pos.x(), self.bubble_pos.y()
            cw, ch = self.collapsed_size()
            x = bx if bx + cw / 2 < scr.center().x() else bx + cw - w
            y = by if by + ch / 2 < scr.center().y() else by + ch - h
            p = self.clamp(QPoint(x, y), w, h)
        elif self.edge:
            w, h = self.collapsed_size()
            p = self.bubble_pos
        else:
            p = self.clamp(self.bubble_pos, w, h, M + PAD)
        self.setFixedSize(w, h)
        self.move(p)
        self.hover = None
        if self.isVisible():
            if expanded:
                self.activateWindow()
        self.update()

    # yerleşim (açık kart)
    def rects(self):
        x0, y0 = M, M
        r = dict(
            art=QRectF(x0 + 14, y0 + 14, 64, 64),
            min=QRectF(x0 + W - 34, y0 + 10, 24, 24),
            bar=QRectF(x0 + 14, y0 + 92, W - 28, 14),
            prev=QRectF(x0 + 14, y0 + 116, 30, 30),
            play=QRectF(x0 + 50, y0 + 113, 36, 36),
            next=QRectF(x0 + 92, y0 + 116, 30, 30),
            vol=QRectF(x0 + W - 144, y0 + 116, 30, 30),
            slider=QRectF(x0 + W - 110, y0 + 122, 96, 18),
        )
        return r

    def hit(self, pos):
        if not self.expanded:
            if self.grow > 0.6:
                for c, name, _ in (self.drop_buttons() if self.edge else self.bubble_buttons()):
                    if (QPointF(pos) - c).manhattanLength() < 17:
                        return name
                if (QPointF(pos) - self.grip_point()).manhattanLength() < 20:
                    return "grip"
            return "bubble"
        for k, r in self.rects().items():
            if k != "art" and r.contains(QPointF(pos)):
                return k
        return None

    # çizim
    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        (self.paint_morph if self.morph else self.paint_card if self.expanded else self.paint_drop if self.edge else self.paint_bubble)(p)

    def shadow(self, p, rect, radius):
        p.setPen(Qt.NoPen)
        for i in range(M, 0, -2):
            p.setBrush(QColor(0, 0, 0, int(28 * (1 - i / M) ** 2)))
            p.drawRoundedRect(rect.adjusted(-i, -i + 3, i, i + 3), radius + i, radius + i)

    def draw_art(self, p, rect, radius):
        path = QPainterPath(); path.addRoundedRect(rect, radius, radius)
        p.save(); p.setClipPath(path, Qt.IntersectClip)
        if self.art:
            pm = self.art
            s = min(pm.width(), pm.height())
            src = QRectF((pm.width() - s) / 2, (pm.height() - s) / 2, s, s)
            p.drawPixmap(rect, pm, src)
        else:
            p.fillRect(rect, QColor("#282828"))
            self.glyph(p, rect, G["note"], rect.width() * 0.36, DIM)
        p.restore()

    def icon(self, p, c, kind, color):
        """Dolgulu oynatma ikonları (font ikonları ince çizgili kalıyor)."""
        x, y = c.x(), c.y()
        p.save(); p.setPen(Qt.NoPen); p.setBrush(color)
        tri = QPainterPath()
        if kind == "play":
            tri.moveTo(x - 4, y - 7); tri.lineTo(x + 7, y); tri.lineTo(x - 4, y + 7)
        elif kind == "pause":
            p.drawRoundedRect(QRectF(x - 5.5, y - 6.5, 4, 13), 1, 1); p.drawRoundedRect(QRectF(x + 1.5, y - 6.5, 4, 13), 1, 1)
        elif kind == "prev":
            p.drawRoundedRect(QRectF(x - 6.5, y - 6, 2.2, 12), 1, 1)
            tri.moveTo(x + 6, y - 6); tri.lineTo(x - 3.5, y); tri.lineTo(x + 6, y + 6)
        elif kind == "next":
            p.drawRoundedRect(QRectF(x + 4.3, y - 6, 2.2, 12), 1, 1)
            tri.moveTo(x - 6, y - 6); tri.lineTo(x + 3.5, y); tri.lineTo(x - 6, y + 6)
        tri.closeSubpath(); p.drawPath(tri)
        p.restore()

    def glyph(self, p, rect, ch, size, color):
        f = QFont(ICON); f.setPixelSize(int(size))
        p.setFont(f); p.setPen(color)
        p.drawText(rect, Qt.AlignCenter, ch)

    def paint_bubble(self, p):
        r, g = self.shape_rect(), self.grow
        if self.pop < 1.0:
            c = r.center(); p.translate(c); p.scale(self.pop, self.pop); p.translate(-c)
        pill = r.adjusted(-PAD * g, 0, (PAD + GRIP) * g, 0)
        self.shadow(p, pill, BUBBLE / 2)
        shape = QPainterPath(); shape.addRoundedRect(pill, BUBBLE / 2, BUBBLE / 2)
        self.glass(p, shape, QColor(18, 18, 18, 150))
        show = max(0.0, (g - 0.35) / 0.65)
        self.paint_buttons(p, self.bubble_buttons(), show)
        self.paint_grip(p, self.grip_point(), show)
        if g > 0.5:
            return
        p.save(); p.setOpacity(1 - g * 2)   # kapak söner, düğmeler belirir
        inner = r.adjusted(5, 5, -5, -5)
        self.draw_art(p, inner, inner.width() / 2)
        i = self.info
        if i and not i["playing"]:
            p.setBrush(QColor(0, 0, 0, 120)); p.drawEllipse(inner)
            self.icon(p, inner.center(), "play", TXT)
        # ilerleme halkası
        ring = r.adjusted(2, 2, -2, -2)
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(255, 255, 255, 30), 2.5)); p.drawEllipse(ring)
        if i and i["end"]:
            p.setPen(QPen(self.accent, 2.5, Qt.SolidLine, Qt.RoundCap))
            p.drawArc(ring, 90 * 16, -int(360 * 16 * self.position() / i["end"]))
        if time.time() < self.vol_flash:
            p.setPen(Qt.NoPen); p.setBrush(QColor(0, 0, 0, 170)); p.drawEllipse(inner)
            f = QFont("Segoe UI Variable Display"); f.setPixelSize(15); f.setBold(True)
            p.setFont(f); p.setPen(TXT)
            p.drawText(inner, Qt.AlignCenter, "×" if self.vol.muted() else f"{round(self.vol.get() * 100)}")
        p.restore()

    def paint_card(self, p):
        full, t = QRectF(M, M, W, H), self.t
        b = self.shape_rect().translated(QPointF(self.bubble_pos - self.pos()))
        # açılma animasyonu: balon -> kart (OutBack hafif taşar, kart "yaylanır")
        card = QRectF(b.x() + (full.x() - b.x()) * t, b.y() + (full.y() - b.y()) * t,
                      b.width() + (full.width() - b.width()) * t, b.height() + (full.height() - b.height()) * t)
        rad = min(BUBBLE / 2 + (20 - BUBBLE / 2) * t, card.height() / 2)
        self.shadow(p, card, rad)
        shape = QPainterPath(); shape.addRoundedRect(card.adjusted(0.5, 0.5, -0.5, -0.5), rad, rad)
        self.glass(p, shape, QColor(18, 18, 18, 175))   # okunurluk için kart balondan daha koyu
        if t < 0.5:  # balondaki kapak sönerek kaybolur
            s_ = min(b.width(), b.height()) - 10
            p.save(); p.setOpacity(1 - t * 2); inner = QRectF(b.center().x() - s_ / 2, b.center().y() - s_ / 2, s_, s_)
            self.draw_art(p, inner, s_ / 2); p.restore()
        if t < 0.45:
            return
        clip = QPainterPath(); clip.addRoundedRect(card, rad, rad); p.setClipPath(clip)
        p.setOpacity(min(1.0, (t - 0.45) / 0.5))
        r, i = self.rects(), self.info
        self.draw_art(p, r["art"], 12)

        tx = r["art"].right() + 14
        tw = r["min"].left() - tx - 6
        f = QFont("Segoe UI Variable Display")
        f.setPixelSize(11); f.setWeight(QFont.DemiBold); f.setLetterSpacing(QFont.AbsoluteSpacing, 0.6)
        p.setFont(f); p.setPen(self.accent if i else DIM)
        p.drawText(QRectF(tx, M + 16, tw, 14), Qt.AlignLeft | Qt.AlignVCenter, (i.get("app") or "Medya").upper() if i else "SESSİZ")
        f.setLetterSpacing(QFont.AbsoluteSpacing, 0); f.setPixelSize(16); f.setWeight(QFont.Bold)
        p.setFont(f); p.setPen(TXT)
        title = i.get("title") or "Bilinmeyen" if i else "Şu an bir şey çalmıyor"
        p.drawText(QRectF(tx, M + 33, tw, 22), Qt.AlignLeft | Qt.AlignVCenter, QFontMetrics(f).elidedText(title, Qt.ElideRight, int(tw)))
        f.setPixelSize(13); f.setWeight(QFont.Normal)
        p.setFont(f); p.setPen(DIM)
        artist = (i.get("artist") or "") if i else "Spotify, YouTube ya da bir video aç"
        p.drawText(QRectF(tx, M + 55, tw + 30, 18), Qt.AlignLeft | Qt.AlignVCenter, QFontMetrics(f).elidedText(artist, Qt.ElideRight, int(tw + 30)))

        self.glyph(p, r["min"], G["min"], 12, TXT if self.hover == "min" else DIM)

        # ilerleme
        bar = r["bar"]
        track = QRectF(bar.left(), bar.top() + 1, bar.width(), 4)
        p.setPen(Qt.NoPen); p.setBrush(QColor(255, 255, 255, 28)); p.drawRoundedRect(track, 2, 2)
        if i and i["end"]:
            frac = self.position() / i["end"]
            # Spotify: çubuk beyaz, üzerine gelince yeşil + beyaz tutamaç
            p.setBrush(self.accent if self.hover == "bar" else TXT); p.drawRoundedRect(QRectF(track.left(), track.top(), track.width() * frac, 4), 2, 2)
            if self.hover == "bar":
                p.setBrush(TXT); p.drawEllipse(QPointF(track.left() + track.width() * frac, track.center().y()), 5, 5)
            f.setPixelSize(10); p.setFont(f); p.setPen(DIM)
            p.drawText(QRectF(bar.left(), bar.top() + 6, 60, 12), Qt.AlignLeft | Qt.AlignVCenter, fmt(self.position()))
            p.drawText(QRectF(bar.right() - 60, bar.top() + 6, 60, 12), Qt.AlignRight | Qt.AlignVCenter, fmt(i["end"]))

        # düğmeler
        for k in ("prev", "next"):
            if self.hover == k:
                p.setPen(Qt.NoPen); p.setBrush(QColor(255, 255, 255, int(18 * self.hl))); p.drawEllipse(r[k])
            self.icon(p, r[k].center(), k, TXT if (i and self.hover == k) else DIM if not i else QColor("#D9D9D9"))
        p.setPen(Qt.NoPen)
        play = r["play"]
        if self.hover == "play":  # Spotify gibi üzerine gelince hafif büyür
            play = play.adjusted(*(1.5 * self.hl * d for d in (-1, -1, 1, 1)))
        p.setBrush(QColor("#3BE477") if self.hover == "play" else self.accent)
        p.drawEllipse(play)
        self.icon(p, play.center(), "pause" if (i and i["playing"]) else "play", QColor("#000000"))

        muted, v = self.vol.muted(), self.vol.get()
        if self.hover == "vol":
            p.setPen(Qt.NoPen); p.setBrush(QColor(255, 255, 255, int(18 * self.hl))); p.drawEllipse(r["vol"])
        self.glyph(p, r["vol"], G["mute" if muted else "vol"], 14, DIM if muted else TXT)
        sl = r["slider"]
        tr = QRectF(sl.left(), sl.center().y() - 2, sl.width(), 4)
        p.setPen(Qt.NoPen); p.setBrush(QColor(255, 255, 255, 28)); p.drawRoundedRect(tr, 2, 2)
        active = self.hover == "slider" or self.grab_slider
        p.setBrush(DIM if muted else self.accent if active else TXT); p.drawRoundedRect(QRectF(tr.left(), tr.top(), tr.width() * v, 4), 2, 2)
        if active:
            p.setBrush(TXT)
            p.drawEllipse(QPointF(tr.left() + tr.width() * v, tr.center().y()), 6, 6)

    # etkileşim
    def mousePressEvent(self, e):
        if e.button() != Qt.LeftButton or self.morph:
            return
        k = self.hit(e.position().toPoint())
        if k == "slider":
            self.grab_slider = True
            self.set_volume_at(e.position().x())
            return
        self.press = (e.globalPosition().toPoint(), self.pos(), k)
        self.dragging = False

    def mouseMoveEvent(self, e):
        if self.grab_slider:
            self.set_volume_at(e.position().x()); return
        if self.press:
            start, origin, k = self.press
            delta = e.globalPosition().toPoint() - start
            if self.dragging or (delta.manhattanLength() > 4 and k in ("bubble", "grip", None)):
                if not self.dragging and not self.expanded:
                    # sürüklerken hep balon: hap düğmeleri söndürerek küçülür, balon imlecin altına kayar
                    from_edge = bool(self.edge)
                    self.edge = None
                    self.setFixedSize(*self.collapsed_size())
                    target = start - self.shape_rect().center().toPoint()
                    self.drag_off = QPoint() if from_edge else origin - target
                    if from_edge:   # damla kenardan kopar, balon yaylanarak belirir
                        self.grow_anim.stop(); self.grow = 0.0
                        self.pop_anim.stop(); self.pop_anim.setStartValue(0.35); self.pop_anim.setEndValue(1.0); self.pop_anim.start()
                    else:
                        self.grow_to(0.0)
                    self.press = (start, target, k)
                    self.slide = 1.0; self.slide_anim.stop(); self.slide_anim.start()
                self.dragging = True
                self.drag_delta = delta
                if self.expanded:
                    self.move(origin + delta)
                else:
                    self.drag_place()
                if time.time() - self.last_grab > 0.08:
                    self.refresh_backdrop()
            return
        k = self.hit(e.position().toPoint())
        if k != self.hover:
            self.hover = k
            self.hl = 0.0; self.hl_anim.stop(); self.hl_anim.start()
            self.setCursor(Qt.SizeAllCursor if k == "grip" else Qt.PointingHandCursor if k and k != "bubble" else Qt.ArrowCursor)
            self.update()

    def mouseReleaseEvent(self, e):
        if self.grab_slider:
            self.grab_slider = None; self.update(); return
        if not self.press:
            return
        _, _, k = self.press
        self.press = None
        if self.dragging:
            self.dragging = False
            if self.expanded:
                # açıkken sürüklendi: kart yerinde kalsın, kapanınca yakınındaki serbest balona dönsün
                self.move(self.clamp(self.pos(), self.width(), self.height()))
                self.edge = None
                self.bubble_pos = self.clamp(self.pos() - QPoint(PAD, 0), *self.collapsed_size(), M + PAD)
                self.settings.setValue("pos", self.bubble_pos); self.settings.setValue("edge", "")
            else:
                circle = self.shape_rect().translated(QPointF(self.pos()))
                self.dock(self.geometry().center())
                self.grow = 0.0
                self.slide_anim.stop(); self.slide = 0.0
                if self.edge:
                    self.start_morph(circle)
                else:
                    self.layout(False)
                    self.settle_hover()
            self.refresh_backdrop()
            return
        if k != self.hit(e.position().toPoint()):
            return
        m = self.media
        if k == "bubble":
            self.set_mode(True)
        elif k == "min":
            self.set_mode(False)
        elif k in ("play", "dplay"):
            m.do("try_toggle_play_pause_async")
        elif k in ("prev", "dprev"):
            m.do("try_skip_previous_async")
        elif k in ("next", "dnext"):
            m.do("try_skip_next_async")
        elif k == "vol":
            self.vol.toggle_mute()
        elif k == "bar" and self.info and self.info["end"]:
            bar = self.rects()["bar"]
            frac = max(0, min(1, (e.position().x() - bar.left()) / bar.width()))
            m.do("try_change_playback_position_async", int(frac * self.info["end"] * 10_000_000))
            self.info["pos"], self.info["stamp"] = frac * self.info["end"], time.time()
        self.update()

    def set_volume_at(self, x):
        sl = self.rects()["slider"]
        self.vol.set((x - sl.left()) / sl.width())
        self.update()

    def wheelEvent(self, e):
        step = 0.02 if e.angleDelta().y() > 0 else -0.02
        self.vol.set(self.vol.get() + step)
        self.vol_flash = time.time() + 1.2
        self.update()

    def contextMenuEvent(self, e):
        menu = QMenu(self)
        menu.addAction("Kapat", QApplication.quit)
        menu.exec(e.globalPos())

    def leaveEvent(self, _):
        self.grow_to(0.0)
        self.hover = None; self.setCursor(Qt.ArrowCursor); self.update()

    def changeEvent(self, e):
        # başka yere tıklanınca kapan
        if e.type() == e.Type.ActivationChange and self.expanded and not self.isActiveWindow():
            QTimer.singleShot(120, lambda: self.expanded and not self.isActiveWindow() and self.set_mode(False))
        super().changeEvent(e)

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape and self.expanded:
            self.set_mode(False)
        elif e.key() == Qt.Key_Space:
            self.media.do("try_toggle_play_pause_async")


if __name__ == "__main__":
    ctypes.windll.kernel32.CreateMutexW(None, False, "medya-panel-tek")
    if ctypes.windll.kernel32.GetLastError() == 183:  # zaten açık: ikinci panel olmasın
        sys.exit(0)
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(True)
    panel = Panel()
    panel.show()
    sys.exit(app.exec())
