"""Utilidades para combinar pistas de video y audio."""

import os
import subprocess
import wave

import cv2
import imageio_ffmpeg


def mux_audio(video_path, audio_path, out_path, fmt, progress_queue):
    """Agrega audio al video sin recodificarlo y compensa la deriva de duración."""
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
            if separator and key == "out_time_ms":
                elapsed = int(value) / 1_000_000
                percent = min(99, int(elapsed * 100 / video_duration))
                progress_queue.put(("progress", percent))

        proc.wait()
        return proc.returncode == 0 and os.path.exists(out_path)
    except (OSError, ValueError, ZeroDivisionError, wave.Error):
        return False
