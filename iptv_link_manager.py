#!/usr/bin/env python3
"""
IPTV Link Yöneticisi
=====================
Kendi M3U linklerini / M3U dosyalarını / Xtream Codes hesaplarını ekleyip
içindeki kanalların ad, grup ve URL bilgilerini düzenleyebileceğin, yeni
link/kanal ekleyebileceğin ve tekrar .m3u olarak dışa aktarabileceğin basit
bir PyQt6 aracı.

Kurulum:
    pip install PyQt6 requests

Çalıştırma:
    python3 iptv_link_manager.py
"""

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field, asdict
from typing import List, Optional

try:
    import requests
except ImportError:
    requests = None

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QListWidget, QListWidgetItem, QPushButton, QLabel, QLineEdit,
    QTableWidget, QTableWidgetItem, QHeaderView, QDialog, QFormLayout,
    QComboBox, QDialogButtonBox, QMessageBox, QSplitter, QFileDialog,
    QStackedWidget, QAbstractItemView
)

CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".config", "iptv_link_manager.json")


# ───────────────────────── Veri modelleri ─────────────────────────

@dataclass
class Source:
    name: str
    kind: str  # "m3u_url" | "m3u_file" | "xtream"
    url: str = ""          # m3u_url / m3u_file için
    server: str = ""       # xtream
    username: str = ""     # xtream
    password: str = ""     # xtream
    channels: List[dict] = field(default_factory=list)  # {name, group, logo, url, tvg_id}

    def effective_m3u_url(self) -> Optional[str]:
        if self.kind == "xtream":
            base = self.server.rstrip("/")
            return (f"{base}/get.php?username={self.username}"
                    f"&password={self.password}&type=m3u_plus&output=ts")
        if self.kind == "m3u_url":
            return self.url
        return None


def load_sources() -> List[Source]:
    if not os.path.exists(CONFIG_PATH):
        return []
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return [Source(**s) for s in raw]
    except Exception:
        return []


def save_sources(sources: List[Source]):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump([asdict(s) for s in sources], f, ensure_ascii=False, indent=2)


# ───────────────────────── M3U parse / export ─────────────────────────

EXTINF_RE = re.compile(r'#EXTINF:-?\d+(?P<attrs>.*?),(?P<title>[^\n]*)')
ATTR_RE = re.compile(r'([a-zA-Z0-9\-]+)="([^"]*)"')


def parse_m3u(text: str) -> List[dict]:
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    channels = []
    pending = None
    for line in lines:
        if line.startswith("#EXTINF"):
            m = EXTINF_RE.match(line)
            if not m:
                pending = {"name": line.split(",")[-1], "group": "", "logo": "", "tvg_id": ""}
                continue
            attrs = dict(ATTR_RE.findall(m.group("attrs")))
            pending = {
                "name": m.group("title").strip() or attrs.get("tvg-name", "İsimsiz"),
                "group": attrs.get("group-title", ""),
                "logo": attrs.get("tvg-logo", ""),
                "tvg_id": attrs.get("tvg-id", ""),
            }
        elif line.startswith("#"):
            continue
        else:
            if pending is not None:
                pending["url"] = line
                channels.append(pending)
                pending = None
            # URL'siz başıboş satırları yoksay
    return channels


def channels_to_m3u(channels: List[dict]) -> str:
    out = ["#EXTM3U"]
    for ch in channels:
        out.append(
            f'#EXTINF:-1 tvg-id="{ch.get("tvg_id", "")}" '
            f'tvg-logo="{ch.get("logo", "")}" '
            f'group-title="{ch.get("group", "")}",{ch.get("name", "İsimsiz")}'
        )
        out.append(ch.get("url", ""))
    return "\n".join(out) + "\n"


