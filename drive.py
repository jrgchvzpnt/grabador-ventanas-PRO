"""Access and download videos from a publicly shared Google Drive folder."""

import math
import os
from dataclasses import dataclass
from pathlib import Path

import cv2
import gdown
import requests
from gdown.exceptions import DownloadError


class DriveImportError(RuntimeError):
    """Raised when a public Drive folder or one of its videos cannot be read."""

    def __init__(self, message, *, open_in_browser=False):
        super().__init__(message)
        self.open_in_browser = open_in_browser


@dataclass(frozen=True)
class DriveVideo:
    file_id: str
    relative_path: str

    @property
    def display_name(self):
        return self.relative_path.replace("\\", "/")


def list_drive_videos(folder_url):
    """Return MP4 files in the order reported by the public Drive folder."""
    if not folder_url.strip():
        raise DriveImportError("Pega el enlace de una carpeta pública de Google Drive.")

    try:
        files = gdown.download_folder(
            url=folder_url.strip(),
            quiet=True,
            skip_download=True,
            use_cookies=False,
        )
    except (DownloadError, requests.RequestException, ValueError, OSError) as exc:
        status_requires_access = any(
            status in str(exc).lower()
            for status in ("status code 401", "status code 403")
        )
        raise DriveImportError(
            "Drive no permitió leer la carpeta. Comprueba el acceso en el "
            "navegador; para esta importación, la carpeta debe estar compartida "
            "como “Cualquier persona con el enlace”. Iniciar sesión en el "
            "navegador no transfiere esa sesión a la aplicación.",
            open_in_browser=status_requires_access,
        ) from exc

    videos = []
    seen_paths = set()
    for file in files:
        relative_path = file.path
        if Path(relative_path).suffix.lower() != ".mp4":
            continue
        if Path(relative_path).is_absolute() or ".." in Path(relative_path).parts:
            raise DriveImportError(
                f"Drive devolvió una ruta de archivo no válida: {relative_path}"
            )
        normalized_path = os.path.normcase(os.path.normpath(relative_path))
        if normalized_path in seen_paths:
            raise DriveImportError(
                f"Hay nombres de archivo duplicados en la carpeta: {relative_path}"
            )
        seen_paths.add(normalized_path)
        videos.append(DriveVideo(file_id=file.id, relative_path=relative_path))

    if not videos:
        raise DriveImportError(
            "No se encontraron archivos MP4. Comprueba que la carpeta sea "
            "pública y que contenga videos con extensión .mp4."
        )
    return videos


def download_drive_video(video, output_directory):
    """Download one public Drive video while preserving its relative path."""
    root = Path(output_directory).resolve()
    relative_path = Path(video.relative_path)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise DriveImportError(f"Ruta de archivo no válida: {video.relative_path}")

    destination = (root / relative_path).resolve()
    try:
        destination.relative_to(root)
    except ValueError as exc:
        raise DriveImportError(
            f"La ruta de descarga sale de la carpeta de destino: {video.relative_path}"
        ) from exc

    if destination.exists():
        raise DriveImportError(
            f"El archivo ya existe y no se sobrescribió: {destination}"
        )

    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise DriveImportError(
            f"No se pudo crear la carpeta de destino para {video.display_name}."
        ) from exc

    try:
        downloaded_path = gdown.download(
            id=video.file_id,
            output=str(destination),
            quiet=True,
            use_cookies=False,
            resume=True,
        )
    except (DownloadError, requests.RequestException, ValueError, OSError) as exc:
        raise DriveImportError(
            f"No se pudo descargar {video.display_name}. "
            "Comprueba que el archivo siga siendo público."
        ) from exc

    try:
        download_is_complete = (
            downloaded_path
            and destination.is_file()
            and destination.stat().st_size > 0
        )
    except OSError as exc:
        raise DriveImportError(
            f"No se pudo verificar la descarga de {video.display_name}."
        ) from exc
    if not download_is_complete:
        raise DriveImportError(f"La descarga de {video.display_name} quedó incompleta.")
    return destination


def get_video_duration(video_path):
    """Read duration from the downloaded MP4's frame count and frame rate."""
    capture = None
    try:
        capture = cv2.VideoCapture(str(video_path))
        if not capture.isOpened():
            raise DriveImportError(
                f"No se pudo leer la duración del video: {video_path}"
            )
        frame_count = capture.get(cv2.CAP_PROP_FRAME_COUNT)
        frame_rate = capture.get(cv2.CAP_PROP_FPS)
    except cv2.error as exc:
        raise DriveImportError(
            f"OpenCV no pudo leer los metadatos de {video_path}."
        ) from exc
    finally:
        if capture is not None:
            capture.release()

    if (
        not math.isfinite(frame_count)
        or not math.isfinite(frame_rate)
        or frame_count <= 0
        or frame_rate <= 0
    ):
        raise DriveImportError(
            f"El archivo se descargó, pero no se pudo determinar su duración: "
            f"{video_path}"
        )
    return frame_count / frame_rate
