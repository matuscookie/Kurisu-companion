"""
Kurisu Desktop Companion
------------------------
Kurisu floats on your desktop. The Future Gadget Lab pin on her shoulder opens
a small tool menu - pick "Storage Manager" for a compact file-search popup.

Kurisu
  * Click her ......... change pose      * Drag ......... move her
  * Mouse wheel ....... resize           * Right-click .. more options
  * Click the PIN ..... tool menu

Storage Manager (compact popup)
  * Type to search file/folder names (all words must match, any case)
  * Down arrow ... browse results        * Enter ........ open
  * Right-click a result: open / show in folder / copy path
  * Esc: clear search, then close        * "..." menu: reindex / choose folders

Setup:   pip install PySide6   then   python kurisu_companion.py
Files:   kurisu_companion.py, storage_index.py, sprites/ (crs_*.png),
         assets/pin.png (the Future Gadget Lab pin)
"""

import os
import random
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import (
    QEvent, QFileInfo, QObject, QPoint, QRunnable, QSettings, QSize,
    QStandardPaths, QThread, QThreadPool, QTimer, QUrl, Qt, Signal,
)
from PySide6.QtGui import (
    QAction, QActionGroup, QColor, QDesktopServices, QGuiApplication, QIcon,
    QPainter, QPen, QPixmap,
)
from PySide6.QtWidgets import (
    QApplication, QComboBox, QFileDialog, QFileIconProvider, QFrame,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMenu,
    QPushButton, QSizePolicy, QSystemTrayIcon, QVBoxLayout, QWidget,
)

import storage_index as si

BASE_DIR = Path(__file__).resolve().parent
SPRITE_DIR = BASE_DIR / "sprites"
PIN_PATH = BASE_DIR / "assets" / "pin.png"

# filename -> display name (shown in the right-click menu)
POSES = [
    ("crs_ala01a.png", "Neutral"),
    ("crs_ala05a.png", "Half-lidded"),
    ("crs_ala09a.png", "Blushing pout"),
    ("crs_alb03a.png", "Arms crossed"),
    ("crs_alc06a.png", "Hand on tie"),
    ("crs_alc03a.png", "Eyes closed"),
    ("crs_ala02a.png", "Wink"),
]

# Where the pin sits on her shoulder, as (x, y) fractions of each sprite.
PIN_ANCHORS = {
    "crs_ala01a.png": (0.84, 0.665),
    "crs_ala05a.png": (0.84, 0.665),
    "crs_ala09a.png": (0.84, 0.665),
    "crs_ala02a.png": (0.84, 0.665),
    "crs_alb03a.png": (0.84, 0.655),
    "crs_alc06a.png": (0.78, 0.665),
    "crs_alc03a.png": (0.78, 0.665),
}
DEFAULT_ANCHOR = (0.82, 0.665)
PIN_SIZE_FRACTION = 0.105   # pin diameter relative to sprite height

DEFAULT_HEIGHT = 420        # sprite height in pixels
MIN_HEIGHT, MAX_HEIGHT = 150, 1000
DRAG_THRESHOLD = 4          # pixels of movement before a click becomes a drag

# Storage Manager geometry (compact on purpose)
MANAGER_W = 360
LIST_H = 162                # about six rows
RESULT_LIMIT = 300
REINDEX_AFTER_SECS = 24 * 3600


def make_fallback_pixmap() -> QPixmap:
    """Orange dot, used if assets/pin.png is missing."""
    pm = QPixmap(128, 128)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor("#ff8a1f"))
    p.setPen(QColor("#5a2a00"))
    p.drawEllipse(8, 8, 112, 112)
    p.end()
    return pm


def human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024


MENU_STYLE = """
QMenu { background:#14161b; color:#f2e6d0; border:1px solid #4a3416; padding:4px; }
QMenu::item { padding:7px 26px 7px 14px; }
QMenu::item:selected { background:#ff8a1f; color:#000; }
QMenu::separator { height:1px; background:#3a3226; margin:4px 8px; }
"""


def styled_menu(parent=None) -> QMenu:
    menu = QMenu(parent)
    menu.setStyleSheet(MENU_STYLE)
    return menu


