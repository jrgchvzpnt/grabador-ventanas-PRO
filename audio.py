"""Captura del audio de salida del sistema mediante WASAPI loopback."""

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
        while is_recording():
            try:
                data = stream.read(chunk_size, exception_on_overflow=False)
            except Exception:
                break
            if not is_paused():
                frames.append(data)

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
