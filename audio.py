"""Captura del audio de salida del sistema mediante WASAPI loopback."""

import time
import wave

try:
    import pyaudiowpatch as pyaudio

    HAS_AUDIO = True
except ImportError:
    pyaudio = None
    HAS_AUDIO = False


def get_loopback_device(audio):
    """Obtiene el dispositivo loopback asociado a la salida predeterminada."""
    try:
        wasapi_info = audio.get_host_api_info_by_type(pyaudio.paWASAPI)
    except OSError:
        return None

    try:
        default_speakers = audio.get_device_info_by_index(
            wasapi_info["defaultOutputDevice"]
        )
    except OSError:
        return None

    if default_speakers.get("isLoopbackDevice", False):
        return default_speakers

    for loopback in audio.get_loopback_device_info_generator():
        if default_speakers["name"] in loopback["name"]:
            return loopback

    return None


def record_system_audio(
    audio_path,
    start_event,
    ready_event,
    is_recording,
    is_paused,
    on_started,
    chunk_size,
):
    """Graba loopback a WAV y descarta los bloques capturados durante pausa."""
    if not HAS_AUDIO:
        ready_event.set()
        return

    audio = None
    stream = None
    try:
        audio = pyaudio.PyAudio()
        device = get_loopback_device(audio)
        if device is None:
            return

        channels = max(1, int(device["maxInputChannels"]))
        rate = int(device["defaultSampleRate"])
        stream = audio.open(
            format=pyaudio.paInt16,
            channels=channels,
            rate=rate,
            input=True,
            input_device_index=device["index"],
            frames_per_buffer=chunk_size,
        )

        ready_event.set()
        while is_recording() and not start_event.wait(0.1):
            pass
        if not is_recording():
            return

        on_started()
        frames = []
        # El loopback no entrega datos mientras no suena nada y un read()
        # normal se bloquea para siempre: solo se lee lo ya disponible y los
        # silencios se rellenan para que el audio dure lo mismo que el video.
        silence = b"\x00" * (chunk_size * channels * audio.get_sample_size(pyaudio.paInt16))
        start = time.monotonic()
        paused_total = 0.0
        pause_start = None
        written = 0
        while is_recording():
            now = time.monotonic()
            try:
                available = stream.get_read_available()
                if is_paused():
                    if pause_start is None:
                        pause_start = now
                    if available >= chunk_size:
                        stream.read(chunk_size, exception_on_overflow=False)
                    else:
                        time.sleep(0.01)
                    continue
                if pause_start is not None:
                    paused_total += now - pause_start
                    pause_start = None

                if available >= chunk_size:
                    frames.append(
                        stream.read(chunk_size, exception_on_overflow=False)
                    )
                    written += chunk_size
                    continue
            except Exception:
                break

            expected = (now - start - paused_total) * rate
            if expected - written >= chunk_size + rate * 0.2:
                frames.append(silence)
                written += chunk_size
            else:
                time.sleep(0.005)

        with wave.open(audio_path, "wb") as wav_file:
            wav_file.setnchannels(channels)
            wav_file.setsampwidth(audio.get_sample_size(pyaudio.paInt16))
            wav_file.setframerate(rate)
            wav_file.writeframes(b"".join(frames))
    finally:
        ready_event.set()
        if stream is not None:
            stream.stop_stream()
            stream.close()
        if audio is not None:
            audio.terminate()