# =============================================================================
#  Background workers
# =============================================================================
class IndexThread(QThread):
    progress = Signal(int, str)
    done = Signal(int, bool)        # total items, completed (False = stopped)
    failed = Signal(str)

    def __init__(self, roots, db_path):
        super().__init__()
        self.roots, self.db_path = roots, db_path
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        try:
            total, completed = si.build_index(
                self.roots, self.db_path,
                should_stop=lambda: self._stop,
                on_progress=lambda n, d: self.progress.emit(n, d),
            )
            self.done.emit(total, completed)
        except Exception as ex:                      # noqa: BLE001
            self.failed.emit(str(ex))


class SearchSignals(QObject):
    results = Signal(int, list)     # sequence id, rows
    error = Signal(int, str)


class SearchTask(QRunnable):
    def __init__(self, seq, db_path, text, kind, signals):
        super().__init__()
        self.seq, self.db_path = seq, db_path
        self.text, self.kind, self.signals = text, kind, signals

    def run(self):
        try:
            rows = si.search(self.db_path, self.text, self.kind, RESULT_LIMIT)
            self.signals.results.emit(self.seq, rows)
        except Exception as ex:                      # noqa: BLE001
            self.signals.error.emit(self.seq, str(ex))


# =============================================================================
#  Storage Manager - compact popup
# =============================================================================
STYLE = """
#card { background:#14161b; border:1px solid #4a3416; border-radius:10px; }
QLineEdit, QComboBox {
    background:#0c0d10; color:#f2e6d0; border:1px solid #3a3226;
    border-radius:7px; padding:2px 8px; font-size:13px;
    selection-background-color:#ff8a1f; selection-color:#000;
}
QLineEdit:focus { border:1px solid #ff8a1f; }
QComboBox QAbstractItemView {
    background:#0c0d10; color:#f2e6d0;
    selection-background-color:#ff8a1f; selection-color:#000;
}
QPushButton {
    background:#1d2027; color:#f2e6d0; border:1px solid #3a3226;
    border-radius:6px; padding:0px;
}
QPushButton:hover { border-color:#ff8a1f; color:#ffb35c; }
QListWidget {
    background:#0c0d10; color:#e8dcc6; border:1px solid #2a2a2a;
    border-radius:7px; outline:0; font-size:13px;
}
QListWidget::item { padding:4px 6px; }
QListWidget::item:selected { background:#ff8a1f; color:#000; }
#status { color:#9a8f7c; font-size:11px; }
"""