def fetch_m3u_text(source: Source) -> str:
    if source.kind == "m3u_file":
        with open(source.url, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()
    url = source.effective_m3u_url()
    if not url:
        raise ValueError("Bu kaynak için geçerli bir URL yok.")
    if requests is None:
        raise RuntimeError("'requests' kurulu değil: pip install requests")
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    return r.text


# ───────────────────────── Oynatma ─────────────────────────

def play_url(url: str):
    for player in ("mpv", "vlc"):
        if shutil.which(player):
            subprocess.Popen([player, url],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
    # Sistemin varsayılan uygulamasıyla aç (Linux)
    if shutil.which("xdg-open"):
        subprocess.Popen(["xdg-open", url],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return
    raise RuntimeError("mpv, vlc veya xdg-open bulunamadı. Birini kurup tekrar dene.")


# ───────────────────────── Kaynak Ekle/Düzenle Diyaloğu ─────────────────────────

class SourceDialog(QDialog):
    def __init__(self, parent=None, source: Optional[Source] = None):
        super().__init__(parent)
        self.setWindowTitle("Kaynak Düzenle" if source else "Yeni Kaynak Ekle")
        self.setMinimumWidth(420)

        self.name_edit = QLineEdit(source.name if source else "")
        self.kind_combo = QComboBox()
        self.kind_combo.addItem("M3U Link (URL)", "m3u_url")
        self.kind_combo.addItem("M3U Dosyası (yerel)", "m3u_file")
        self.kind_combo.addItem("Xtream Codes (server/kullanıcı/şifre)", "xtream")

        self.url_edit = QLineEdit(source.url if source else "")
        self.file_edit = QLineEdit(source.url if (source and source.kind == "m3u_file") else "")
        browse_btn = QPushButton("Gözat...")
        browse_btn.clicked.connect(self._browse)

        self.server_edit = QLineEdit(source.server if source else "")
        self.user_edit = QLineEdit(source.username if source else "")
        self.pass_edit = QLineEdit(source.password if source else "")

        self.stack = QStackedWidget()

        w1 = QWidget(); l1 = QFormLayout(w1)
        l1.addRow("M3U URL:", self.url_edit)
        self.stack.addWidget(w1)

        w2 = QWidget(); l2 = QHBoxLayout(w2)
        l2.addWidget(self.file_edit); l2.addWidget(browse_btn)
        self.stack.addWidget(w2)

        w3 = QWidget(); l3 = QFormLayout(w3)
        l3.addRow("Sunucu (http://host:port):", self.server_edit)
        l3.addRow("Kullanıcı adı:", self.user_edit)
        l3.addRow("Şifre:", self.pass_edit)
        self.stack.addWidget(w3)

        self.kind_combo.currentIndexChanged.connect(self.stack.setCurrentIndex)

        form = QFormLayout()
        form.addRow("Ad:", self.name_edit)
        form.addRow("Tür:", self.kind_combo)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.stack)
        layout.addWidget(buttons)

        if source:
            idx = {"m3u_url": 0, "m3u_file": 1, "xtream": 2}.get(source.kind, 0)
            self.kind_combo.setCurrentIndex(idx)
            self.stack.setCurrentIndex(idx)

    def _browse(self):
        path, _ = QFileDialog.getOpenFileName(self, "M3U dosyası seç", "", "M3U (*.m3u *.m3u8);;Tüm dosyalar (*)")
        if path:
            self.file_edit.setText(path)

    def get_source(self) -> Optional[Source]:
        name = self.name_edit.text().strip()
        if not name:
            QMessageBox.warning(self, "Eksik bilgi", "Ad boş olamaz.")
            return None
        kind = self.kind_combo.currentData()
        if kind == "m3u_url":
            url = self.url_edit.text().strip()
            if not url:
                QMessageBox.warning(self, "Eksik bilgi", "M3U URL boş olamaz.")
                return None
            return Source(name=name, kind=kind, url=url)
        if kind == "m3u_file":
            path = self.file_edit.text().strip()
            if not path:
                QMessageBox.warning(self, "Eksik bilgi", "Dosya seçmelisin.")
                return None
            return Source(name=name, kind=kind, url=path)
        server = self.server_edit.text().strip()
        user = self.user_edit.text().strip()
        pw = self.pass_edit.text().strip()
        if not (server and user and pw):
            QMessageBox.warning(self, "Eksik bilgi", "Sunucu / kullanıcı / şifre boş olamaz.")
            return None
        return Source(name=name, kind=kind, server=server, username=user, password=pw)


# ───────────────────────── Kanal (link) Ekle/Düzenle Diyaloğu ─────────────────────────

class ChannelDialog(QDialog):
    def __init__(self, parent=None, channel: Optional[dict] = None):
        super().__init__(parent)
        self.setWindowTitle("Link Düzenle" if channel else "Link Ekle")
        self.setMinimumWidth(420)

        ch = channel or {}
        self.name_edit = QLineEdit(ch.get("name", ""))
        self.group_edit = QLineEdit(ch.get("group", ""))
        self.logo_edit = QLineEdit(ch.get("logo", ""))
        self.url_edit = QLineEdit(ch.get("url", ""))

        form = QFormLayout(self)
        form.addRow("Ad:", self.name_edit)
        form.addRow("Grup:", self.group_edit)
        form.addRow("Logo URL:", self.logo_edit)
        form.addRow("Yayın/Stream URL:", self.url_edit)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def get_channel(self) -> Optional[dict]:
        name = self.name_edit.text().strip()
        url = self.url_edit.text().strip()
        if not name or not url:
            QMessageBox.warning(self, "Eksik bilgi", "Ad ve URL boş olamaz.")
            return None
        return {
            "name": name,
            "group": self.group_edit.text().strip(),
            "logo": self.logo_edit.text().strip(),
            "url": url,
            "tvg_id": "",
        }


# ───────────────────────── Ana Pencere ─────────────────────────

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("IPTV Link Yöneticisi")
        self.resize(1000, 620)

        self.sources: List[Source] = load_sources()
        self.current_source: Optional[Source] = None
        self.all_channels: List[dict] = []

        # ---- Sol panel: kaynaklar ----
        self.source_list = QListWidget()
        self.source_list.currentRowChanged.connect(self._on_source_selected)

        add_src_btn = QPushButton("Kaynak Ekle")
        edit_src_btn = QPushButton("Düzenle")
        del_src_btn = QPushButton("Sil")
        refresh_btn = QPushButton("Yenile / Çek")

        add_src_btn.clicked.connect(self._add_source)
        edit_src_btn.clicked.connect(self._edit_source)
        del_src_btn.clicked.connect(self._delete_source)
        refresh_btn.clicked.connect(self._refresh_current_source)

        left = QWidget()
        left_l = QVBoxLayout(left)
        left_l.addWidget(QLabel("Kaynaklarım (M3U / Xtream)"))
        left_l.addWidget(self.source_list)
        row = QHBoxLayout()
        for b in (add_src_btn, edit_src_btn, del_src_btn):
            row.addWidget(b)
        left_l.addLayout(row)
        left_l.addWidget(refresh_btn)

        # ---- Sağ panel: kanallar / linkler ----
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Ara (ad veya grup)...")
        self.search_edit.textChanged.connect(self._apply_filter)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Ad", "Grup", "URL"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.doubleClicked.connect(self._play_selected)

        add_ch_btn = QPushButton("Link Ekle")
        edit_ch_btn = QPushButton("Link Düzenle")
        del_ch_btn = QPushButton("Link Sil")
        play_btn = QPushButton("Oynat")
        export_btn = QPushButton("M3U Olarak Dışa Aktar")

        add_ch_btn.clicked.connect(self._add_channel)
        edit_ch_btn.clicked.connect(self._edit_channel)
        del_ch_btn.clicked.connect(self._delete_channel)
        play_btn.clicked.connect(self._play_selected)
        export_btn.clicked.connect(self._export_m3u)

        right = QWidget()
        right_l = QVBoxLayout(right)
        right_l.addWidget(self.search_edit)
        right_l.addWidget(self.table)
        row2 = QHBoxLayout()
        for b in (add_ch_btn, edit_ch_btn, del_ch_btn, play_btn, export_btn):
            row2.addWidget(b)
        right_l.addLayout(row2)

        splitter = QSplitter()
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setSizes([260, 740])
        self.setCentralWidget(splitter)

        self.statusBar().showMessage("Hazır")
        self._reload_source_list()

    # ---- Kaynak listesi ----

    def _reload_source_list(self):
        self.source_list.clear()
        for s in self.sources:
            self.source_list.addItem(QListWidgetItem(f"{s.name}  [{s.kind}]"))

    def _add_source(self):
        dlg = SourceDialog(self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            src = dlg.get_source()
            if src:
                self.sources.append(src)
                save_sources(self.sources)
                self._reload_source_list()
                self.source_list.setCurrentRow(len(self.sources) - 1)

    def _edit_source(self):
        row = self.source_list.currentRow()
        if row < 0:
            return
        old = self.sources[row]
        dlg = SourceDialog(self, old)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            src = dlg.get_source()
            if src:
                src.channels = old.channels  # önceki çekilen kanalları koru
                self.sources[row] = src
                save_sources(self.sources)
                self._reload_source_list()
                self.source_list.setCurrentRow(row)

    def _delete_source(self):
        row = self.source_list.currentRow()
        if row < 0:
            return
        if QMessageBox.question(self, "Emin misin?", "Bu kaynağı silmek istiyor musun?") \
                != QMessageBox.StandardButton.Yes:
            return
        del self.sources[row]
        save_sources(self.sources)
        self._reload_source_list()
        self.table.setRowCount(0)
        self.current_source = None

    def _on_source_selected(self, row: int):
        if row < 0 or row >= len(self.sources):
            self.current_source = None
            self.table.setRowCount(0)
            return
        self.current_source = self.sources[row]
        self.all_channels = self.current_source.channels
        self._apply_filter()
        self.statusBar().showMessage(f"{self.current_source.name}: {len(self.all_channels)} kayıtlı link")

    def _refresh_current_source(self):
        if not self.current_source:
            QMessageBox.information(self, "Kaynak seç", "Önce soldan bir kaynak seç.")
            return
        try:
            text = fetch_m3u_text(self.current_source)
            channels = parse_m3u(text)
        except Exception as e:
            QMessageBox.critical(self, "Hata", f"Çekilemedi:\n{e}")
            return
        self.current_source.channels = channels
        self.all_channels = channels
        save_sources(self.sources)
        self._apply_filter()
        self.statusBar().showMessage(f"{len(channels)} link çekildi ve kaydedildi.")

    # ---- Kanal (link) tablosu ----

    def _apply_filter(self):
        query = self.search_edit.text().strip().lower()
        rows = [c for c in self.all_channels
                if query in c.get("name", "").lower() or query in c.get("group", "").lower()] if query \
            else list(self.all_channels)
        self.table.setRowCount(len(rows))
        for i, ch in enumerate(rows):
            self.table.setItem(i, 0, QTableWidgetItem(ch.get("name", "")))
            self.table.setItem(i, 1, QTableWidgetItem(ch.get("group", "")))
            self.table.setItem(i, 2, QTableWidgetItem(ch.get("url", "")))
        self._filtered = rows  # görünen satırların gerçek referansları

    def _selected_channel_index(self) -> Optional[int]:
        row = self.table.currentRow()
        if row < 0 or row >= len(getattr(self, "_filtered", [])):
            return None
        target = self._filtered[row]
        # gerçek listedeki index'i bul (aynı obje referansı)
        for i, c in enumerate(self.all_channels):
            if c is target:
                return i
        return None

    def _add_channel(self):
        if self.current_source is None:
            QMessageBox.information(self, "Kaynak seç", "Önce soldan bir kaynak seç (veya oluştur).")
            return
        dlg = ChannelDialog(self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            ch = dlg.get_channel()
            if ch:
                self.all_channels.append(ch)
                self.current_source.channels = self.all_channels
                save_sources(self.sources)
                self._apply_filter()

    def _edit_channel(self):
        idx = self._selected_channel_index()
        if idx is None:
            QMessageBox.information(self, "Seçim yok", "Önce bir link seç.")
            return
        dlg = ChannelDialog(self, self.all_channels[idx])
        if dlg.exec() == QDialog.DialogCode.Accepted:
            ch = dlg.get_channel()
            if ch:
                self.all_channels[idx] = ch
                self.current_source.channels = self.all_channels
                save_sources(self.sources)
                self._apply_filter()

    def _delete_channel(self):
        idx = self._selected_channel_index()
        if idx is None:
            QMessageBox.information(self, "Seçim yok", "Önce bir link seç.")
            return
        del self.all_channels[idx]
        self.current_source.channels = self.all_channels
        save_sources(self.sources)
        self._apply_filter()

    def _play_selected(self):
        idx = self._selected_channel_index()
        if idx is None:
            return
        url = self.all_channels[idx].get("url", "")
        try:
            play_url(url)
        except Exception as e:
            QMessageBox.critical(self, "Oynatılamadı", str(e))

    def _export_m3u(self):
        if not self.all_channels:
            QMessageBox.information(self, "Boş", "Dışa aktarılacak link yok.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "M3U olarak kaydet", "playlist.m3u", "M3U (*.m3u)")
        if not path:
            return
        with open(path, "w", encoding="utf-8") as f:
            f.write(channels_to_m3u(self.all_channels))
        self.statusBar().showMessage(f"Kaydedildi: {path}")


def main():
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
