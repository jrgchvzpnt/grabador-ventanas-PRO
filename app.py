"""
Grabador de ventanas (por ejemplo, una ventana del navegador), con audio,
pausa, cronómetro y vista previa en vivo.

Permite elegir una ventana abierta de la lista, grabarla en video junto
con el audio del sistema, pausar/reanudar la grabación, ver una vista
previa y un cronómetro mientras se graba, y reproducir el resultado al
finalizar.

El video se codifica EN VIVO (cuadro a cuadro, vía un pipe a ffmpeg) ya en
el códec final del formato elegido, mientras se está grabando. Por eso,
al pulsar "Detener": si no hay audio, el archivo ya está listo al
instante; si hay audio, solo falta una mezcla rápida (copia el video sin
recodificar, únicamente codifica el audio), muchísimo más rápida que una
recompresión completa al final.

Nota sobre el audio: Windows no permite capturar el audio de UNA sola
ventana/pestaña de forma sencilla. Lo que se graba es el audio de salida
del dispositivo de sonido predeterminado (loopback), es decir, todo lo
que se está reproduciendo en el equipo mientras grabas. Si solo quieres
el audio del navegador, evita reproducir otros sonidos mientras grabas.

Requisitos: pip install -r requirements.txt
Uso: python app.py
"""

import datetime
import ctypes
import math
import os
import queue
import subprocess
import threading
import time
import wave
import webbrowser


def _enable_dpi_awareness():
    """Make Win32 window bounds use the same physical pixels as MSS."""
    try:
        user32 = ctypes.windll.user32
        set_context = getattr(user32, "SetProcessDpiAwarenessContext", None)
        if set_context and set_context(ctypes.c_void_p(-4)):
            return True
    except (AttributeError, OSError):
        pass

    try:
        if ctypes.windll.shcore.SetProcessDpiAwareness(2) == 0:
            return True
    except (AttributeError, OSError):
        pass

    try:
        return bool(ctypes.windll.user32.SetProcessDPIAware())
    except (AttributeError, OSError):
        return False


DPI_AWARENESS_ENABLED = _enable_dpi_awareness()

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import cv2
import imageio_ffmpeg
import mss
import numpy as np
import pygetwindow as gw
import win32api
import win32con
import win32gui
from PIL import Image, ImageDraw, ImageTk

try:
    import pyaudiowpatch as pyaudio

    HAS_AUDIO = True
except ImportError:
    HAS_AUDIO = False

try:
    from pycaw.pycaw import AudioUtilities

    HAS_VOLUME_CONTROL = True
except ImportError:
    HAS_VOLUME_CONTROL = False

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "recordings")
FPS = 15
AUDIO_CHUNK = 1024
PREVIEW_SIZE = (280, 158)  # 16:9
PREVIEW_INTERVAL_MS = 200

# Formatos de video disponibles para exportar (video/audio + contenedor).
# El video se codifica EN VIVO mientras se graba (no al detener), por eso
# "video_args_live" usa ajustes rápidos (aptos para tiempo real) en vez de
# los más lentos/compresivos que se usarían en una conversión offline.
FORMAT_OPTIONS = {
    "MP4 (.mp4)": {
        "ext": ".mp4",
        "video_args_live": ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p"],
        "audio_args": ["-c:a", "aac", "-b:a", "128k"],
    },
    "MKV (.mkv)": {
        "ext": ".mkv",
        "video_args_live": ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p"],
        "audio_args": ["-c:a", "aac", "-b:a", "128k"],
    },
    "MOV (.mov)": {
        "ext": ".mov",
        "video_args_live": ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p"],
        "audio_args": ["-c:a", "aac", "-b:a", "128k"],
    },
    "AVI (.avi)": {
        "ext": ".avi",
        "video_args_live": ["-c:v", "libxvid", "-qscale:v", "4", "-tag:v", "XVID"],
        "audio_args": ["-c:a", "libmp3lame", "-b:a", "192k"],
    },
    "WEBM (.webm)": {
        "ext": ".webm",
        "video_args_live": [
            "-c:v", "libvpx-vp9", "-crf", "32", "-b:v", "0", "-pix_fmt", "yuv420p",
            "-deadline", "realtime", "-cpu-used", "8",
        ],
        "audio_args": ["-c:a", "libopus", "-b:a", "128k"],
    },
}
DEFAULT_FORMAT = "MP4 (.mp4)"


def get_loopback_device(p):
    """Devuelve el dispositivo de loopback WASAPI del altavoz predeterminado."""
    try:
        wasapi_info = p.get_host_api_info_by_type(pyaudio.paWASAPI)
    except OSError:
        return None

    try:
        default_speakers = p.get_device_info_by_index(wasapi_info["defaultOutputDevice"])
    except OSError:
        return None

    if default_speakers.get("isLoopbackDevice", False):
        return default_speakers

    for loopback in p.get_loopback_device_info_generator():
        if default_speakers["name"] in loopback["name"]:
            return loopback

    return None


