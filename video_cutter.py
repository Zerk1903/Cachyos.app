#!/usr/bin/env python3
"""
Video Cutter - Kare kare ileri/geri sarma özellikli video kesme programı
CachyOS / Arch Linux için optimize edilmiştir.
Çıktı: x264 MP4
"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import cv2
from PIL import Image, ImageTk
import subprocess
import threading
import os
import sys
import shutil
from pathlib import Path


class VideoCutterApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Video Cutter - Kare Kare Kesim")
        self.root.geometry("1000x720")
        self.root.minsize(800, 600)

        # Video state
        self.cap = None
        self.video_path = None
        self.total_frames = 0
        self.fps = 30.0
        self.current_frame = 0
        self.duration = 0.0
        self.is_playing = False
        self.play_job = None

        # Cut points
        self.start_frame = 0
        self.end_frame = 0

        # Display
        self.photo = None
        self.display_width = 800
        self.display_height = 450

        self._check_dependencies()
        self._build_ui()
        self._bind_keys()

    def _check_dependencies(self):
        """ffmpeg ve opencv kontrolü"""
        if not shutil.which("ffmpeg"):
            messagebox.showwarning(
                "Eksik Bağımlılık",
                "ffmpeg bulunamadı!\n\n"
                "CachyOS'ta kurmak için:\n"
                "sudo pacman -S ffmpeg"
            )
        try:
            import cv2
            from PIL import Image
        except ImportError:
            messagebox.showerror(
                "Eksik Python Paketi",
                "opencv-python veya Pillow eksik!\n\n"
                "Kurulum:\n"
                "sudo pacman -S python-opencv python-pillow\n"
                "veya\n"
                "pip install opencv-python pillow"
            )
            sys.exit(1)

    def _build_ui(self):
        # Ana çerçeve
        main = ttk.Frame(self.root, padding=8)
        main.pack(fill=tk.BOTH, expand=True)

        # --- Üst: Video önizleme ---
        preview_frame = ttk.LabelFrame(main, text="Önizleme", padding=4)
        preview_frame.pack(fill=tk.BOTH, expand=True)

        self.canvas = tk.Canvas(
            preview_frame,
            bg="#1a1a1a",
            width=self.display_width,
            height=self.display_height,
            highlightthickness=0
        )
        self.canvas.pack(fill=tk.BOTH, expand=True)

        # --- Bilgi satırı ---
        info_frame = ttk.Frame(main)
        info_frame.pack(fill=tk.X, pady=(6, 2))

        self.info_label = ttk.Label(
            info_frame,
            text="Video yüklenmedi  |  Kare: - / -  |  Zaman: --:--.-- / --:--.--",
            font=("Consolas", 10)
        )
        self.info_label.pack(side=tk.LEFT)

        self.cut_info_label = ttk.Label(
            info_frame,
            text="",
            font=("Consolas", 10),
            foreground="#00aa00"
        )
        self.cut_info_label.pack(side=tk.RIGHT)

        # --- Seek slider ---
        slider_frame = ttk.Frame(main)
        slider_frame.pack(fill=tk.X, pady=4)

        self.seek_var = tk.DoubleVar(value=0)
        self.seek_slider = ttk.Scale(
            slider_frame,
            from_=0,
            to=100,
            orient=tk.HORIZONTAL,
            variable=self.seek_var,
            command=self._on_seek
        )
        self.seek_slider.pack(fill=tk.X)

        # --- Kontroller ---
        ctrl = ttk.Frame(main)
        ctrl.pack(fill=tk.X, pady=4)

        # Sol: navigasyon
        nav = ttk.Frame(ctrl)
        nav.pack(side=tk.LEFT)

        ttk.Button(nav, text="⏮  -10", width=8, command=lambda: self._step(-10)).pack(side=tk.LEFT, padx=2)
        ttk.Button(nav, text="◀  -1", width=7, command=lambda: self._step(-1)).pack(side=tk.LEFT, padx=2)
        self.play_btn = ttk.Button(nav, text="▶ Oynat", width=10, command=self._toggle_play)
        self.play_btn.pack(side=tk.LEFT, padx=4)
        ttk.Button(nav, text="+1  ▶", width=7, command=lambda: self._step(1)).pack(side=tk.LEFT, padx=2)
        ttk.Button(nav, text="+10  ⏭", width=8, command=lambda: self._step(10)).pack(side=tk.LEFT, padx=2)

        # Orta: kesim noktaları
        cut = ttk.Frame(ctrl)
        cut.pack(side=tk.LEFT, padx=20)

        ttk.Button(cut, text="✂ Başlangıç Ayarla", command=self._set_start).pack(side=tk.LEFT, padx=3)
        ttk.Button(cut, text="✂ Bitiş Ayarla", command=self._set_end).pack(side=tk.LEFT, padx=3)
        ttk.Button(cut, text="↺ Sıfırla", command=self._reset_cut).pack(side=tk.LEFT, padx=3)

        # Sağ: dosya işlemleri
        file_btns = ttk.Frame(ctrl)
        file_btns.pack(side=tk.RIGHT)

        ttk.Button(file_btns, text="📂 Video Aç", command=self._open_video).pack(side=tk.LEFT, padx=3)
        ttk.Button(file_btns, text="💾 Kes & Kaydet (x264)", command=self._export).pack(side=tk.LEFT, padx=3)

        # --- Durum çubuğu ---
        self.status = ttk.Label(main, text="Hazır. Video açmak için 'Video Aç' butonuna tıklayın.", relief=tk.SUNKEN, anchor=tk.W)
        self.status.pack(fill=tk.X, pady=(6, 0))

        # Kısayol bilgisi
        help_text = "Kısayollar:  ← →  : ±1 kare   |   Shift+← →  : ±10 kare   |   Space : Oynat/Duraklat   |   S : Başlangıç   |   E : Bitiş"
        ttk.Label(main, text=help_text, font=("Segoe UI", 8), foreground="#666").pack(pady=(4, 0))

    def _bind_keys(self):
        self.root.bind("<Left>", lambda e: self._step(-1))
        self.root.bind("<Right>", lambda e: self._step(1))
        self.root.bind("<Shift-Left>", lambda e: self._step(-10))
        self.root.bind("<Shift-Right>", lambda e: self._step(10))
        self.root.bind("<space>", lambda e: self._toggle_play())
        self.root.bind("s", lambda e: self._set_start())
        self.root.bind("S", lambda e: self._set_start())
        self.root.bind("e", lambda e: self._set_end())
        self.root.bind("E", lambda e: self._set_end())
        self.root.bind("<Control-o>", lambda e: self._open_video())
        self.root.focus_set()

    # ───────────────────────── Video işlemleri ─────────────────────────

    def _open_video(self):
        path = filedialog.askopenfilename(
            title="Video Seç",
            filetypes=[
                ("Video dosyaları", "*.mp4 *.mkv *.avi *.mov *.webm *.flv *.wmv *.m4v"),
                ("Tüm dosyalar", "*.*")
            ]
        )
        if not path:
            return

        if self.cap is not None:
            self.cap.release()
            self._stop_play()

        self.cap = cv2.VideoCapture(path)
        if not self.cap.isOpened():
            messagebox.showerror("Hata", f"Video açılamadı:\n{path}")
            self.cap = None
            return

        self.video_path = path
        self.total_frames = int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT))
        self.fps = self.cap.get(cv2.CAP_PROP_FPS) or 30.0
        self.duration = self.total_frames / self.fps if self.fps > 0 else 0
        self.current_frame = 0
        self.start_frame = 0
        self.end_frame = max(0, self.total_frames - 1)

        self.seek_slider.configure(to=max(0, self.total_frames - 1))
        self.seek_var.set(0)

        self._show_frame(0)
        self._update_info()
        self._update_cut_info()
        self.status.config(text=f"Yüklendi: {os.path.basename(path)}  |  {self.total_frames} kare  |  {self.fps:.2f} fps")

    def _show_frame(self, frame_idx):
        if self.cap is None:
            return

        frame_idx = max(0, min(frame_idx, self.total_frames - 1))
        self.cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
        ret, frame = self.cap.read()
        if not ret:
            return

        self.current_frame = frame_idx
        self.seek_var.set(frame_idx)

        # BGR → RGB
        frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

        # Canvas boyutuna göre ölçekle (aspect ratio koru)
        h, w = frame.shape[:2]
        canvas_w = self.canvas.winfo_width() or self.display_width
        canvas_h = self.canvas.winfo_height() or self.display_height

        scale = min(canvas_w / w, canvas_h / h)
        new_w = int(w * scale)
        new_h = int(h * scale)

        if new_w > 0 and new_h > 0:
            frame = cv2.resize(frame, (new_w, new_h), interpolation=cv2.INTER_AREA)

        img = Image.fromarray(frame)
        self.photo = ImageTk.PhotoImage(image=img)

        self.canvas.delete("all")
        self.canvas.create_image(canvas_w // 2, canvas_h // 2, image=self.photo, anchor=tk.CENTER)

        self._update_info()

    def _step(self, delta):
        if self.cap is None:
            return
        self._stop_play()
        new_frame = self.current_frame + delta
        self._show_frame(new_frame)

    def _on_seek(self, value):
        if self.cap is None:
            return
        # Slider sürüklenirken oynatmayı durdur
        if self.is_playing:
            self._stop_play()
        frame_idx = int(float(value))
        if frame_idx != self.current_frame:
            self._show_frame(frame_idx)

    def _toggle_play(self):
        if self.cap is None:
            return
        if self.is_playing:
            self._stop_play()
        else:
            self.is_playing = True
            self.play_btn.config(text="⏸ Duraklat")
            self._play_loop()

    def _stop_play(self):
        self.is_playing = False
        self.play_btn.config(text="▶ Oynat")
        if self.play_job is not None:
            self.root.after_cancel(self.play_job)
            self.play_job = None

    def _play_loop(self):
        if not self.is_playing or self.cap is None:
            return
        next_frame = self.current_frame + 1
        if next_frame >= self.total_frames:
            self._stop_play()
            return
        self._show_frame(next_frame)
        delay = max(1, int(1000 / self.fps))
        self.play_job = self.root.after(delay, self._play_loop)

    # ───────────────────────── Kesim noktaları ─────────────────────────

    def _set_start(self):
        if self.cap is None:
            return
        self.start_frame = self.current_frame
        if self.start_frame > self.end_frame:
            self.end_frame = self.start_frame
        self._update_cut_info()
        self.status.config(text=f"Başlangıç karesi ayarlandı: {self.start_frame}")

    def _set_end(self):
        if self.cap is None:
            return
        self.end_frame = self.current_frame
        if self.end_frame < self.start_frame:
            self.start_frame = self.end_frame
        self._update_cut_info()
        self.status.config(text=f"Bitiş karesi ayarlandı: {self.end_frame}")

    def _reset_cut(self):
        if self.cap is None:
            return
        self.start_frame = 0
        self.end_frame = max(0, self.total_frames - 1)
        self._update_cut_info()
        self.status.config(text="Kesim noktaları sıfırlandı.")

    def _update_cut_info(self):
        if self.cap is None:
            self.cut_info_label.config(text="")
            return
        start_t = self._fmt_time(self.start_frame / self.fps)
        end_t = self._fmt_time(self.end_frame / self.fps)
        duration_f = max(0, self.end_frame - self.start_frame)
        dur_t = self._fmt_time(duration_f / self.fps)
        self.cut_info_label.config(
            text=f"Kesim: {self.start_frame} → {self.end_frame}  ({start_t} → {end_t})  |  Süre: {dur_t}"
        )

    def _update_info(self):
        if self.cap is None:
            return
        cur_t = self._fmt_time(self.current_frame / self.fps)
        tot_t = self._fmt_time(self.duration)
        self.info_label.config(
            text=f"Kare: {self.current_frame} / {self.total_frames - 1}  |  Zaman: {cur_t} / {tot_t}  |  FPS: {self.fps:.2f}"
        )

    @staticmethod
    def _fmt_time(seconds):
        if seconds < 0:
            seconds = 0
        m = int(seconds // 60)
        s = seconds % 60
        return f"{m:02d}:{s:05.2f}"

    # ───────────────────────── Dışa aktarma ─────────────────────────

    def _export(self):
        if self.cap is None or not self.video_path:
            messagebox.showwarning("Uyarı", "Önce bir video açın.")
            return

        if self.start_frame >= self.end_frame:
            messagebox.showwarning("Uyarı", "Başlangıç karesi bitiş karesinden küçük olmalıdır.")
            return

        out_path = filedialog.asksaveasfilename(
            title="Kesilmiş Videoyu Kaydet",
            defaultextension=".mp4",
            filetypes=[("MP4 dosyası", "*.mp4")],
            initialfile=Path(self.video_path).stem + "_cut.mp4"
        )
        if not out_path:
            return

        start_sec = self.start_frame / self.fps
        end_sec = (self.end_frame + 1) / self.fps  # bitiş karesi dahil

        self.status.config(text="Kesiliyor... Lütfen bekleyin.")
        self.root.config(cursor="watch")
        self.root.update()

        # Arka planda çalıştır
        thread = threading.Thread(
            target=self._run_ffmpeg,
            args=(self.video_path, out_path, start_sec, end_sec),
            daemon=True
        )
        thread.start()

    def _run_ffmpeg(self, input_path, output_path, start_sec, end_sec):
        """Hassas kesim için ffmpeg kullan (x264)"""
        # -ss input'tan sonra = daha yavaş ama kare-doğru
        # -c:v libx264 -crf 18 = yüksek kalite
        cmd = [
            "ffmpeg",
            "-y",
            "-ss", f"{start_sec:.6f}",
            "-to", f"{end_sec:.6f}",
            "-i", input_path,
            "-c:v", "libx264",
            "-preset", "medium",
            "-crf", "18",
            "-c:a", "aac",
            "-b:a", "192k",
            "-movflags", "+faststart",
            "-pix_fmt", "yuv420p",
            output_path
        ]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False
            )
            self.root.after(0, lambda: self._export_done(result.returncode == 0, output_path, result.stderr))
        except Exception as e:
            self.root.after(0, lambda: self._export_done(False, output_path, str(e)))

    def _export_done(self, success, path, error_msg):
        self.root.config(cursor="")
        if success:
            self.status.config(text=f"Başarıyla kaydedildi: {path}")
            messagebox.showinfo("Tamam", f"Video başarıyla kesildi ve kaydedildi:\n\n{path}")
        else:
            self.status.config(text="Kesme işlemi başarısız.")
            # Kısa hata mesajı göster
            short = error_msg[-500:] if error_msg else "Bilinmeyen hata"
            messagebox.showerror("Hata", f"ffmpeg hatası:\n\n{short}")


def main():
    root = tk.Tk()
    # Tema (ttk)
    try:
        style = ttk.Style()
        if "clam" in style.theme_names():
            style.theme_use("clam")
    except Exception:
        pass

    app = VideoCutterApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
