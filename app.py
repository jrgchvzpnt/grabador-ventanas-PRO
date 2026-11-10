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
import webbrowser

from audio import HAS_AUDIO, record_system_audio
from config import (
    AUDIO_CHUNK,
    DEFAULT_FORMAT,
    DEFAULT_QUALITY,
    FORMAT_OPTIONS,
    FPS,
    PREVIEW_INTERVAL_MS,
    PREVIEW_SIZE,
    QUALITY_LEVELS,
    QUALITY_PROFILES,
)
from drive import (
    DriveImportError,
    download_drive_video,
    get_video_duration,
    list_drive_videos,
)
from media import mux_audio


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
    from pycaw.pycaw import AudioUtilities

    HAS_VOLUME_CONTROL = True
except ImportError:
    HAS_VOLUME_CONTROL = False

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "recordings")


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
        window_height = min(850, screen_height - 40)
        screen_width = self.root.winfo_screenwidth()
        window_width = min(500, screen_width - 40)
        x = max(0, (screen_width - window_width) // 2)
        y = max(0, (screen_height - window_height) // 2)
        self.root.geometry(f"{window_width}x{window_height}+{x}+{y}")
        self.root.resizable(False, True)
        self._configure_styles()

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

    def _configure_styles(self):
        style = ttk.Style(self.root)
        if "clam" in style.theme_names():
            style.theme_use("clam")

        self.root.configure(bg="#f3f5f8")
        style.configure("App.TFrame", background="#f3f5f8")
        style.configure("Card.TLabelframe", background="#ffffff", bordercolor="#dce2ea")
        style.configure(
            "Card.TLabelframe.Label",
            background="#ffffff",
            foreground="#526174",
            font=("Segoe UI", 9, "bold"),
        )
        style.configure("Card.TFrame", background="#ffffff")
        style.configure(
            "AppTitle.TLabel",
            background="#f3f5f8",
            foreground="#17263c",
            font=("Segoe UI", 15, "bold"),
        )
        style.configure(
            "Subtitle.TLabel",
            background="#f3f5f8",
            foreground="#64748b",
            font=("Segoe UI", 8),
        )
        style.configure(
            "Field.TLabel",
            background="#ffffff",
            foreground="#344256",
            font=("Segoe UI", 9, "bold"),
        )
        style.configure(
            "Hint.TLabel",
            background="#ffffff",
            foreground="#738196",
            font=("Segoe UI", 8),
        )
        style.configure(
            "Timer.TLabel",
            background="#17263c",
            foreground="#f8fafc",
            font=("Consolas", 18, "bold"),
        )
        style.configure(
            "TimerState.TLabel",
            background="#17263c",
            foreground="#aab8ca",
            font=("Segoe UI", 8, "bold"),
        )
        style.configure(
            "Status.TLabel",
            background="#f3f5f8",
            foreground="#526174",
            font=("Segoe UI", 9),
        )
        style.configure(
            "Primary.TButton",
            background="#1769aa",
            foreground="#ffffff",
            font=("Segoe UI", 9, "bold"),
            padding=(8, 4),
        )
        style.map(
            "Primary.TButton",
            background=[("active", "#12578f"), ("disabled", "#a9bdcf")],
            foreground=[("disabled", "#f4f7fa")],
        )
        style.configure(
            "Danger.TButton",
            background="#bd3d45",
            foreground="#ffffff",
            font=("Segoe UI", 9, "bold"),
            padding=(8, 4),
        )
        style.map(
            "Danger.TButton",
            background=[("active", "#a52c35"), ("disabled", "#d9b2b5")],
            foreground=[("disabled", "#f8f3f3")],
        )
        style.configure("TEntry", padding=(5, 3))
        style.configure("TCombobox", padding=(4, 3))
        style.configure(
            "TButton", font=("Segoe UI", 8), padding=(5, 2)
        )
        style.configure(
            "TCheckbutton",
            background="#ffffff",
            foreground="#344256",
            font=("Segoe UI", 8),
        )
        style.map("TCheckbutton", background=[("active", "#ffffff")])
        style.configure(
            "TProgressbar",
            troughcolor="#e5eaf0",
            background="#2187c9",
            bordercolor="#e5eaf0",
            lightcolor="#2187c9",
            darkcolor="#2187c9",
        )

    def _build_ui(self):
        content = ttk.Frame(self.root, style="App.TFrame", padding=(12, 7, 12, 7))
        content.pack(fill="both", expand=True)

        ttk.Label(content, text="Grabador de ventanas", style="AppTitle.TLabel").pack(
            anchor="w"
        )
        source_card = ttk.LabelFrame(
            content, text="  FUENTE DE VIDEO  ", style="Card.TLabelframe", padding=7
        )
        source_card.pack(fill="x", pady=(0, 5))

        open_row = ttk.Frame(source_card, style="Card.TFrame")
        open_row.pack(fill="x", pady=(0, 4))
        ttk.Label(
            open_row, text="URL Drive", style="Field.TLabel", width=9
        ).pack(side="left")
        self.url_var = tk.StringVar()
        self.url_entry = ttk.Entry(open_row, textvariable=self.url_var)
        self.url_entry.pack(side="left", fill="x", expand=True)
        ttk.Button(open_row, text="Abrir", command=self.open_video_url).pack(
            side="left", padx=(7, 0)
        )
        ttk.Button(
            open_row, text="Importar MP4", command=self.open_drive_importer
        ).pack(side="left", padx=(5, 0))
        window_row = ttk.Frame(source_card, style="Card.TFrame")
        window_row.pack(fill="x")
        ttk.Label(window_row, text="Ventana", style="Field.TLabel", width=8).pack(
            side="left"
        )
        self.combo = ttk.Combobox(window_row, state="readonly", width=30)
        self.combo.pack(side="left", fill="x", expand=True)
        ttk.Button(window_row, text="Actualizar", command=self.refresh_windows).pack(
            side="left", padx=(7, 0)
        )

        settings_card = ttk.LabelFrame(
            content, text="  AJUSTES  ", style="Card.TLabelframe", padding=7
        )
        settings_card.pack(fill="x", pady=(0, 5))

        options_frame = ttk.Frame(settings_card, style="Card.TFrame")
        options_frame.pack(fill="x", pady=(0, 2))
        self.audio_var = tk.BooleanVar(value=HAS_AUDIO)
        audio_check = ttk.Checkbutton(
            options_frame,
            text="Audio del sistema",
            variable=self.audio_var,
        )
        audio_check.pack(side="left", anchor="w")
        if not HAS_AUDIO:
            audio_check.config(state="disabled")
            self.audio_var.set(False)

        self.play_on_start_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            options_frame,
            text="Sincronizar Play/Pausa del video",
            variable=self.play_on_start_var,
        ).pack(side="left", anchor="w", padx=(12, 0))

        volume_row = ttk.Frame(settings_card, style="Card.TFrame")
        volume_row.pack(fill="x", pady=(1, 3))
        ttk.Label(volume_row, text="Volumen", style="Field.TLabel").pack(
            side="left", padx=(0, 8)
        )
        initial_percent = self._get_current_volume_percent()
        self.volume_var = tk.IntVar(value=initial_percent)
        self.volume_scale = ttk.Scale(
            volume_row, from_=0, to=100, orient="horizontal"
        )
        self.volume_scale.pack(side="left", fill="x", expand=True)
        self.volume_label = ttk.Label(
            volume_row, text=f"{initial_percent}%", width=5, style="Field.TLabel"
        )
        self.volume_label.pack(side="left", padx=(8, 0))
        self.volume_scale.set(initial_percent)
        self.volume_scale.config(command=self._on_volume_change)
        if not self.volume_available:
            self.volume_scale.config(state="disabled")

        output_row = ttk.Frame(settings_card, style="Card.TFrame")
        output_row.pack(fill="x", pady=(0, 3))
        format_column = ttk.Frame(output_row, style="Card.TFrame")
        format_column.pack(side="left", fill="x", expand=True, padx=(0, 8))
        ttk.Label(format_column, text="Formato", style="Field.TLabel").pack(anchor="w")
        self.format_var = tk.StringVar(value=DEFAULT_FORMAT)
        self.format_combo = ttk.Combobox(
            format_column,
            textvariable=self.format_var,
            state="readonly",
            values=list(FORMAT_OPTIONS.keys()),
        )
        self.format_combo.pack(fill="x", pady=(3, 0))
        self.format_combo.bind("<<ComboboxSelected>>", self._on_format_change)

        quality_column = ttk.Frame(output_row, style="Card.TFrame")
        quality_column.pack(side="left", fill="x", expand=True)
        ttk.Label(
            quality_column, text="Calidad", style="Field.TLabel"
        ).pack(anchor="w")
        self.quality_var = tk.StringVar(value=DEFAULT_QUALITY)
        self.quality_combo = ttk.Combobox(
            quality_column,
            textvariable=self.quality_var,
            state="readonly",
            values=QUALITY_LEVELS,
        )
        self.quality_combo.pack(fill="x", pady=(3, 0))
        self.quality_combo.bind(
            "<<ComboboxSelected>>", self._on_quality_change
        )

        duration_row = ttk.Frame(settings_card, style="Card.TFrame")
        duration_row.pack(fill="x", pady=(0, 3))
        duration_column = ttk.Frame(duration_row, style="Card.TFrame")
        duration_column.pack(side="left")
        ttk.Label(
            duration_column, text="Duración máx. (HH:MM:SS)", style="Field.TLabel"
        ).pack(anchor="w")
        self.duration_var = tk.StringVar()
        duration_picker = ttk.Frame(duration_column, style="Card.TFrame")
        duration_picker.pack(anchor="w", pady=(2, 0))
        self.duration_hours_var = tk.StringVar(value="00")
        self.duration_minutes_var = tk.StringVar(value="00")
        self.duration_seconds_var = tk.StringVar(value="00")
        self.duration_entry = []
        for label, variable, maximum in (
            ("HH", self.duration_hours_var, 99),
            ("MM", self.duration_minutes_var, 59),
            ("SS", self.duration_seconds_var, 59),
        ):
            spinbox = ttk.Spinbox(
                duration_picker,
                from_=0,
                to=maximum,
                increment=1,
                wrap=True,
                format="%02.0f",
                textvariable=variable,
                width=3,
                justify="center",
                state="normal",
            )
            spinbox.pack(side="left")
            ttk.Label(
                duration_picker, text=label, style="Hint.TLabel"
            ).pack(side="left", padx=(1, 3))
            self.duration_entry.append(spinbox)
            variable.trace_add("write", self._sync_duration_value)

        self.quality_estimate_var = tk.StringVar()
        ttk.Label(
            duration_row,
            textvariable=self.quality_estimate_var,
            style="Hint.TLabel",
        ).pack(side="left", padx=(12, 0), pady=(17, 0))
        self._update_quality_estimate()

        save_row = ttk.Frame(settings_card, style="Card.TFrame")
        save_row.pack(fill="x")
        self.save_path_var = tk.StringVar(value="Automático (carpeta recordings/)")
        self.save_path_entry = ttk.Entry(
            save_row, textvariable=self.save_path_var, state="readonly"
        )
        self.save_path_entry.pack(side="left", fill="x", expand=True)
        self.save_btn = ttk.Button(
            save_row, text="Guardar como...", command=self.choose_save_location
        )
        self.save_btn.pack(side="left", padx=(7, 0))
        preview_card = ttk.LabelFrame(
            content, text="  VISTA PREVIA  ", style="Card.TLabelframe", padding=2
        )
        preview_card.pack(fill="x", pady=(0, 5))
        monitor_row = ttk.Frame(preview_card, style="Card.TFrame")
        monitor_row.pack(fill="x")

        self.preview_label = tk.Label(
            monitor_row,
            width=PREVIEW_SIZE[0],
            height=PREVIEW_SIZE[1],
            bg="#111827",
            highlightthickness=1,
            highlightbackground="#26364a",
            bd=0,
        )
        self.preview_label.pack(side="left", padx=(0, 8))

        capture_controls = ttk.Frame(monitor_row, style="Card.TFrame")
        capture_controls.pack(side="left", fill="both", expand=True)
        self.timer_var = tk.StringVar(value="00:00.000")
        timer_panel = tk.Frame(capture_controls, bg="#17263c", padx=10, pady=6)
        timer_panel.pack(fill="x", pady=(0, 5))
        timer_header = tk.Frame(timer_panel, bg="#17263c")
        timer_header.pack(fill="x")
        tk.Label(
            timer_header,
            text="TIEMPO GRABADO",
            bg="#17263c",
            fg="#9fb0c5",
            font=("Segoe UI", 8, "bold"),
        ).pack(side="left")
        self.timer_state_var = tk.StringVar(value="LISTO")
        self.timer_state_label = tk.Label(
            timer_header,
            textvariable=self.timer_state_var,
            bg="#17263c",
            fg="#aab8ca",
            font=("Segoe UI", 8, "bold"),
        )
        self.timer_state_label.pack(side="right")
        ttk.Label(
            timer_panel, textvariable=self.timer_var, style="Timer.TLabel"
        ).pack(anchor="center", pady=(4, 1))

        self.start_btn = ttk.Button(
            capture_controls,
            text="●  Iniciar grabación",
            command=self.start_recording,
            style="Primary.TButton",
        )
        self.start_btn.pack(fill="x", pady=(0, 3))

        btn_frame = ttk.Frame(capture_controls, style="Card.TFrame")
        btn_frame.pack(fill="x", pady=(0, 3))
        self.pause_btn = ttk.Button(
            btn_frame, text="Pausar", command=self.toggle_pause, state="disabled"
        )
        self.pause_btn.pack(side="left", expand=True, fill="x", padx=(0, 4))
        self.stop_btn = ttk.Button(
            btn_frame,
            text="Detener",
            command=self.stop_recording,
            state="disabled",
            style="Danger.TButton",
        )
        self.stop_btn.pack(side="left", expand=True, fill="x", padx=4)
        self.play_btn = ttk.Button(
            capture_controls,
            text="Reproducir",
            command=self.play_recording,
            state="disabled",
        )
        self.play_btn.pack(fill="x", pady=(0, 3))

        progress_frame = ttk.Frame(capture_controls, style="Card.TFrame")
        progress_frame.pack(fill="x")
        self.progress_label_var = tk.StringVar(value="Conversión: 0%")
        ttk.Label(
            progress_frame,
            textvariable=self.progress_label_var,
            style="Hint.TLabel",
        ).pack(anchor="w")
        self.progress_bar = ttk.Progressbar(
            progress_frame, mode="determinate", maximum=100, value=0
        )
        self.progress_bar.pack(fill="x", pady=(2, 0))

        self.status_var = tk.StringVar(value="Listo.")
        ttk.Label(
            content,
            textvariable=self.status_var,
            style="Status.TLabel",
            wraplength=510,
            justify="left",
        ).pack(anchor="w", pady=(1, 0))

        if not HAS_AUDIO:
            ttk.Label(
                content,
                text="No se encontró PyAudioWPatch: la grabación no incluirá audio.",
                foreground="#a33",
                style="Subtitle.TLabel",
                wraplength=510,
            ).pack(anchor="w", pady=(3, 0))
        if not self.volume_available:
            ttk.Label(
                content,
                text="No se pudo acceder al volumen del sistema (pycaw/comtypes).",
                foreground="#a33",
                style="Subtitle.TLabel",
                wraplength=510,
            ).pack(anchor="w", pady=(3, 0))

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

    def open_drive_importer(self):
        window = tk.Toplevel(self.root)
        window.title("Descargar videos de Google Drive")
        window.geometry("620x460")
        window.minsize(520, 380)
        window.transient(self.root)

        container = ttk.Frame(window, padding=12)
        container.pack(fill="both", expand=True)
        ttk.Label(
            container,
            text=(
                "Pega el URL de la carpeta de Drive en la ventana principal. "
                "Si Drive pide iniciar sesión, se abrirá el navegador para que "
                "lo hagas. La descarga requiere que la carpeta esté compartida "
                "como “Cualquier persona con el enlace”; la aplicación no usa "
                "la sesión ni las cookies del navegador. Los MP4 se descargan "
                "en el orden de Drive, conservando nombres y subcarpetas."
            ),
            wraplength=580,
            justify="left",
        ).pack(anchor="w", pady=(0, 8))

        ttk.Label(container, text="URL de carpeta de Google Drive").pack(anchor="w")
        url_var = self.url_var
        url_entry = ttk.Entry(container, textvariable=url_var)
        url_entry.pack(fill="x", pady=(3, 8))

        destination_row = ttk.Frame(container)
        destination_row.pack(fill="x", pady=(0, 8))
        ttk.Label(destination_row, text="Guardar en").pack(side="left")
        destination_var = tk.StringVar(
            value=os.path.join(OUTPUT_DIR, "drive_downloads")
        )
        destination_entry = ttk.Entry(
            destination_row, textvariable=destination_var, state="readonly"
        )
        destination_entry.pack(side="left", fill="x", expand=True, padx=(8, 6))

        def choose_drive_destination():
            initial_directory = destination_var.get()
            if not os.path.isdir(initial_directory):
                initial_directory = os.path.dirname(initial_directory)
            if not os.path.isdir(initial_directory):
                initial_directory = os.path.expanduser("~")
            selected = filedialog.askdirectory(
                parent=window,
                title="Seleccionar carpeta para los videos",
                initialdir=initial_directory,
                mustexist=False,
            )
            if selected:
                destination_var.set(selected)

        ttk.Button(
            destination_row,
            text="Examinar...",
            command=choose_drive_destination,
        ).pack(side="left")

        list_frame = ttk.Frame(container)
        list_frame.pack(fill="both", expand=True, pady=(0, 8))
        scrollbar = ttk.Scrollbar(list_frame, orient="vertical")
        video_list = tk.Listbox(
            list_frame,
            height=12,
            activestyle="none",
            yscrollcommand=scrollbar.set,
        )
        scrollbar.config(command=video_list.yview)
        video_list.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        progress = ttk.Progressbar(container, mode="determinate", maximum=100)
        progress.pack(fill="x", pady=(0, 4))
        status_var = tk.StringVar(value="Pega o confirma el enlace de Drive.")
        ttk.Label(
            container, textvariable=status_var, wraplength=580, justify="left"
        ).pack(anchor="w", pady=(0, 8))

        buttons = ttk.Frame(container)
        buttons.pack(fill="x")
        events = queue.Queue()
        state = {"videos": [], "busy": False}

        def set_busy(is_busy):
            state["busy"] = is_busy
            action_state = "disabled" if is_busy else "normal"
            analyze_button.config(state=action_state)
            download_button.config(
                state=(
                    "normal"
                    if not is_busy and state["videos"]
                    else "disabled"
                )
            )
            url_entry.config(state=action_state)

        def analyze_folder():
            folder_url = url_var.get().strip()
            if not folder_url:
                messagebox.showwarning(
                    "Falta el enlace",
                    "Pega el enlace de la carpeta de Google Drive.",
                    parent=window,
                )
                return

            video_list.delete(0, tk.END)
            state["videos"] = []
            progress.config(value=0, maximum=100)
            status_var.set("Leyendo la carpeta pública de Drive...")
            set_busy(True)

            def worker():
                try:
                    videos = list_drive_videos(folder_url)
                except DriveImportError as exc:
                    events.put(("error", str(exc), exc.open_in_browser))
                else:
                    events.put(("listed", videos))

            threading.Thread(target=worker, daemon=True).start()

        def download_folder_videos():
            destination = destination_var.get().strip()
            videos = state["videos"]
            if not videos:
                return

            destination_path = os.path.abspath(destination)
            conflicts = [
                video.display_name
                for video in videos
                if os.path.exists(
                    os.path.join(destination_path, video.relative_path)
                )
            ]
            if conflicts:
                messagebox.showwarning(
                    "Archivos existentes",
                    "No se sobrescribieron archivos. Elige otra carpeta o mueve "
                    "los que ya existen:\n\n" + "\n".join(conflicts[:5]),
                    parent=window,
                )
                return

            video_list.delete(0, tk.END)
            for video in videos:
                video_list.insert(tk.END, f"{video.display_name} — pendiente")
            progress.config(value=0, maximum=len(videos))
            status_var.set(f"Descargando 0 de {len(videos)}...")
            set_busy(True)

            def worker():
                for index, video in enumerate(videos):
                    events.put(("current", index, video.display_name))
                    try:
                        local_path = download_drive_video(video, destination_path)
                        duration = get_video_duration(local_path)
                    except DriveImportError as exc:
                        events.put(("download_error", index, str(exc)))
                        return
                    events.put(("downloaded", index, duration))
                events.put(("download_complete", len(videos)))

            threading.Thread(target=worker, daemon=True).start()

        analyze_button = ttk.Button(
            buttons, text="Analizar carpeta", command=analyze_folder
        )
        analyze_button.pack(side="left")
        download_button = ttk.Button(
            buttons,
            text="Descargar todos los MP4 en orden",
            command=download_folder_videos,
            state="disabled",
        )
        download_button.pack(side="right")

        def process_events():
            if not window.winfo_exists():
                return
            while True:
                try:
                    event = events.get_nowait()
                except queue.Empty:
                    break

                event_type = event[0]
                if event_type == "listed":
                    state["videos"] = event[1]
                    for video in state["videos"]:
                        video_list.insert(tk.END, f"{video.display_name} — duración pendiente")
                    status_var.set(
                        f"{len(state['videos'])} archivo(s) MP4 encontrados. "
                        "La duración se verificará después de cada descarga."
                    )
                    set_busy(False)
                elif event_type == "error":
                    status_var.set(event[1])
                    set_busy(False)
                    if event[2]:
                        browser_error = None
                        try:
                            browser_opened = webbrowser.open(url_var.get().strip())
                        except (webbrowser.Error, OSError) as exc:
                            browser_opened = False
                            browser_error = str(exc)
                        if browser_opened:
                            status_var.set(
                                "Drive requiere acceso. Se abrió la carpeta en el "
                                "navegador para que inicies sesión o revises los "
                                "permisos. Después, configura “Cualquier persona "
                                "con el enlace” y vuelve a analizar."
                            )
                    messagebox.showerror(
                        "Acceso a Google Drive",
                        (
                            f"{event[1]}\n\n"
                            + (
                                "Se abrió el enlace en el navegador."
                                if browser_opened
                                else "No se pudo abrir el navegador automáticamente."
                            )
                            + " Inicia sesión allí si Google lo solicita y revisa "
                            "los permisos de la carpeta. Esta aplicación no recibe "
                            "esa sesión; para descargar, habilita “Cualquier "
                            "persona con el enlace” y vuelve a analizar."
                            + (
                                f"\n\nDetalle al abrir el navegador: {browser_error}"
                                if browser_error
                                else ""
                            )
                            if event[2]
                            else event[1]
                        ),
                        parent=window,
                    )
                elif event_type == "current":
                    status_var.set(
                        f"Descargando archivo {event[1] + 1} de "
                        f"{len(state['videos'])}: {event[2]}"
                    )
                elif event_type == "downloaded":
                    index, duration = event[1], event[2]
                    hours, remainder = divmod(int(duration), 3600)
                    minutes, seconds = divmod(remainder, 60)
                    video = state["videos"][index]
                    video_list.delete(index)
                    video_list.insert(
                        index,
                        f"{video.display_name} — "
                        f"{hours:02d}:{minutes:02d}:{seconds:02d}",
                    )
                    progress.config(value=index + 1)
                    status_var.set(
                        f"Verificados {index + 1} de {len(state['videos'])} MP4."
                    )
                elif event_type == "download_error":
                    index, error_message = event[1], event[2]
                    video = state["videos"][index]
                    video_list.delete(index)
                    video_list.insert(
                        index,
                        f"{video.display_name} — error al leer/descargar",
                    )
                    status_var.set(
                        f"Proceso detenido en el archivo {index + 1}: {error_message}"
                    )
                    set_busy(False)
                    messagebox.showerror(
                        "Error durante la descarga",
                        error_message,
                        parent=window,
                    )
                elif event_type == "download_complete":
                    set_busy(False)
                    progress.config(value=event[1])
                    status_var.set(
                        f"Listo: {event[1]} MP4 descargados. "
                        f"Carpeta: {destination_var.get()}"
                    )
                    messagebox.showinfo(
                        "Descarga completa",
                        status_var.get(),
                        parent=window,
                    )
            window.after(100, process_events)

        def close_window():
            if state["busy"]:
                messagebox.showinfo(
                    "Proceso en curso",
                    "Espera a que termine el análisis o la descarga antes de cerrar.",
                    parent=window,
                )
                return
            window.destroy()

        window.protocol("WM_DELETE_WINDOW", close_window)
        window.after(100, process_events)

    # ------------------------------------------------------------------
    # Formato de video
    # ------------------------------------------------------------------
    def get_selected_format(self):
        selected_format = FORMAT_OPTIONS.get(
            self.format_var.get(), FORMAT_OPTIONS[DEFAULT_FORMAT]
        )
        quality = self.quality_var.get()
        profile = QUALITY_PROFILES[selected_format["quality_family"]].get(
            quality, QUALITY_PROFILES[selected_format["quality_family"]][DEFAULT_QUALITY]
        )
        return {**selected_format, **profile}

    def _update_quality_estimate(self):
        fmt = FORMAT_OPTIONS.get(
            self.format_var.get(), FORMAT_OPTIONS[DEFAULT_FORMAT]
        )
        quality = self.quality_var.get()
        profile = QUALITY_PROFILES[fmt["quality_family"]].get(
            quality, QUALITY_PROFILES[fmt["quality_family"]][DEFAULT_QUALITY]
        )
        estimate = profile["estimate_mib_10min"]
        formatted_estimate = f"{estimate:,}".replace(",", ".")
        resolution = "1080p" if profile["output_size"] else "1080p ref."
        self.quality_estimate_var.set(
            f"10 min - {resolution}/{profile['fps']} fps: ~{formatted_estimate} MiB"
        )

    def _on_quality_change(self, event=None):
        self._update_quality_estimate()

    def _on_format_change(self, event=None):
        self._update_quality_estimate()
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
                target=record_system_audio,
                args=(
                    self._audio_tmp,
                    self._capture_start_event,
                    self._audio_ready_event,
                    lambda: self.recording,
                    lambda: self.paused,
                    self._mark_audio_recording_started,
                    AUDIO_CHUNK,
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
        self.quality_combo.config(state="disabled")
        self._set_duration_controls_state(False)

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
        self._set_timer_state("REC", "#fb7185")

    def _set_duration_controls_state(self, enabled):
        state = "normal" if enabled else "disabled"
        for spinbox in self.duration_entry:
            spinbox.config(state=state)

    def _sync_duration_value(self, *_):
        parts = (
            self.duration_hours_var.get(),
            self.duration_minutes_var.get(),
            self.duration_seconds_var.get(),
        )
        if all(part.isdigit() and int(part) == 0 for part in parts):
            self.duration_var.set("")
            return
        self.duration_var.set(":".join(parts))

    def _set_timer_state(self, text, color):
        self.timer_state_var.set(text)
        self.timer_state_label.config(fg=color)

    def _send_space_to_recording_window(self):
        key_pressed = False
        error = None
        try:
            win32gui.SetForegroundWindow(self._recording_hwnd)
            if win32gui.GetForegroundWindow() != self._recording_hwnd:
                raise OSError("Windows no permitió activar la ventana seleccionada.")
            win32api.keybd_event(win32con.VK_SPACE, 0, 0, 0)
            key_pressed = True
            time.sleep(0.05)
        except Exception as exc:
            error = str(exc)
        finally:
            if key_pressed:
                try:
                    win32api.keybd_event(
                        win32con.VK_SPACE, 0, win32con.KEYEVENTF_KEYUP, 0
                    )
                except Exception as exc:
                    error = f"No se pudo liberar la tecla ESPACIO: {exc}"
        return error

    def toggle_pause(self):
        if not self.recording:
            return
        self.paused = not self.paused
        if self.paused:
            self._pause_start_ts = time.monotonic()
            self.pause_btn.config(text="Reproducir")
            self._set_timer_state("PAUSA", "#fbbf24")
            status = "Video y grabación en pausa."
        else:
            if self._pause_start_ts is not None:
                self._paused_total += time.monotonic() - self._pause_start_ts
                self._pause_start_ts = None
            self.pause_btn.config(text="Pausar")
            self._set_timer_state("REC", "#fb7185")
            status = "Video y grabación reanudados."

        if self.play_on_start_var.get():
            space_error = self._send_space_to_recording_window()
            if space_error:
                status += f" No se pudo controlar el video automáticamente: {space_error}"
                status += " La captura sí cambió de estado."
        self.status_var.set(status)

    def _record_video_loop(
        self, left, top, width, height, filename, fmt, start_event, ready_event
    ):
        """Captura la ventana y la codifica EN VIVO (cuadro a cuadro) con
        ffmpeg vía pipe, ya en el códec final del formato elegido. Así, al
        pulsar Detener, el video ya está terminado (no hace falta esperar
        una recompresión posterior)."""
        try:
            ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
            fps = fmt["fps"]
            output_size = fmt["output_size"] or (width, height)
            cmd = [
                ffmpeg_exe, "-y",
                "-f", "rawvideo",
                "-pixel_format", "bgr24",
                "-video_size", f"{output_size[0]}x{output_size[1]}",
                "-framerate", str(fps),
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
        frame_interval = 1.0 / fps
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
            with mss.MSS() as sct:
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
                    if (frame.shape[1], frame.shape[0]) != output_size:
                        scale = min(
                            output_size[0] / frame.shape[1],
                            output_size[1] / frame.shape[0],
                        )
                        resized_size = (
                            max(1, round(frame.shape[1] * scale)),
                            max(1, round(frame.shape[0] * scale)),
                        )
                        interpolation = (
                            cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR
                        )
                        resized = cv2.resize(
                            frame, resized_size, interpolation=interpolation
                        )
                        frame = np.zeros(
                            (output_size[1], output_size[0], 3), dtype=np.uint8
                        )
                        offset_x = (output_size[0] - resized_size[0]) // 2
                        offset_y = (output_size[1] - resized_size[1]) // 2
                        frame[
                            offset_y:offset_y + resized_size[1],
                            offset_x:offset_x + resized_size[0],
                        ] = resized
                    frame_bytes = frame.tobytes()

                    with self._frame_lock:
                        self._latest_frame = frame

                    elapsed = time.monotonic() - start_time - paused_total
                    target_frames = max(1, math.ceil(elapsed * fps))
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

    def _mark_audio_recording_started(self):
        self._recording_audio = True

    def stop_recording(self):
        if not self.recording:
            return
        self.recording = False
        self.paused = False
        self._set_timer_state("GUARDANDO", "#60a5fa")

        self.start_btn.config(state="disabled")
        self.pause_btn.config(state="disabled", text="Pausar")
        self.stop_btn.config(state="disabled")
        self.play_btn.config(state="disabled")
        self.combo.config(state="disabled")
        self.save_btn.config(state="disabled")
        self.format_combo.config(state="disabled")
        self.quality_combo.config(state="disabled")
        self._set_duration_controls_state(False)
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
                mux_succeeded = mux_audio(
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
        self.quality_combo.config(state="readonly")
        self._set_duration_controls_state(True)
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
        if self._finalize_result and self._finalize_result.startswith(
            "Grabación guardada"
        ):
            self._set_timer_state("GUARDADO", "#34d399")
        else:
            self._set_timer_state("ERROR", "#fb7185")

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
