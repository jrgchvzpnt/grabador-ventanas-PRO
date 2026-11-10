"""Access and download videos from a publicly shared Google Drive folder."""

import math
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import cv2
import gdown
import imageio_ffmpeg
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
    duration_seconds: float = None  # solo se conoce al listar con la cuenta

    @property
    def display_name(self):
        return self.relative_path.replace("\\", "/")


APP_DIR = Path(__file__).resolve().parent
CREDENTIALS_FILE = APP_DIR / "credentials.json"
TOKEN_FILE = APP_DIR / "token.json"
DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]


def account_login_available():
    """True when the OAuth client file for the Drive API is present."""
    return CREDENTIALS_FILE.is_file()


def _natural_key(text):
    return [
        int(part) if part.isdigit() else part.lower()
        for part in re.split(r"(\d+)", text)
    ]


def _extract_folder_id(folder_url):
    match = re.search(r"/folders/([\w-]+)", folder_url) or re.search(
        r"[?&]id=([\w-]+)", folder_url
    )
    if not match:
        raise DriveImportError("No se reconoció el ID de la carpeta en el enlace.")
    return match.group(1)


def _drive_service():
    """Drive API client authorized with the user's own Google account."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds = None
    if TOKEN_FILE.is_file():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), DRIVE_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception:
                creds = None
        else:
            creds = None
        if creds is None:
            flow = InstalledAppFlow.from_client_secrets_file(
                str(CREDENTIALS_FILE), DRIVE_SCOPES
            )
            creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def list_drive_videos_with_account(folder_url):
    """List MP4s (with durations) in a folder the signed-in account can see."""
    if not account_login_available():
        raise DriveImportError(
            "Falta credentials.json junto a app.py (ver README: acceso con "
            "tu cuenta de Google)."
        )
    folder_id = _extract_folder_id(folder_url)

    try:
        service = _drive_service()
        videos = []

        def walk(parent_id, prefix):
            page_token = None
            entries = []
            while True:
                response = (
                    service.files()
                    .list(
                        q=f"'{parent_id}' in parents and trashed = false",
                        fields=(
                            "nextPageToken, files(id, name, mimeType, "
                            "videoMediaMetadata(durationMillis))"
                        ),
                        pageSize=1000,
                        pageToken=page_token,
                        supportsAllDrives=True,
                        includeItemsFromAllDrives=True,
                    )
                    .execute()
                )
                entries.extend(response.get("files", []))
                page_token = response.get("nextPageToken")
                if not page_token:
                    break
            for entry in sorted(entries, key=lambda e: _natural_key(e["name"])):
                path = os.path.join(prefix, entry["name"])
                if entry["mimeType"] == "application/vnd.google-apps.folder":
                    walk(entry["id"], path)
                elif Path(entry["name"]).suffix.lower() == ".mp4":
                    millis = (entry.get("videoMediaMetadata") or {}).get(
                        "durationMillis"
                    )
                    videos.append(
                        DriveVideo(
                            file_id=entry["id"],
                            relative_path=path,
                            duration_seconds=int(millis) / 1000 if millis else None,
                        )
                    )

        walk(folder_id, "")
    except DriveImportError:
        raise
    except Exception as exc:
        raise DriveImportError(
            f"No se pudo leer la carpeta con tu cuenta de Google: {exc}"
        ) from exc

    if not videos:
        raise DriveImportError("No se encontraron archivos MP4 en la carpeta.")
    return videos


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


def drive_view_url(file_id):
    """URL of Drive's web player for a file."""
    return f"https://drive.google.com/file/d/{file_id}/view"


def get_remote_duration(file_id, timeout=60):
    """Read a shared Drive video's duration (seconds) without downloading it.

    ffmpeg reads only the MP4 metadata through HTTP range requests.
    """
    url = (
        "https://drive.usercontent.google.com/download"
        f"?id={file_id}&export=download&confirm=t"
    )
    try:
        result = subprocess.run(
            [imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-i", url],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise DriveImportError(
            f"No se pudo leer la duración del video {file_id} desde Drive."
        ) from exc

    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", result.stderr)
    if not match:
        raise DriveImportError(
            f"Drive no entregó la duración del video {file_id}."
        )
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


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
