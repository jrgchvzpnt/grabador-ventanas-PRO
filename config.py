"""Configuración de captura, formatos y perfiles de calidad."""

FPS = 15
AUDIO_CHUNK = 1024
PREVIEW_SIZE = (220, 124)  # 16:9
PREVIEW_INTERVAL_MS = 200
QUALITY_LEVELS = ("Alta", "Media", "Baja")

QUALITY_PROFILES = {
    "h264": {
        "Alta": {
            "video_args_live": [
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "16",
                "-pix_fmt", "yuv420p",
            ],
            "fps": 30,
            "output_size": (1920, 1080),
            "estimate_mib_10min": 763,
        },
        "Media": {
            "video_args_live": [
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                "-pix_fmt", "yuv420p",
            ],
            "fps": FPS,
            "output_size": None,
            "estimate_mib_10min": 436,
        },
        "Baja": {
            "video_args_live": [
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                "-pix_fmt", "yuv420p",
            ],
            "fps": FPS,
            "output_size": None,
            "estimate_mib_10min": 296,
        },
    },
    "xvid": {
        "Alta": {
            "video_args_live": [
                "-c:v", "libxvid", "-qscale:v", "1", "-tag:v", "XVID",
            ],
            "fps": 30,
            "output_size": (1920, 1080),
            "estimate_mib_10min": 2924,
        },
        "Media": {
            "video_args_live": [
                "-c:v", "libxvid", "-qscale:v", "2", "-tag:v", "XVID",
            ],
            "fps": FPS,
            "output_size": None,
            "estimate_mib_10min": 1033,
        },
        "Baja": {
            "video_args_live": [
                "-c:v", "libxvid", "-qscale:v", "4", "-tag:v", "XVID",
            ],
            "fps": FPS,
            "output_size": None,
            "estimate_mib_10min": 647,
        },
    },
    "vp9": {
        "Alta": {
            "video_args_live": [
                "-c:v", "libvpx-vp9", "-crf", "24", "-b:v", "0",
                "-pix_fmt", "yuv420p", "-deadline", "realtime", "-cpu-used", "6",
            ],
            "fps": 30,
            "output_size": (1920, 1080),
            "estimate_mib_10min": 860,
        },
        "Media": {
            "video_args_live": [
                "-c:v", "libvpx-vp9", "-crf", "28", "-b:v", "0",
                "-pix_fmt", "yuv420p", "-deadline", "realtime", "-cpu-used", "6",
            ],
            "fps": FPS,
            "output_size": None,
            "estimate_mib_10min": 387,
        },
        "Baja": {
            "video_args_live": [
                "-c:v", "libvpx-vp9", "-crf", "34", "-b:v", "0",
                "-pix_fmt", "yuv420p", "-deadline", "realtime", "-cpu-used", "6",
            ],
            "fps": FPS,
            "output_size": None,
            "estimate_mib_10min": 298,
        },
    },
}

# El video se codifica en vivo con el perfil seleccionado por el usuario.
FORMAT_OPTIONS = {
    "MP4 (.mp4)": {
        "ext": ".mp4",
        "quality_family": "h264",
        "audio_args": ["-c:a", "aac", "-b:a", "192k"],
    },
    "MKV (.mkv)": {
        "ext": ".mkv",
        "quality_family": "h264",
        "audio_args": ["-c:a", "aac", "-b:a", "192k"],
    },
    "MOV (.mov)": {
        "ext": ".mov",
        "quality_family": "h264",
        "audio_args": ["-c:a", "aac", "-b:a", "192k"],
    },
    "AVI (.avi)": {
        "ext": ".avi",
        "quality_family": "xvid",
        "audio_args": ["-c:a", "libmp3lame", "-b:a", "192k"],
    },
    "WEBM (.webm)": {
        "ext": ".webm",
        "quality_family": "vp9",
        "audio_args": ["-c:a", "libopus", "-b:a", "128k"],
    },
}
DEFAULT_FORMAT = "MP4 (.mp4)"
DEFAULT_QUALITY = "Media"
