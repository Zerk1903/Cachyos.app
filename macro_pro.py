#!/usr/bin/env python3
"""
Macro Pro - Mouse Macro Recorder & Player
Kurulum: pip install pynput pyautogui
"""

import os, time, json, threading
import tkinter as tk
from tkinter import filedialog, messagebox
from pynput import mouse, keyboard
from pynput.mouse import Button, Controller as MouseCtrl
from pynput.keyboard import Key, Listener as KeyListener
import pyautogui

pyautogui.FAILSAFE = True

# ── Globals ────────────────────────────────────────────────
macro_events      = []
is_recording      = False
is_playing        = False
record_start_time = 0.0
mouse_ctrl        = MouseCtrl()

# ── Renk paleti ───────────────────────────────────────────
BG    = "#0f0f0f"
CARD  = "#1a1a1a"
ACC   = "#00e5ff"
TXT   = "#e0e0e0"
DIM   = "#555"
RED   = "#ff4444"
GRN   = "#00ff88"
YLW   = "#ffcc00"
FONT  = ("Consolas", 10)
FONTH = ("Consolas", 11, "bold")


# ── Kayıt dinleyicileri ────────────────────────────────────
def on_move(x, y):
    if is_recording:
        macro_events.append({
            "type": "move", "x": x, "y": y,
            "time": time.time() - record_start_time
        })

def on_click_rec(x, y, button, pressed):
    if is_recording:
        macro_events.append({
            "type": "click", "x": x, "y": y,
            "button": str(button), "pressed": pressed,
            "time": time.time() - record_start_time
        })

def on_scroll_rec(x, y, dx, dy):
    if is_recording:
        macro_events.append({
            "type": "scroll", "x": x, "y": y,
            "dx": dx, "dy": dy,
            "time": time.time() - record_start_time
        })

mouse_listener = mouse.Listener(
    on_move=on_move,
    on_click=on_click_rec,
    on_scroll=on_scroll_rec
)
mouse_listener.start()


# ── Oynatma thread'i ───────────────────────────────────────
def run_macro(events, repeat, speed, on_done):
    global is_playing
    for i in range(repeat):
        if not is_playing:
            break
        prev = 0.0
        for ev in events:
            if not is_playing:
                break
            delay = (ev["time"] - prev) / speed
            if delay > 0:
                time.sleep(delay)
            prev = ev["time"]

            t = ev["type"]
            if t == "move":
                mouse_ctrl.position = (ev["x"], ev["y"])
            elif t == "click":
                mouse_ctrl.position = (ev["x"], ev["y"])
                # Kaydedilen orijinal butonu kullan
                btn_str = ev.get("button", "Button.left")
                btn_map = {
                    "Button.left": Button.left,
                    "Button.right": Button.right,
                    "Button.middle": Button.middle,
                }
                btn = btn_map.get(btn_str, Button.left)
                if ev["pressed"]:
                    mouse_ctrl.press(btn)
                else:
                    mouse_ctrl.release(btn)
            elif t == "scroll":
                mouse_ctrl.position = (ev["x"], ev["y"])
                mouse_ctrl.scroll(ev["dx"], ev["dy"])

    is_playing = False
    on_done()