def parse_duration(value):
    """Convierte MM:SS o HH:MM:SS a segundos; None indica duración manual."""
    value = value.strip()
    if not value:
        return None

    parts = value.split(":")
    if len(parts) not in (2, 3) or any(not part.isdigit() for part in parts):
        raise ValueError("Usa el formato MM:SS o HH:MM:SS.")

    values = [int(part) for part in parts]
    if len(values) == 2:
        minutes, seconds = values
        if seconds > 59:
            raise ValueError("Los segundos deben estar entre 00 y 59.")
        duration = minutes * 60 + seconds
    else:
        hours, minutes, seconds = values
        if minutes > 59 or seconds > 59:
            raise ValueError("Los minutos y segundos deben estar entre 00 y 59.")
        duration = hours * 3600 + minutes * 60 + seconds

    if duration <= 0:
        raise ValueError("La duración debe ser mayor que cero.")
    return duration


class RecorderApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Grabador de ventana")
        screen_height = self.root.winfo_screenheight()
        window_height = min(760, screen_height - 40)
        self.root.geometry(f"480x{window_height}+0+0")
        self.root.resizable(False, False)

        self.windows = []
        self.recording = False
        self.paused = False
        self.record_thread = None
        self.audio_thread = None
        self.finalize_thread = None
        self._finalize_queue = None
        self._finalize_result = None
        self._finalize_had_audio = False
        self.output_path = None
        self._video_tmp = None
        self._audio_tmp = None
        self._recording_audio = False
        self._recording_format = FORMAT_OPTIONS[DEFAULT_FORMAT]
        self._capture_start_event = None
        self._audio_ready_event = None
        self._video_ready_event = None
        self._recording_hwnd = None
        self._recording_start_error = None
        self._recording_duration = None
        self._auto_stop_triggered = False

        self.custom_output_path = None  # ruta elegida por el usuario (o None = automática)
        self._last_save_dir = OUTPUT_DIR

        self._frame_lock = threading.Lock()
        self._latest_frame = None  # último frame BGR capturado (para el preview)
        self._preview_photo = None  # referencia viva para que Tkinter no la recolecte

        self.volume_endpoint = self._get_volume_endpoint()
        self.volume_available = self.volume_endpoint is not None

        # --- Cronómetro de grabación ---
        self._rec_start_ts = None  # time.monotonic() al iniciar la grabación actual
        self._paused_total = 0.0  # segundos acumulados en pausa (pausas ya cerradas)
        self._pause_start_ts = None  # time.monotonic() en que empezó la pausa actual

        self._build_ui()
        self.refresh_windows()
        self._show_placeholder_preview("Sin grabación en curso")
        self.root.after(PREVIEW_INTERVAL_MS, self._update_preview)

    def _build_ui(self):
        pad = {"padx": 8, "pady": 1}

        # --- Abrir video por URL (opcional, no maneja credenciales) ---
        open_frame = ttk.Frame(self.root)
        open_frame.pack(fill="x", **pad)

        ttk.Label(open_frame, text="Abrir video (URL, opcional):").pack(anchor="w")

        open_row = ttk.Frame(open_frame)
        open_row.pack(fill="x", pady=(4, 0))

        self.url_var = tk.StringVar()
        self.url_entry = ttk.Entry(open_row, textvariable=self.url_var)
        self.url_entry.pack(side="left", fill="x", expand=True)

        ttk.Button(open_row, text="Abrir", command=self.open_video_url).pack(
            side="left", padx=(6, 0)
        )

        ttk.Label(
            self.root,
            text=(
                "Abre el video en el navegador y selecciona su ventana."
            ),
            foreground="#888",
            wraplength=440,
            justify="left",
        ).pack(anchor="w", padx=8)

        top_frame = ttk.Frame(self.root)
        top_frame.pack(fill="x", **pad)

        ttk.Label(top_frame, text="Ventana a grabar:").pack(anchor="w")

        row = ttk.Frame(top_frame)
        row.pack(fill="x", pady=(4, 0))

        self.combo = ttk.Combobox(row, state="readonly", width=48)
        self.combo.pack(side="left", fill="x", expand=True)

        ttk.Button(row, text="Actualizar", command=self.refresh_windows).pack(
            side="left", padx=(6, 0)
        )

        options_frame = ttk.Frame(self.root)
        options_frame.pack(fill="x", padx=8)

        self.audio_var = tk.BooleanVar(value=HAS_AUDIO)
        audio_check = ttk.Checkbutton(
            options_frame,
            text="Grabar también el audio del sistema",
            variable=self.audio_var,
        )
        audio_check.pack(side="left", anchor="w")
        if not HAS_AUDIO:
            audio_check.config(state="disabled")
            self.audio_var.set(False)

        self.play_on_start_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            options_frame,
            text="Play al iniciar (video pausado)",
            variable=self.play_on_start_var,
        ).pack(side="left", anchor="w", padx=(8, 0))

        # --- Volumen del sistema (para no tener que salir de la app) ---
        volume_frame = ttk.Frame(self.root)
        volume_frame.pack(fill="x", **pad)

        ttk.Label(volume_frame, text="Volumen de la PC:").pack(anchor="w")

        volume_row = ttk.Frame(volume_frame)
        volume_row.pack(fill="x", pady=(4, 0))

        initial_percent = self._get_current_volume_percent()
        self.volume_var = tk.IntVar(value=initial_percent)

        self.volume_scale = ttk.Scale(
            volume_row,
            from_=0,
            to=100,
            orient="horizontal",
        )
        self.volume_scale.pack(side="left", fill="x", expand=True)

        self.volume_label = ttk.Label(volume_row, text=f"{initial_percent}%", width=5)
        self.volume_label.pack(side="left", padx=(6, 0))

        # El valor inicial se fija sin disparar el cambio de volumen real
        # (el callback recién se conecta después de posicionar la barra).
        self.volume_scale.set(initial_percent)
        self.volume_scale.config(command=self._on_volume_change)

        if not self.volume_available:
            self.volume_scale.config(state="disabled")

        # --- Formato de video de salida ---
        format_frame = ttk.Frame(self.root)
        format_frame.pack(fill="x", **pad)

        ttk.Label(format_frame, text="Formato de video:").pack(anchor="w")

        self.format_var = tk.StringVar(value=DEFAULT_FORMAT)
        self.format_combo = ttk.Combobox(
            format_frame,
            textvariable=self.format_var,
            state="readonly",
            values=list(FORMAT_OPTIONS.keys()),
        )
        self.format_combo.pack(fill="x", pady=(4, 0))
        self.format_combo.bind("<<ComboboxSelected>>", self._on_format_change)

        # --- Ubicación y nombre de guardado ---
        save_frame = ttk.Frame(self.root)
        save_frame.pack(fill="x", **pad)

        ttk.Label(save_frame, text="Guardar en:").pack(anchor="w")

        save_row = ttk.Frame(save_frame)
        save_row.pack(fill="x", pady=(4, 0))

        self.save_path_var = tk.StringVar(value="Automático (carpeta recordings/)")
        self.save_path_entry = ttk.Entry(
            save_row, textvariable=self.save_path_var, state="readonly"
        )
        self.save_path_entry.pack(side="left", fill="x", expand=True)

        self.save_btn = ttk.Button(
            save_row, text="Guardar como...", command=self.choose_save_location
        )
        self.save_btn.pack(side="left", padx=(6, 0))

        # --- Duración programada ---
        duration_frame = ttk.Frame(self.root)
        duration_frame.pack(fill="x", **pad)

        ttk.Label(
            duration_frame,
            text="Detener automáticamente (MM:SS o HH:MM:SS; vacío = manual):",
        ).pack(anchor="w")
        self.duration_var = tk.StringVar()
        self.duration_entry = ttk.Entry(
            duration_frame, textvariable=self.duration_var, width=14
        )
        self.duration_entry.pack(anchor="w", pady=(4, 0))

        # --- Cronómetro ---
        self.timer_var = tk.StringVar(value="00:00.000")
        tk.Label(
            self.root,
            textvariable=self.timer_var,
            font=("Consolas", 18, "bold"),
            fg="#1a4d7a",
        ).pack(pady=(2, 0))

        # --- Vista previa ---
        preview_frame = ttk.Frame(self.root)
        preview_frame.pack(padx=8, pady=(3, 1))

        self.preview_label = tk.Label(
            preview_frame,
            width=PREVIEW_SIZE[0],
            height=PREVIEW_SIZE[1],
            bg="black",
        )
        self.preview_label.pack()

        # --- Botones ---
        btn_frame = ttk.Frame(self.root)
        btn_frame.pack(fill="x", **pad)

        self.start_btn = ttk.Button(
            btn_frame, text="Iniciar grabación", command=self.start_recording
        )
        self.start_btn.pack(side="left", expand=True, fill="x", padx=(0, 4))

        self.pause_btn = ttk.Button(
            btn_frame, text="Pausar", command=self.toggle_pause, state="disabled"
        )
        self.pause_btn.pack(side="left", expand=True, fill="x", padx=4)

        self.stop_btn = ttk.Button(
            btn_frame, text="Detener", command=self.stop_recording, state="disabled"
        )
        self.stop_btn.pack(side="left", expand=True, fill="x", padx=4)

        self.play_btn = ttk.Button(
            btn_frame, text="Reproducir", command=self.play_recording, state="disabled"
        )
        self.play_btn.pack(side="left", expand=True, fill="x", padx=(4, 0))

        # --- Progreso de finalización / conversión ---
        progress_frame = ttk.Frame(self.root)
        progress_frame.pack(fill="x", padx=8, pady=(1, 1))

        self.progress_label_var = tk.StringVar(value="Conversión: 0%")
        ttk.Label(progress_frame, textvariable=self.progress_label_var).pack(anchor="w")
        self.progress_bar = ttk.Progressbar(
            progress_frame, mode="determinate", maximum=100, value=0
        )
        self.progress_bar.pack(fill="x")

        # --- Estado (texto grande para que se note bien) ---
        self.status_var = tk.StringVar(value="Listo.")
        tk.Label(
            self.root,
            textvariable=self.status_var,
            font=("Segoe UI", 10, "bold"),
            fg="#1a4d7a",
            wraplength=440,
            justify="left",
        ).pack(anchor="w", padx=10, pady=(4, 6))

        if not HAS_AUDIO:
            ttk.Label(
                self.root,
                text="(No se encontró PyAudioWPatch: instala las dependencias para grabar audio)",
                foreground="#a33",
                wraplength=480,
            ).pack(anchor="w", padx=10)

        if not self.volume_available:
            ttk.Label(
                self.root,
                text="(No se pudo acceder al volumen del sistema: instala pycaw/comtypes)",
                foreground="#a33",
                wraplength=480,
            ).pack(anchor="w", padx=10)

    # ------------------------------------------------------------------
    # Volumen del sistema
    # ------------------------------------------------------------------
    def _get_volume_endpoint(self):
        if not HAS_VOLUME_CONTROL:
            return None
        try:
            speakers = AudioUtilities.GetSpeakers()
            return speakers.EndpointVolume
        except Exception:
            return None

    def _get_current_volume_percent(self):
        if not getattr(self, "volume_available", False) or self.volume_endpoint is None:
            return 50
        try:
            scalar = self.volume_endpoint.GetMasterVolumeLevelScalar()
            return round(scalar * 100)
        except Exception:
            return 50

    def _on_volume_change(self, value):
        percent = round(float(value))
        self.volume_var.set(percent)
        self.volume_label.config(text=f"{percent}%")

        if self.volume_available and self.volume_endpoint is not None:
            try:
                self.volume_endpoint.SetMasterVolumeLevelScalar(percent / 100.0, None)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Vista previa
    # ------------------------------------------------------------------
    def _show_placeholder_preview(self, text):
        img = Image.new("RGB", PREVIEW_SIZE, color=(24, 24, 24))
        draw = ImageDraw.Draw(img)
        bbox = draw.textbbox((0, 0), text)
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]
        draw.text(
            ((PREVIEW_SIZE[0] - text_w) / 2, (PREVIEW_SIZE[1] - text_h) / 2),
            text,
            fill=(160, 160, 160),
        )
        self._preview_photo = ImageTk.PhotoImage(img)
        self.preview_label.config(image=self._preview_photo)

    def _update_preview(self):
        if self.recording:
            with self._frame_lock:
                frame = self._latest_frame

            if frame is not None:
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                img = Image.fromarray(rgb).resize(PREVIEW_SIZE, Image.LANCZOS)

                if self.paused:
                    draw = ImageDraw.Draw(img)
                    draw.rectangle([0, 0, PREVIEW_SIZE[0], 28], fill=(0, 0, 0))
                    draw.text((8, 6), "PAUSADO", fill=(255, 210, 0))

                self._preview_photo = ImageTk.PhotoImage(img)
                self.preview_label.config(image=self._preview_photo)

        self._update_timer_display()
        self.root.after(PREVIEW_INTERVAL_MS, self._update_preview)

    # ------------------------------------------------------------------
    # Cronómetro
    # ------------------------------------------------------------------
    def _get_elapsed_seconds(self):
        if self._rec_start_ts is None:
            return 0.0
        now = time.monotonic()
        paused = self._paused_total
        if self.paused and self._pause_start_ts is not None:
            paused += now - self._pause_start_ts
        return max(0.0, now - self._rec_start_ts - paused)

    @staticmethod
    def _format_elapsed(seconds):
        total_ms = int(seconds * 1000)
        hours, rem_ms = divmod(total_ms, 3_600_000)
        minutes, rem_ms = divmod(rem_ms, 60_000)
        secs, ms = divmod(rem_ms, 1000)
        if hours > 0:
            return f"{hours:02d}:{minutes:02d}:{secs:02d}.{ms:03d}"
        return f"{minutes:02d}:{secs:02d}.{ms:03d}"

    def _update_timer_display(self):
        if self.recording:
            elapsed = self._get_elapsed_seconds()
            self.timer_var.set(self._format_elapsed(elapsed))
            if (
                self._recording_duration is not None
                and elapsed >= self._recording_duration
            ):
                self._auto_stop_triggered = True
                self.stop_recording()

    # ------------------------------------------------------------------
    # Abrir video por URL
    # ------------------------------------------------------------------
    def open_video_url(self):
        url = self.url_var.get().strip()
        if not url:
            messagebox.showwarning("Aviso", "Pega primero el link del video.")
            return
        if not (url.startswith("http://") or url.startswith("https://")):
            messagebox.showwarning(
                "Aviso", "El link debe comenzar con http:// o https://"
            )
            return

        webbrowser.open(url)
        self.status_var.set(
            "Abriendo el video en tu navegador... inicia sesión si te lo pide, "
            "déjalo pausado y elige esa ventana en la lista."
        )
        # Le damos tiempo al navegador a abrir la ventana antes de refrescar
        # la lista, así aparece disponible para seleccionarla.
        self.root.after(2500, self.refresh_windows)

    # ------------------------------------------------------------------
    # Formato de video
    # ------------------------------------------------------------------
    def get_selected_format(self):
        return FORMAT_OPTIONS.get(self.format_var.get(), FORMAT_OPTIONS[DEFAULT_FORMAT])

    def _on_format_change(self, event=None):
        # Si ya se había elegido una ubicación, actualizamos su extensión
        # para que siempre coincida con el formato seleccionado.
        if self.custom_output_path:
            fmt = self.get_selected_format()
            base, _ext = os.path.splitext(self.custom_output_path)
            self.custom_output_path = base + fmt["ext"]
            self.save_path_var.set(self.custom_output_path)

    # ------------------------------------------------------------------
    # Ubicación de guardado
    # ------------------------------------------------------------------
    def choose_save_location(self):
        fmt = self.get_selected_format()
        ext = fmt["ext"]
        default_name = (
            f"recording_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}{ext}"
        )
        path = filedialog.asksaveasfilename(
            title="Guardar grabación como",
            initialdir=self._last_save_dir,
            initialfile=default_name,
            defaultextension=ext,
            filetypes=[(f"Video {ext[1:].upper()}", f"*{ext}")],
        )
        if not path:
            return

        # Sin importar lo que el usuario haya escrito, la extensión
        # siempre queda forzada al formato elegido en el combo.
        base, _ext = os.path.splitext(path)
        path = base + ext

        self.custom_output_path = path
        self._last_save_dir = os.path.dirname(path)
        self.save_path_var.set(path)

    # ------------------------------------------------------------------
    # Ventanas
    # ------------------------------------------------------------------
    def refresh_windows(self):
        all_windows = gw.getAllWindows()
        self.windows = [
            w for w in all_windows if w.title.strip() and w.width > 0 and w.height > 0
        ]
        titles = [w.title for w in self.windows]
        self.combo["values"] = titles
        if titles:
            self.combo.current(0)
        else:
            self.combo.set("")
        self.status_var.set(f"{len(titles)} ventana(s) encontradas.")

    # ------------------------------------------------------------------
    # Grabación
    # ------------------------------------------------------------------
    def start_recording(self):
        try:
            recording_duration = parse_duration(self.duration_var.get())
        except ValueError as exc:
            messagebox.showwarning("Duración no válida", str(exc))
            return

        idx = self.combo.current()
        if idx < 0 or idx >= len(self.windows):
            messagebox.showwarning("Aviso", "Selecciona una ventana primero.")
            return

        win = self.windows[idx]

        try:
            if win.isMinimized:
                win.restore()
            win.activate()
        except Exception:
            pass

        time.sleep(0.3)

        try:
            left, top, right, bottom = win32gui.GetWindowRect(win._hWnd)
        except Exception as exc:
            messagebox.showerror("Error", f"No se pudo leer la ventana: {exc}")
            return

        width = right - left
        height = bottom - top
        if width <= 0 or height <= 0:
            messagebox.showerror("Error", "La ventana seleccionada no es válida.")
            return

        # libx264/libvpx-vp9 con yuv420p requieren ancho y alto pares.
        width -= width % 2
        height -= height % 2
        if width <= 0 or height <= 0:
            messagebox.showerror("Error", "La ventana seleccionada no es válida.")
            return

        os.makedirs(OUTPUT_DIR, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

        self._recording_format = self.get_selected_format()
        ext = self._recording_format["ext"]

        if self.custom_output_path:
            self.output_path = self.custom_output_path
            os.makedirs(os.path.dirname(self.output_path), exist_ok=True)
        else:
            self.output_path = os.path.join(OUTPUT_DIR, f"recording_{stamp}{ext}")

        want_audio = self.audio_var.get() and HAS_AUDIO
        self._recording_audio = False

        # El video temporal ya queda codificado EN VIVO con el códec final
        # del formato elegido (por eso comparte su extensión); al detener
        # solo falta mezclarle el audio (si lo hay), no volver a codificarlo.
        self._video_tmp = os.path.join(OUTPUT_DIR, f".tmp_video_{stamp}{ext}")
        self._audio_tmp = (
            os.path.join(OUTPUT_DIR, f".tmp_audio_{stamp}.wav") if want_audio else None
        )

        self.paused = False
        self.recording = True
        self._recording_duration = recording_duration
        self._auto_stop_triggered = False
        self._capture_start_event = threading.Event()
        self._audio_ready_event = threading.Event()
        self._video_ready_event = threading.Event()
        self._recording_hwnd = win._hWnd
        self._recording_start_error = None
        self.progress_bar.config(value=0)
        self.progress_label_var.set("Conversión: 0%")
        if not want_audio:
            self._audio_ready_event.set()

        self._rec_start_ts = None
        self._paused_total = 0.0
        self._pause_start_ts = None
        self.timer_var.set("00:00.000")

        self.record_thread = threading.Thread(
            target=self._record_video_loop,
            args=(
                left,
                top,
                width,
                height,
                self._video_tmp,
                self._recording_format,
                self._capture_start_event,
                self._video_ready_event,
            ),
            daemon=True,
        )
        self.record_thread.start()

        if want_audio:
            self.audio_thread = threading.Thread(
                target=self._record_audio_loop,
                args=(
                    self._audio_tmp,
                    self._capture_start_event,
                    self._audio_ready_event,
                ),
                daemon=True,
            )
            self.audio_thread.start()
        else:
            self.audio_thread = None
            if self.audio_var.get() and not HAS_AUDIO:
                self.status_var.set(
                    "Aviso: no se pudo activar audio (falta PyAudioWPatch). Grabando solo video..."
                )

        self.start_btn.config(state="disabled")
        self.pause_btn.config(state="normal", text="Pausar")
        self.stop_btn.config(state="normal")
        self.play_btn.config(state="disabled")
        self.combo.config(state="disabled")
        self.save_btn.config(state="disabled")
        self.format_combo.config(state="disabled")
        self.duration_entry.config(state="disabled")

        # Ya se fijó el destino de esta grabación; dejamos el campo listo
        # para que la próxima vuelva a ser "automática" salvo que el
        # usuario elija otra vez una ubicación.
        self.custom_output_path = None
        self.save_path_var.set("Automático (carpeta recordings/)")

        if not (self.audio_var.get() and not HAS_AUDIO):
            self.status_var.set(
                f"Grabando '{win.title}'... no la cubras con otra ventana."
            )
        self.root.after(25, self._begin_capture_when_ready)

    def _begin_capture_when_ready(self):
        if not self.recording:
            return
        if not (
            self._audio_ready_event.is_set()
            and self._video_ready_event.is_set()
        ):
            self.root.after(25, self._begin_capture_when_ready)
            return

        if self._recording_start_error:
            self.status_var.set(self._recording_start_error)
            self.stop_recording()
            return

        key_pressed = False
        try:
            if self.play_on_start_var.get():
                win32gui.SetForegroundWindow(self._recording_hwnd)
                if win32gui.GetForegroundWindow() != self._recording_hwnd:
                    raise OSError("Windows no permitió activar la ventana seleccionada.")
                win32api.keybd_event(win32con.VK_SPACE, 0, 0, 0)
                key_pressed = True
                self._rec_start_ts = time.monotonic()
                self._capture_start_event.set()
                time.sleep(0.05)
                win32api.keybd_event(
                    win32con.VK_SPACE, 0, win32con.KEYEVENTF_KEYUP, 0
                )
                key_pressed = False
        except Exception as exc:
            self.status_var.set(
                f"No se pudo enviar ESPACIO al reproductor: {exc}. "
                "Inicia el video manualmente."
            )
        finally:
            if key_pressed:
                try:
                    win32api.keybd_event(
                        win32con.VK_SPACE, 0, win32con.KEYEVENTF_KEYUP, 0
                    )
                except Exception as exc:
                    self.status_var.set(f"No se pudo liberar la tecla ESPACIO: {exc}")
            if not self._capture_start_event.is_set():
                self._rec_start_ts = time.monotonic()
                self._capture_start_event.set()

    def toggle_pause(self):
        if not self.recording:
            return
        self.paused = not self.paused
        if self.paused:
            self._pause_start_ts = time.monotonic()
            self.pause_btn.config(text="Reanudar")
            self.status_var.set("Grabación en pausa.")
        else:
            if self._pause_start_ts is not None:
                self._paused_total += time.monotonic() - self._pause_start_ts
                self._pause_start_ts = None
            self.pause_btn.config(text="Pausar")
            self.status_var.set("Grabando...")

    def _record_video_loop(
        self, left, top, width, height, filename, fmt, start_event, ready_event
    ):
        """Captura la ventana y la codifica EN VIVO (cuadro a cuadro) con
        ffmpeg vía pipe, ya en el códec final del formato elegido. Así, al
        pulsar Detener, el video ya está terminado (no hace falta esperar
        una recompresión posterior)."""
        try:
            ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
            cmd = [
                ffmpeg_exe, "-y",
                "-f", "rawvideo",
                "-pixel_format", "bgr24",
                "-video_size", f"{width}x{height}",
                "-framerate", str(FPS),
                "-i", "pipe:0",
            ] + fmt["video_args_live"] + [filename]

            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as exc:
            self._recording_start_error = f"No se pudo iniciar la captura de video: {exc}"
            ready_event.set()
            return
        ready_event.set()

        monitor = {"left": left, "top": top, "width": width, "height": height}
        frame_interval = 1.0 / FPS
        while self.recording and not start_event.wait(0.1):
            pass
        if not self.recording:
            proc.stdin.close()
            proc.wait()
            return

        start_time = time.monotonic()
        paused_total = 0.0
        pause_start = None
        frames_written = 0

        try:
            with mss.mss() as sct:
                while self.recording:
                    if self.paused:
                        if pause_start is None:
                            pause_start = time.monotonic()
                        time.sleep(0.1)
                        continue

                    now = time.monotonic()
                    if pause_start is not None:
                        paused_total += now - pause_start
                        pause_start = None

                    img = np.array(sct.grab(monitor))
                    frame = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
                    frame_bytes = frame.tobytes()

                    with self._frame_lock:
                        self._latest_frame = frame

                    elapsed = time.monotonic() - start_time - paused_total
                    target_frames = max(1, math.ceil(elapsed * FPS))
                    frames_to_write = max(1, target_frames - frames_written)
                    try:
                        for _ in range(frames_to_write):
                            proc.stdin.write(frame_bytes)
                            frames_written += 1
                    except (BrokenPipeError, OSError):
                        break

                    next_time = start_time + paused_total + frames_written * frame_interval
                    sleep_time = next_time - time.monotonic()
                    if sleep_time > 0:
                        time.sleep(sleep_time)
        finally:
            try:
                proc.stdin.close()
            except Exception:
                pass
            try:
                proc.wait(timeout=30)
            except Exception:
                proc.kill()

    def _record_audio_loop(self, audio_path, start_event, ready_event):
        p = None
        stream = None
        try:
            p = pyaudio.PyAudio()
            device = get_loopback_device(p)
            if device is None:
                return

            channels = max(1, int(device["maxInputChannels"]))
            rate = int(device["defaultSampleRate"])

            stream = p.open(
                format=pyaudio.paInt16,
                channels=channels,
                rate=rate,
                input=True,
                input_device_index=device["index"],
                frames_per_buffer=AUDIO_CHUNK,
            )

            ready_event.set()
            while self.recording and not start_event.wait(0.1):
                pass
            if not self.recording:
                return

            self._recording_audio = True
            frames = []
            while self.recording:
                try:
                    data = stream.read(AUDIO_CHUNK, exception_on_overflow=False)
                except Exception:
                    break
                # Mientras está en pausa seguimos leyendo el stream (para que no
                # se desborde el buffer) pero descartamos el audio capturado.
                if not self.paused:
                    frames.append(data)

            with wave.open(audio_path, "wb") as wf:
                wf.setnchannels(channels)
                wf.setsampwidth(p.get_sample_size(pyaudio.paInt16))
                wf.setframerate(rate)
                wf.writeframes(b"".join(frames))
        finally:
            ready_event.set()
            if stream is not None:
                stream.stop_stream()
                stream.close()
            if p is not None:
                p.terminate()

    def stop_recording(self):
        if not self.recording:
            return
        self.recording = False
        self.paused = False

        self.start_btn.config(state="disabled")
        self.pause_btn.config(state="disabled", text="Pausar")
        self.stop_btn.config(state="disabled")
        self.play_btn.config(state="disabled")
        self.combo.config(state="disabled")
        self.save_btn.config(state="disabled")
        self.format_combo.config(state="disabled")
        self.duration_entry.config(state="disabled")
        self.progress_bar.config(value=0)
        self.progress_label_var.set("Finalizando grabación: 0%")
        self.status_var.set("Finalizando grabación en segundo plano...")

        self._finalize_queue = queue.Queue()
        self._finalize_result = None
        self._finalize_had_audio = False
        self.finalize_thread = threading.Thread(
            target=self._finalize_recording_worker,
            daemon=True,
        )
        self.finalize_thread.start()
        self.root.after(100, self._poll_finalization)

    def _finalize_recording_worker(self):
        try:
            if self.record_thread:
                self.record_thread.join()
            if self.audio_thread:
                self.audio_thread.join()

            if self._recording_start_error:
                self._finalize_result = self._recording_start_error
                return

            has_audio_file = (
                self._audio_tmp
                and os.path.exists(self._audio_tmp)
                and os.path.getsize(self._audio_tmp) > 44
                and self._recording_audio
            )
            self._finalize_had_audio = bool(has_audio_file)

            if has_audio_file:
                self._finalize_queue.put(("status", "Combinando audio y video..."))
                mux_succeeded = self._mux_audio(
                    self._video_tmp,
                    self._audio_tmp,
                    self.output_path,
                    self._recording_format,
                    self._finalize_queue,
                )
                if mux_succeeded:
                    self._cleanup_temp_files()
                else:
                    self.output_path = self._video_tmp
                    self._finalize_result = (
                        "No se pudo combinar el audio; se guardó el video sin audio."
                    )
                    return
            else:
                try:
                    if self._video_tmp and self._video_tmp != self.output_path:
                        os.replace(self._video_tmp, self.output_path)
                except OSError as exc:
                    self._finalize_result = f"No se pudo guardar el archivo final: {exc}"
                    return
                self._cleanup_temp_files()

            if self.output_path and os.path.exists(self.output_path):
                size_mb = os.path.getsize(self.output_path) / (1024 * 1024)
                self._finalize_result = (
                    f"Grabación guardada en: {self.output_path} ({size_mb:.1f} MB)"
                )
                if self._auto_stop_triggered:
                    self._finalize_result += " — duración programada alcanzada."
            else:
                self._finalize_result = "La grabación no se guardó correctamente."
        except Exception as exc:
            self._finalize_result = f"Error al finalizar la grabación: {exc}"

    def _poll_finalization(self):
        if self._finalize_queue is not None:
            while True:
                try:
                    event, value = self._finalize_queue.get_nowait()
                except queue.Empty:
                    break

                if event == "progress":
                    percent = max(0, min(99, int(value)))
                    self.progress_bar.config(value=percent)
                    self.progress_label_var.set(f"Conversión: {percent}%")
                elif event == "status":
                    self.status_var.set(value)

        if self.finalize_thread and self.finalize_thread.is_alive():
            self.root.after(100, self._poll_finalization)
            return

        self.start_btn.config(state="normal")
        self.combo.config(state="readonly")
        self.save_btn.config(state="normal")
        self.format_combo.config(state="readonly")
        self.duration_entry.config(state="normal")
        self._show_placeholder_preview("Sin grabación en curso")
        self.play_btn.config(
            state="normal"
            if self.output_path and os.path.exists(self.output_path)
            else "disabled"
        )

        if (
            self._finalize_result
            and self._finalize_result.startswith("Grabación guardada")
            and self._finalize_had_audio
        ):
            self.progress_bar.config(value=100)
            self.progress_label_var.set("Conversión: 100%")
        elif not self._finalize_had_audio:
            self.progress_label_var.set("Conversión de audio no requerida")
        elif self._finalize_result and not self._finalize_result.startswith(
            "Grabación guardada"
        ):
            self.progress_label_var.set("Conversión incompleta")
        self.status_var.set(self._finalize_result or "La grabación finalizó.")

    def _mux_audio(self, video_path, audio_path, out_path, fmt, progress_queue):
        """Añade la pista de audio al video ya codificado (copia el video
        tal cual) y corrige la pequeña deriva entre los relojes de captura."""
        try:
            with wave.open(audio_path, "rb") as audio_file:
                audio_duration = audio_file.getnframes() / audio_file.getframerate()

            video = cv2.VideoCapture(video_path)
            try:
                fps = video.get(cv2.CAP_PROP_FPS)
                frame_count = video.get(cv2.CAP_PROP_FRAME_COUNT)
            finally:
                video.release()
            if fps <= 0 or frame_count <= 0 or audio_duration <= 0:
                return False

            video_duration = frame_count / fps
            duration_delta = audio_duration - video_duration
            audio_tempo = (
                audio_duration / video_duration
                if abs(duration_delta) > 0.02
                else 1.0
            )
            audio_filter = (
                f"atempo={audio_tempo:.12f},"
                f"atrim=duration={video_duration:.6f},"
                "asetpts=PTS-STARTPTS"
            )

            ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
            cmd = [
                ffmpeg_exe, "-y",
                "-i", video_path,
                "-i", audio_path,
                "-c:v", "copy",
            ] + fmt["audio_args"] + [
                "-af", audio_filter,
                "-progress", "pipe:1",
                "-nostats",
                out_path,
            ]

            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
            if proc.stdout is None:
                proc.kill()
                return False

            for line in proc.stdout:
                key, separator, value = line.strip().partition("=")
                if separator and key == "out_time_ms" and video_duration > 0:
                    elapsed = int(value) / 1_000_000
                    percent = min(99, int(elapsed * 100 / video_duration))
                    progress_queue.put(("progress", percent))

            proc.wait()
            return proc.returncode == 0 and os.path.exists(out_path)
        except (OSError, ValueError, ZeroDivisionError, wave.Error):
            return False

    def _cleanup_temp_files(self):
        for path in (self._video_tmp, self._audio_tmp):
            if path and path != self.output_path and os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass

    def play_recording(self):
        if self.output_path and os.path.exists(self.output_path):
            os.startfile(self.output_path)
        else:
            messagebox.showwarning("Aviso", "No hay ninguna grabación para reproducir.")


def main():
    root = tk.Tk()
    RecorderApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