class StorageManager(QWidget):
    def __init__(self):
        super().__init__()
        self.settings = QSettings("KurisuCompanion", "Kurisu")

        data_dir = Path(
            QStandardPaths.writableLocation(QStandardPaths.AppDataLocation)
        )
        data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = data_dir / "index.db"

        self.seq = 0
        self.n_results = 0
        self.indexer = None
        self.anchor_rect = None        # Kurisu's window rect (global)
        self.anchor_y = 0              # pin's y position (global)
        self.pool = QThreadPool.globalInstance()
        self.signals = SearchSignals()
        self.signals.results.connect(self._on_results)
        self.signals.error.connect(
            lambda _s, msg: self._set_status(f"Search error: {msg}")
        )
        self.icons = QFileIconProvider()
        self.icon_cache = {}

        self.setWindowTitle("Storage Manager")
        self.setAttribute(Qt.WA_TranslucentBackground)
        # Popup = closes by itself when you click anywhere else
        self.setWindowFlags(Qt.Popup | Qt.FramelessWindowHint)
        self.setStyleSheet(STYLE)
        self._build_ui()

        self.debounce = QTimer(self)
        self.debounce.setSingleShot(True)
        self.debounce.setInterval(200)
        self.debounce.timeout.connect(self._run_search)
        self.edit.textChanged.connect(lambda _t: self.debounce.start())
        self.kind.currentIndexChanged.connect(lambda _i: self._run_search())
        self.edit.returnPressed.connect(self._open_current)
        self.edit.installEventFilter(self)
        self.list.installEventFilter(self)

        self._update_status()

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        card = QFrame()
        card.setObjectName("card")
        root.addWidget(card)
        lay = QVBoxLayout(card)
        lay.setContentsMargins(8, 8, 8, 6)
        lay.setSpacing(6)

        top = QHBoxLayout()
        top.setSpacing(6)
        self.edit = QLineEdit()
        self.edit.setFixedHeight(30)
        self.edit.setClearButtonEnabled(True)
        self.edit.setPlaceholderText("Search files and folders…")
        self.kind = QComboBox()
        self.kind.setFixedSize(88, 30)
        self.kind.addItems(["All", "Folders"] + list(si.KINDS))
        top.addWidget(self.edit, 1)
        top.addWidget(self.kind)
        lay.addLayout(top)

        self.list = QListWidget()
        self.list.setUniformItemSizes(True)
        self.list.setIconSize(QSize(16, 16))
        self.list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list.setFixedHeight(LIST_H)
        self.list.itemActivated.connect(
            lambda item: self._open(item.data(Qt.UserRole))
        )
        self.list.currentItemChanged.connect(self._on_current_changed)
        self.list.customContextMenuRequested.connect(self._result_menu)
        self.list.hide()
        lay.addWidget(self.list)

        foot = QHBoxLayout()
        foot.setSpacing(6)
        self.status = QLabel("")
        self.status.setObjectName("status")
        self.status.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        more = QPushButton("⋯")
        more.setFixedSize(26, 20)
        more.clicked.connect(lambda: self._more_menu(more))
        foot.addWidget(self.status, 1)
        foot.addWidget(more)
        lay.addLayout(foot)

        self.setFixedWidth(MANAGER_W)

    def _resize_to_content(self):
        self.layout().activate()
        self.setFixedSize(MANAGER_W, self.layout().sizeHint().height())
        self._place()

    def _show_results(self, visible: bool):
        if self.list.isVisible() != visible:
            self.list.setVisible(visible)
            self._resize_to_content()

    def _place(self):
        """Put the popup beside Kurisu, level with the pin, inside the screen."""
        if self.anchor_rect is None:
            return
        screen = (QGuiApplication.screenAt(self.anchor_rect.center())
                  or QGuiApplication.primaryScreen())
        avail = screen.availableGeometry()
        w, h = self.width(), self.height()
        x = self.anchor_rect.right() + 6
        if x + w > avail.right():
            x = self.anchor_rect.left() - w - 6
        x = max(avail.left() + 4, min(x, avail.right() - w - 4))
        y = self.anchor_y - 40
        y = max(avail.top() + 4, min(y, avail.bottom() - h - 4))
        self.move(x, y)

    # --------------------------------------------------------- show / hide
    def open_near(self, anchor_rect, anchor_y):
        self.anchor_rect, self.anchor_y = anchor_rect, anchor_y
        self._update_status()
        self._resize_to_content()
        self.show()
        self.raise_()
        self.edit.setFocus()
        self.edit.selectAll()
        if len(self.edit.text().strip()) >= 2:
            self._run_search()

    def eventFilter(self, obj, ev):
        if ev.type() == QEvent.KeyPress:
            key = ev.key()
            if obj is self.edit:
                if key == Qt.Key_Escape:
                    if self.edit.text():
                        self.edit.clear()
                        self.seq += 1
                        self._show_results(False)
                        self._update_status()
                    else:
                        self.hide()
                    return True
                if key == Qt.Key_Down and self.list.count():
                    self.list.setFocus()
                    if not self.list.currentItem():
                        self.list.setCurrentRow(0)
                    return True
            elif obj is self.list:
                if key == Qt.Key_Escape:
                    self.edit.setFocus()
                    return True
                if key == Qt.Key_Up and self.list.currentRow() == 0:
                    self.edit.setFocus()
                    return True
        return super().eventFilter(obj, ev)

    # -------------------------------------------------------------- status
    def _set_status(self, text, tip=""):
        shown = self.status.fontMetrics().elidedText(
            text, Qt.ElideMiddle, MANAGER_W - 70
        )
        self.status.setText(shown)
        self.status.setToolTip(tip or (text if shown != text else ""))

    def _on_current_changed(self, cur, _prev):
        if cur is not None and self.list.isVisible():
            more = "+" if self.n_results >= RESULT_LIMIT else ""
            path = cur.data(Qt.UserRole)
            self._set_status(f"{self.n_results}{more} · {path}", path)

    # -------------------------------------------------------------- search
    def _run_search(self):
        text = self.edit.text().strip()
        if len(text) < 2:
            self.seq += 1               # discard any in-flight result
            self._show_results(False)
            self._update_status()
            return
        if not self.db_path.exists():
            self._set_status("Index not ready yet…")
            return
        self.seq += 1
        self.pool.start(
            SearchTask(
                self.seq, str(self.db_path), text,
                self.kind.currentText(), self.signals,
            )
        )

    def _on_results(self, seq, rows):
        if seq != self.seq:
            return                      # stale
        self.n_results = len(rows)
        if not rows:
            self._show_results(False)
            self._set_status("No matches")
            return
        self.list.setUpdatesEnabled(False)
        self.list.clear()
        for name, path, is_dir, size, mtime in rows:
            item = QListWidgetItem(self._icon_for(name, path, bool(is_dir)), name)
            item.setData(Qt.UserRole, path)
            bits = [path]
            if not is_dir:
                bits.append(human_size(size))
            if mtime:
                bits.append(datetime.fromtimestamp(mtime).strftime("%Y-%m-%d"))
            item.setToolTip("\n".join(bits))
            self.list.addItem(item)
        self.list.setUpdatesEnabled(True)
        self._show_results(True)
        self.list.setCurrentRow(0)
        self._on_current_changed(self.list.currentItem(), None)

    def _icon_for(self, name, path, is_dir):
        if is_dir:
            key = "<dir>"
        else:
            ext = os.path.splitext(name)[1].lower()
            # these types have per-file icons; everything else is shared
            key = path if ext in (".exe", ".lnk", ".ico", ".url", ".msi") else ext
        if key not in self.icon_cache:
            if is_dir:
                self.icon_cache[key] = self.icons.icon(
                    QFileIconProvider.IconType.Folder
                )
            else:
                self.icon_cache[key] = self.icons.icon(QFileInfo(path))
        return self.icon_cache[key]

    # ------------------------------------------------------ result actions
    def _open_current(self):
        item = self.list.currentItem()
        if item is None and self.list.count():
            item = self.list.item(0)
        if item is not None:
            self._open(item.data(Qt.UserRole))

    def _open(self, path):
        if not os.path.exists(path):
            self._set_status("That item no longer exists - try Reindex")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))
        self.hide()

    def _reveal(self, path):
        if not os.path.exists(path):
            self._set_status("That item no longer exists - try Reindex")
            return
        if sys.platform.startswith("win"):
            subprocess.Popen(["explorer", f"/select,{os.path.normpath(path)}"])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", path])
        else:
            target = path if os.path.isdir(path) else os.path.dirname(path)
            subprocess.Popen(["xdg-open", target])
        self.hide()

    def _result_menu(self, pos):
        item = self.list.itemAt(pos)
        if item is None:
            return
        path = item.data(Qt.UserRole)
        menu = styled_menu(self)
        menu.addAction("Open", lambda: self._open(path))
        menu.addAction("Show in folder", lambda: self._reveal(path))
        menu.addAction(
            "Copy path", lambda: QGuiApplication.clipboard().setText(path)
        )
        menu.exec(self.list.viewport().mapToGlobal(pos))

    def _more_menu(self, button):
        menu = styled_menu(self)
        self.populate_index_menu(menu)
        menu.exec(button.mapToGlobal(QPoint(0, button.height())))

    # ------------------------------------------------------------ indexing
    def roots(self):
        v = self.settings.value("roots")
        if v is None:
            custom = []
        elif isinstance(v, str):
            custom = [v]
        else:
            custom = list(v)
        return custom or si.default_roots()

    def is_indexing(self):
        return self.indexer is not None and self.indexer.isRunning()

    def maybe_auto_index(self):
        stamp = self.settings.value("index_time", 0, type=float)
        if not self.db_path.exists() or time.time() - stamp > REINDEX_AFTER_SECS:
            self.start_index()

    def start_index(self):
        if self.is_indexing():
            return
        self.indexer = IndexThread(self.roots(), str(self.db_path))
        self.indexer.progress.connect(self._on_progress)
        self.indexer.done.connect(self._on_index_done)
        self.indexer.failed.connect(self._on_index_failed)
        self.indexer.start()
        self._set_status("Indexing… 0")

    def toggle_index(self):
        if self.is_indexing():
            self.indexer.stop()
            self._set_status("Stopping…")
        else:
            self.start_index()

    def _on_progress(self, n, current):
        self._set_status(f"Indexing… {n:,} items", current)

    def _on_index_done(self, total, completed):
        if completed:
            self.settings.setValue("index_count", total)
            self.settings.setValue("index_time", time.time())
        self._update_status()
        if self.isVisible() and len(self.edit.text().strip()) >= 2:
            self._run_search()

    def _on_index_failed(self, msg):
        self._set_status(f"Index error: {msg}")

    def _update_status(self):
        if self.is_indexing():
            return                      # progress text is already showing
        count = self.settings.value("index_count", 0, type=int)
        stamp = self.settings.value("index_time", 0, type=float)
        if count and stamp:
            when = datetime.fromtimestamp(stamp).strftime("%b %d %H:%M")
            self._set_status(f"{count:,} items indexed", f"Last indexed {when}")
        else:
            self._set_status("Not indexed yet")

    def add_folder(self):
        d = QFileDialog.getExistingDirectory(None, "Choose a folder to index")
        if not d:
            return
        v = self.settings.value("roots")
        custom = [] if v is None else ([v] if isinstance(v, str) else list(v))
        d = os.path.normpath(d)
        if d not in custom:
            custom.append(d)
        self.settings.setValue("roots", custom)
        self.start_index()

    def reset_roots(self):
        self.settings.remove("roots")
        self.start_index()

    def populate_index_menu(self, menu: QMenu):
        menu.addAction(
            "Stop indexing" if self.is_indexing() else "Reindex now",
            self.toggle_index,
        )
        menu.addAction("Index only a chosen folder…", self.add_folder)
        menu.addAction("Index all drives (default)", self.reset_roots)

    def shutdown(self):
        if self.is_indexing():
            self.indexer.stop()
            self.indexer.wait(5000)