# ── Ana uygulama ───────────────────────────────────────────
class App:
    def __init__(self, root):
        self.root = root
        self.root.title("Macro Pro")
        self.root.geometry("500x580")
        self.root.resizable(False, False)
        self.root.configure(bg=BG)

        # Tetikleyici tuş
        self.trigger_btn     = None      # pynput Button objesi
        self.trigger_raw     = ""        # str temsili
        self._assigning      = False     # atama modunda mı?
        self.play_thread     = None
        self.last_path       = ""        # son açılan/kaydedilen dosya

        self._build_ui()
        self._start_f7_listener()
        self._start_trigger_listener()
        self.root.protocol("WM_DELETE_WINDOW", self.root.destroy)

    # ── UI ────────────────────────────────────────────────
    def _build_ui(self):
        # Başlık
        tk.Frame(self.root, bg=ACC, height=46).pack(fill="x")
        tk.Label(self.root, text="● MACRO PRO",
                 font=("Consolas", 14, "bold"), bg=ACC, fg=BG
                 ).place(x=0, y=0, width=500, height=46)

        # Durum
        self.status_var = tk.StringVar(value="Hazır — F7 ile kayıt başlat")
        tk.Label(self.root, textvariable=self.status_var,
                 font=FONT, bg=BG, fg=ACC).pack(pady=(10, 4))

        # ── KAYIT bölümü ──
        rec = tk.LabelFrame(self.root, text=" KAYIT ", font=FONTH,
                            bg=CARD, fg=ACC, bd=1, relief="solid", labelanchor="n")
        rec.pack(fill="x", padx=16, pady=6)

        tk.Label(rec, text="F7 → Kaydı Başlat / Durdur",
                 font=FONT, bg=CARD, fg=DIM).pack(pady=(6,2))

        self.event_var = tk.StringVar(value="Olay sayısı: 0")
        tk.Label(rec, textvariable=self.event_var,
                 font=FONT, bg=CARD, fg=TXT).pack()

        row = tk.Frame(rec, bg=CARD)
        row.pack(pady=8)
        self._btn(row, "💾 Kaydet", ACC,  BG,  self.save_macro ).pack(side="left", padx=5)
        self._btn(row, "📂 Aç",     YLW,  BG,  self.load_macro ).pack(side="left", padx=5)
        self._btn(row, "🗑 Temizle", RED, "#fff", self.clear_macro).pack(side="left", padx=5)

        self.path_var = tk.StringVar(value="")
        tk.Label(rec, textvariable=self.path_var,
                 font=("Consolas", 8), bg=CARD, fg=DIM).pack(pady=(0, 6))

        # ── OYNATMA bölümü ──
        pf = tk.LabelFrame(self.root, text=" OYNATMA ", font=FONTH,
                           bg=CARD, fg=ACC, bd=1, relief="solid", labelanchor="n")
        pf.pack(fill="x", padx=16, pady=6)

        # Tetikleyici tuş
        trow = tk.Frame(pf, bg=CARD)
        trow.pack(fill="x", padx=12, pady=(10, 4))
        tk.Label(trow, text="Tetikleyici Tuş:", font=FONT,
                 bg=CARD, fg=TXT, width=16, anchor="w").pack(side="left")
        self.trig_var = tk.StringVar(value="— Atanmadı —")
        tk.Label(trow, textvariable=self.trig_var,
                 font=("Consolas", 10, "bold"), bg="#222", fg=YLW,
                 padx=8, pady=3, width=18, anchor="w", relief="flat"
                 ).pack(side="left", padx=6)
        self.assign_btn = self._btn(trow, "Ata", YLW, BG, self.start_assign)
        self.assign_btn.pack(side="left", padx=2)
        self._btn(trow, "✕", DIM, TXT, self.clear_trigger, padx=6).pack(side="left", padx=2)

        tk.Label(pf, text="Bu tuşa basınca makro başlar, tekrar basınca durur.",
                 font=("Consolas", 8), bg=CARD, fg=DIM).pack(padx=12, anchor="w")

        tk.Frame(pf, bg=DIM, height=1).pack(fill="x", padx=12, pady=8)

        # Tekrar
        rrow = tk.Frame(pf, bg=CARD)
        rrow.pack(fill="x", padx=12, pady=3)
        tk.Label(rrow, text="Tekrar Sayısı:", font=FONT,
                 bg=CARD, fg=TXT, width=16, anchor="w").pack(side="left")
        self.repeat_var = tk.IntVar(value=1)
        tk.Spinbox(rrow, from_=1, to=9999, textvariable=self.repeat_var,
                   width=8, font=FONT, bg="#222", fg=TXT,
                   insertbackground=ACC, relief="flat").pack(side="left")

        # Hız
        srow = tk.Frame(pf, bg=CARD)
        srow.pack(fill="x", padx=12, pady=3)
        tk.Label(srow, text="Hız Çarpanı:", font=FONT,
                 bg=CARD, fg=TXT, width=16, anchor="w").pack(side="left")
        self.speed_var = tk.DoubleVar(value=1.0)
        self.speed_lbl = tk.Label(srow, text="1.0x", font=FONTH,
                                  bg=CARD, fg=ACC, width=6)
        self.speed_lbl.pack(side="right", padx=4)
        tk.Scale(srow, from_=0.1, to=20, resolution=0.1,
                 orient="horizontal", variable=self.speed_var,
                 command=lambda v: self.speed_lbl.config(
                     text=f"{float(v):.1f}x"),
                 bg=CARD, fg=TXT, troughcolor="#222",
                 activebackground=ACC, highlightthickness=0,
                 length=220, showvalue=False).pack(side="left")

        # Oynat / Durdur
        br = tk.Frame(pf, bg=CARD)
        br.pack(pady=10)
        self.play_btn = self._btn(br, "▶ Başlat", GRN, BG, self.start_play)
        self.play_btn.pack(side="left", padx=8)
        self.stop_btn = self._btn(br, "⏹ Durdur", RED, "#fff", self.stop_play)
        self.stop_btn.pack(side="left", padx=8)
        self.stop_btn.config(state="disabled")

        tk.Label(self.root,
                 text="F7 = Kayıt Başlat/Durdur  |  Failsafe = Sol Üst Köşeye Git",
                 font=("Consolas", 7), bg=BG, fg=DIM).pack(side="bottom", pady=6)

    def _btn(self, parent, text, bg, fg, cmd, **kw):
        kw.setdefault("padx", 10)
        kw.setdefault("pady", 4)
        return tk.Button(parent, text=text, bg=bg, fg=fg,
                         activebackground=bg, activeforeground=fg,
                         font=FONTH, relief="flat", cursor="hand2",
                         command=cmd, **kw)

    # ── F7 Kayıt ──────────────────────────────────────────
    def _start_f7_listener(self):
        def _on_key(key):
            if key == Key.f7:
                self.root.after(0, self.toggle_record)
        kl = KeyListener(on_press=_on_key)
        kl.daemon = True
        kl.start()

    def toggle_record(self):
        global is_recording, macro_events, record_start_time
        if is_playing:
            return
        if not is_recording:
            macro_events = []
            record_start_time = time.time()
            is_recording = True
            self.status_var.set("🔴 Kaydediliyor... (F7 ile durdur)")
            self._tick()
        else:
            is_recording = False
            self.event_var.set(f"Olay sayısı: {len(macro_events)}")
            self.status_var.set(f"⏸ Kayıt durdu — {len(macro_events)} olay")

    def _tick(self):
        if is_recording:
            self.event_var.set(f"Olay sayısı: {len(macro_events)}")
            self.root.after(200, self._tick)

    # ── Tetikleyici Tuş ───────────────────────────────────
    def _start_trigger_listener(self):
        """Sürekli arka planda çalışır, tetikleyici tuşu izler."""
        def _watch(x, y, button, pressed):
            if not pressed or self._assigning:
                return
            if self.trigger_btn and str(button) == self.trigger_raw:
                self.root.after(0, self._toggle_play_trigger)

        tl = mouse.Listener(on_click=_watch)
        tl.daemon = True
        tl.start()

    def _toggle_play_trigger(self):
        if is_playing:
            self.stop_play()
        else:
            self.start_play()

    def start_assign(self):
        if self._assigning:
            return
        self._assigning = True
        self.assign_btn.config(text="⌛ Tuşa bas...", state="disabled",
                               bg="#333", fg=YLW)
        self.status_var.set("🖱 Tetikleyici olarak atamak istediğin mouse tuşuna bas...")
        threading.Thread(target=self._listen_assign, daemon=True).start()

    def _listen_assign(self):
        captured = []
        def _catch(x, y, button, pressed):
            if pressed and not captured:
                captured.append(button)
                return False
        with mouse.Listener(on_click=_catch) as lst:
            lst.join()
        if captured:
            btn = captured[0]
            self.trigger_btn = btn
            self.trigger_raw = str(btn)
            label = {
                "Button.left": "Sol Tık",
                "Button.right": "Sağ Tık",
                "Button.middle": "Orta Tuş",
            }.get(self.trigger_raw, self.trigger_raw)
            self.root.after(0, lambda: self._finish_assign(label))

    def _finish_assign(self, label):
        self._assigning = False
        self.trig_var.set(label)
        self.assign_btn.config(text="Ata", state="normal", bg=YLW, fg=BG)
        self.status_var.set(f"✅ Tetikleyici atandı → {label}")

    def clear_trigger(self):
        self.trigger_btn = None
        self.trigger_raw = ""
        self.trig_var.set("— Atanmadı —")
        self.status_var.set("Tetikleyici kaldırıldı")

    # ── Makro Kaydet / Aç ─────────────────────────────────
    def save_macro(self):
        if not macro_events:
            messagebox.showwarning("Uyarı", "Kaydedilecek makro yok!")
            return
        init = os.path.dirname(self.last_path) if self.last_path else os.path.expanduser("~")
        path = filedialog.asksaveasfilename(
            defaultextension=".json",
            filetypes=[("Makro JSON", "*.json"), ("Tümü", "*.*")],
            title="Makroyu Kaydet",
            initialdir=init,
        )
        if not path:
            return
        with open(path, "w", encoding="utf-8") as f:
            json.dump(macro_events, f, ensure_ascii=False, indent=2)
        self.last_path = path
        self.path_var.set(f"📄 {os.path.basename(path)}")
        self.status_var.set(f"✅ Kaydedildi: {os.path.basename(path)}")

    def load_macro(self):
        init = os.path.dirname(self.last_path) if self.last_path else os.path.expanduser("~")
        path = filedialog.askopenfilename(
            filetypes=[("Makro JSON", "*.json"), ("Tümü", "*.*")],
            title="Makro Aç",
            initialdir=init,
        )
        if not path:
            return
        self._load_from(path)

    def _load_from(self, path):
        global macro_events
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, list):
                raise ValueError("Geçersiz format: JSON listesi bekleniyor.")
            macro_events = data
            self.last_path = path
            self.path_var.set(f"📄 {os.path.basename(path)}")
            self.event_var.set(f"Olay sayısı: {len(macro_events)}")
            self.status_var.set(f"📂 Yüklendi: {os.path.basename(path)}")
            self.play_btn.config(state="normal")   # ← kritik
        except Exception as e:
            messagebox.showerror("Hata", f"Dosya açılamadı:\n{e}")

    def clear_macro(self):
        global macro_events
        if is_playing:
            return
        macro_events = []
        self.last_path = ""
        self.path_var.set("")
        self.event_var.set("Olay sayısı: 0")
        self.status_var.set("🗑 Makro temizlendi")

    # ── Oynatma ───────────────────────────────────────────
    def start_play(self):
        global is_playing
        if not macro_events:
            messagebox.showwarning("Uyarı", "Oynatılacak makro yok!\nÖnce F7 ile kayıt yapın veya dosya açın.")
            return
        if is_playing or is_recording:
            return
        is_playing = True
        self.play_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.status_var.set(f"▶ Oynatılıyor... (×{self.repeat_var.get()})")
        self.play_thread = threading.Thread(
            target=run_macro,
            args=(list(macro_events), self.repeat_var.get(),
                  self.speed_var.get(), self._play_done),
            daemon=True,
        )
        self.play_thread.start()

    def stop_play(self):
        global is_playing
        is_playing = False
        self.play_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        self.status_var.set("⏹ Durduruldu")

    def _play_done(self):
        self.root.after(0, lambda: (
            self.play_btn.config(state="normal"),
            self.stop_btn.config(state="disabled"),
            self.status_var.set("✅ Tamamlandı"),
        ))


# ── Başlat ────────────────────────────────────────────────
if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