# =============================================================================
#  Kurisu sprite (with the Future Gadget Lab pin on her shoulder)
# =============================================================================
class KurisuWidget(QWidget):
    def __init__(self, icon: QIcon, pin_pixmap: QPixmap):
        super().__init__()
        self.settings = QSettings("KurisuCompanion", "Kurisu")
        self.manager = None          # StorageManager, set from main()
        self.pin_src = pin_pixmap

        self.setWindowTitle("Kurisu")
        self.setWindowIcon(icon)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setMouseTracking(True)
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
        )

        # --- load sprites -------------------------------------------------
        # Only the Kurisu sprites listed in POSES are loaded, in that order.
        search_dir = SPRITE_DIR if SPRITE_DIR.is_dir() else BASE_DIR
        print(f"[info] looking for sprites in: {search_dir}")
        self.originals: list[tuple[str, QPixmap]] = []
        self.anchors: list[tuple[float, float]] = []
        for filename, label in POSES:
            path = search_dir / filename
            if not path.exists():
                print(f"[warn] missing: {path}")
                continue
            pm = QPixmap(str(path))
            if pm.isNull():
                print(f"[warn] could not load {path}")
                continue
            self.originals.append((label, pm))
            self.anchors.append(PIN_ANCHORS.get(filename, DEFAULT_ANCHOR))
        if not self.originals:
            raise SystemExit(
                f"None of the Kurisu sprites were found in {search_dir}\n"
                "Put the crs_*.png files in a folder named 'sprites' next to "
                "this script."
            )

        # --- state --------------------------------------------------------
        self.index = 0
        self.random_mode = self.settings.value("random", False, type=bool)
        self.sprite_height = self.settings.value(
            "height", DEFAULT_HEIGHT, type=int
        )
        self.always_on_top = self.settings.value("on_top", True, type=bool)
        self._drag_offset = QPoint()
        self._press_global = QPoint()
        self._dragging = False
        self._press_on_pin = False
        self._pin_hover = False

        self._rescale()
        self._apply_on_top()

        pos = self.settings.value("pos_v2")
        if pos is not None:
            self.move(pos)
        else:
            screen = QApplication.primaryScreen().availableGeometry()
            self.move(
                screen.right() - self.width() - 40,
                screen.bottom() - self.height(),
            )

    # ---------------------------------------------------------------- sprites
    def _rescale(self):
        """Scale every sprite to the same height; window fits the widest one."""
        self.scaled: list[QPixmap] = []
        for _, pm in self.originals:
            self.scaled.append(
                pm.scaledToHeight(self.sprite_height, Qt.SmoothTransformation)
            )
        width = max(pm.width() for pm in self.scaled)
        self.setFixedSize(width, self.sprite_height)
        d = max(16, round(self.sprite_height * PIN_SIZE_FRACTION))
        self.pin_scaled = self.pin_src.scaled(
            d, d, Qt.KeepAspectRatio, Qt.SmoothTransformation
        )
        self.update()

    def set_pose(self, i: int):
        self.index = i % len(self.scaled)
        self.update()

    def next_pose(self):
        if self.random_mode and len(self.scaled) > 1:
            choices = [i for i in range(len(self.scaled)) if i != self.index]
            self.set_pose(random.choice(choices))
        else:
            self.set_pose(self.index + 1)

    # -------------------------------------------------------------------- pin
    def pin_geom(self):
        """(centre_x, centre_y, radius) of the pin in widget coordinates."""
        pm = self.scaled[self.index]
        fx, fy = self.anchors[self.index]
        x0 = (self.width() - pm.width()) // 2
        return (
            x0 + fx * pm.width(),
            fy * pm.height(),
            self.pin_scaled.width() / 2,
        )

    def _on_pin(self, pos) -> bool:
        cx, cy, r = self.pin_geom()
        return (pos.x() - cx) ** 2 + (pos.y() - cy) ** 2 <= (r + 3) ** 2

    def _set_hover(self, on: bool):
        if on != self._pin_hover:
            self._pin_hover = on
            if on:
                self.setCursor(Qt.PointingHandCursor)
            else:
                self.unsetCursor()
            self.update()

    def leaveEvent(self, _e):
        self._set_hover(False)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        p.setRenderHint(QPainter.Antialiasing)
        pm = self.scaled[self.index]
        x = (self.width() - pm.width()) // 2   # centre horizontally
        p.drawPixmap(x, 0, pm)

        # the pin: soft shadow, the badge, and an orange ring on hover
        cx, cy, r = self.pin_geom()
        d = self.pin_scaled.width()
        left, top = int(cx - r), int(cy - r)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 70))
        p.drawEllipse(left + 1, top + 2, d, d)
        p.drawPixmap(left, top, self.pin_scaled)
        if self._pin_hover:
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(QColor("#ff8a1f"), 2))
            p.drawEllipse(left - 2, top - 2, d + 4, d + 4)

    # ----------------------------------------------------------------- window
    def _apply_on_top(self):
        flags = self.windowFlags()
        if self.always_on_top:
            flags |= Qt.WindowStaysOnTopHint
        else:
            flags &= ~Qt.WindowStaysOnTopHint
        was_visible = self.isVisible()
        self.setWindowFlags(flags)
        if was_visible:
            self.show()

    # ------------------------------------------------------------------ mouse
    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._press_global = e.globalPosition().toPoint()
            self._drag_offset = self._press_global - self.frameGeometry().topLeft()
            self._dragging = False
            self._press_on_pin = self._on_pin(e.position())
            e.accept()

    def mouseMoveEvent(self, e):
        if e.buttons() & Qt.LeftButton:
            gp = e.globalPosition().toPoint()
            if (
                not self._dragging
                and (gp - self._press_global).manhattanLength() > DRAG_THRESHOLD
            ):
                self._dragging = True
            if self._dragging:
                self.move(gp - self._drag_offset)
        else:
            self._set_hover(self._on_pin(e.position()))
        e.accept()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton:
            if self._dragging:
                self.settings.setValue("pos_v2", self.pos())
            elif self._press_on_pin and self._on_pin(e.position()):
                self.open_launcher()      # clicked the pin
            else:
                self.next_pose()          # clicked Kurisu
            self._dragging = False
            self._press_on_pin = False
            e.accept()

    def wheelEvent(self, e):
        step = 20 if e.angleDelta().y() > 0 else -20
        new_h = max(MIN_HEIGHT, min(MAX_HEIGHT, self.sprite_height + step))
        if new_h != self.sprite_height:
            self.sprite_height = new_h
            self.settings.setValue("height", new_h)
            self._rescale()
        e.accept()

    # --------------------------------------------------------------- launcher
    def open_launcher(self):
        """The pin's menu. To add another tool, add one line to `tools`."""
        tools = [
            ("Storage Manager", self.open_manager_soon),
        ]
        menu = styled_menu(self)
        for label, action in tools:
            menu.addAction(label, action)
        menu.addSeparator()
        menu.addAction("Next pose", self.next_pose)
        menu.addAction("Quit", QApplication.quit)
        cx, cy, r = self.pin_geom()
        menu.exec(self.mapToGlobal(QPoint(int(cx + r + 4), int(cy - 10))))

    def open_manager_soon(self):
        # tiny delay so the menu has fully closed before the popup opens
        QTimer.singleShot(60, self.open_manager)

    def open_manager(self):
        if self.manager is None:
            return
        _, cy, _ = self.pin_geom()
        self.manager.open_near(
            self.frameGeometry(), self.mapToGlobal(QPoint(0, int(cy))).y()
        )

    # ------------------------------------------------------------------- menu
    def contextMenuEvent(self, e):
        menu = QMenu(self)

        pose_menu = menu.addMenu("Pose")
        group = QActionGroup(pose_menu)
        group.setExclusive(True)
        for i, (label, _) in enumerate(self.originals):
            act = QAction(label, pose_menu, checkable=True)
            act.setChecked(i == self.index)
            act.triggered.connect(lambda _=False, i=i: self.set_pose(i))
            group.addAction(act)
            pose_menu.addAction(act)

        if self.manager is not None:
            sub = menu.addMenu("Storage Manager")
            sub.addAction("Open", self.open_manager_soon)
            sub.addSeparator()
            self.manager.populate_index_menu(sub)

        rnd = QAction("Random poses on click", menu, checkable=True)
        rnd.setChecked(self.random_mode)
        rnd.toggled.connect(self._toggle_random)
        menu.addAction(rnd)

        top = QAction("Always on top", menu, checkable=True)
        top.setChecked(self.always_on_top)
        top.toggled.connect(self._toggle_on_top)
        menu.addAction(top)

        menu.addSeparator()
        menu.addAction("Reset size", self._reset_size)
        menu.addAction("Quit", QApplication.quit)
        menu.exec(e.globalPos())

    def _toggle_random(self, on: bool):
        self.random_mode = on
        self.settings.setValue("random", on)

    def _toggle_on_top(self, on: bool):
        self.always_on_top = on
        self.settings.setValue("on_top", on)
        self._apply_on_top()

    def _reset_size(self):
        self.sprite_height = DEFAULT_HEIGHT
        self.settings.setValue("height", DEFAULT_HEIGHT)
        self._rescale()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("KurisuCompanion")
    app.setQuitOnLastWindowClosed(False)   # keep running via the tray

    pin_pm = QPixmap(str(PIN_PATH)) if PIN_PATH.exists() else QPixmap()
    if pin_pm.isNull():
        pin_pm = make_fallback_pixmap()
    icon = QIcon(pin_pm)
    app.setWindowIcon(icon)

    kurisu = KurisuWidget(icon, pin_pm)
    manager = StorageManager()
    kurisu.manager = manager
    app.aboutToQuit.connect(manager.shutdown)

    kurisu.show()
    manager.maybe_auto_index()        # builds the file index in the background

    # --- system tray ------------------------------------------------------
    tray = QSystemTrayIcon(icon, app)
    tray.setToolTip("Kurisu Companion")
    tray_menu = QMenu()

    def toggle_kurisu():
        kurisu.setVisible(not kurisu.isVisible())

    tray_menu.addAction("Show / Hide Kurisu", toggle_kurisu)
    tray_menu.addAction("Next pose", kurisu.next_pose)
    tray_menu.addAction("Storage Manager", kurisu.open_manager_soon)
    tray_menu.addSeparator()
    tray_menu.addAction("Quit", app.quit)
    tray.setContextMenu(tray_menu)
    tray.activated.connect(
        lambda reason: toggle_kurisu()
        if reason == QSystemTrayIcon.Trigger else None
    )
    tray.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
