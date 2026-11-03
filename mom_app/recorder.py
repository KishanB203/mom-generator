"""Records two tracks at once: your microphone and what your speakers play (WASAPI loopback = other participants).

Keeping them separate lets the transcript label lines spoken by you vs. remote participants.
WASAPI loopback delivers no data while nothing is playing, so silence is padded using wall-clock time
to keep both tracks aligned.
"""

import logging
import queue
import threading
import time
import wave
from pathlib import Path

import numpy as np
import pyaudiowpatch as pyaudio

log = logging.getLogger(__name__)

PAD_THRESHOLD_S = 0.25


class AudioDeviceError(RuntimeError):
    pass


MIC_HELP = (
    "Check Windows Settings > Privacy & security > Microphone: turn on 'Microphone access' and "
    "'Let desktop apps access your microphone'. Also make sure the microphone is plugged in and not "
    "used exclusively by another app."
)


def list_devices() -> tuple[list[str], list[str]]:
    """Return (microphones, loopback speakers) names on the WASAPI host API."""
    pa = pyaudio.PyAudio()
    try:
        wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)["index"]
        mics, speakers = [], []
        for i in range(pa.get_device_count()):
            d = pa.get_device_info_by_index(i)
            if d["hostApi"] != wasapi or d["maxInputChannels"] <= 0:
                continue
            (speakers if d.get("isLoopbackDevice") else mics).append(d["name"])
        return mics, speakers
    finally:
        pa.terminate()


BUILT_IN_MIC_HINTS = ("microphone array", "internal", "built-in", "integrated")


def room_microphone(configured: str = "") -> str:
    """Microphone for in-person meetings: the configured one, else the laptop's built-in mic, else "" (default).

    A headset mic only hears its wearer, so the built-in array is preferred for a room."""
    if configured:
        return configured
    mics, _ = list_devices()
    for name in mics:
        if any(h in name.lower() for h in BUILT_IN_MIC_HINTS):
            return name
    return ""


def _find_device(pa, name: str, loopback: bool) -> dict:
    wasapi_info = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
    if name:
        for i in range(pa.get_device_count()):
            d = pa.get_device_info_by_index(i)
            if (
                d["hostApi"] == wasapi_info["index"]
                and d["name"] == name
                and bool(d.get("isLoopbackDevice")) == loopback
                and d["maxInputChannels"] > 0
            ):
                return d
        log.warning("Device %r not found, falling back to default", name)
    if loopback:
        return pa.get_default_wasapi_loopback()
    idx = wasapi_info["defaultInputDevice"]
    if idx < 0:
        raise AudioDeviceError("No microphone found. " + MIC_HELP)
    return pa.get_device_info_by_index(idx)


class _Track:
    def __init__(self, pa, device: dict, path: Path, t0: float):
        self.device = device
        self.path = path
        self.rate = int(device["defaultSampleRate"])
        self.channels = max(1, min(2, int(device["maxInputChannels"])))
        self.t0 = t0
        self.written = 0
        self.peak = 0
        self.q: queue.Queue = queue.Queue()
        self.wav = wave.open(str(path), "wb")
        self.wav.setnchannels(self.channels)
        self.wav.setsampwidth(2)
        self.wav.setframerate(self.rate)
        self.stream = pa.open(
            format=pyaudio.paInt16,
            channels=self.channels,
            rate=self.rate,
            input=True,
            input_device_index=device["index"],
            frames_per_buffer=int(self.rate * 0.05),
            stream_callback=self._callback,
        )

    def _callback(self, in_data, frame_count, time_info, status):
        self.q.put((time.monotonic(), in_data, frame_count))
        return (None, pyaudio.paContinue)

    def _pad_to(self, frame_pos: int) -> None:
        gap = frame_pos - self.written
        if gap > self.rate * PAD_THRESHOLD_S:
            self.wav.writeframes(b"\x00\x00" * self.channels * gap)
            self.written += gap

    def drain(self) -> None:
        while True:
            try:
                arrived, data, frames = self.q.get_nowait()
            except queue.Empty:
                return
            start = int((arrived - self.t0) * self.rate) - frames
            self._pad_to(start)
            self.wav.writeframes(data)
            self.written += frames
            if data:
                self.peak = max(self.peak, int(np.abs(np.frombuffer(data, dtype=np.int16)).max(initial=0)))

    def close(self, end_time: float) -> None:
        try:
            self.stream.stop_stream()
            self.stream.close()
        except Exception:
            log.exception("Error closing stream %s", self.path.name)
        self.drain()
        self._pad_to(int((end_time - self.t0) * self.rate))
        self.wav.close()


class Recorder:
    """Start/stop dual-track recording into a session folder."""

    def __init__(self, mic_name: str = "", speaker_name: str = "", mic_only: bool = False):
        self.mic_name = mic_name
        self.speaker_name = speaker_name
        self.mic_only = mic_only  # in-person meeting: nothing to capture from the speakers
        self.pa = None
        self.tracks: dict[str, _Track] = {}
        self.started_at = 0.0
        self._stop = threading.Event()
        self._thread = None
        self.warnings: list[str] = []

    @property
    def is_recording(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def elapsed(self) -> float:
        return time.monotonic() - self.started_at if self.is_recording else 0.0

    def mic_peak(self) -> int:
        t = self.tracks.get("mic")
        return t.peak if t else 0

    def start(self, folder: Path) -> None:
        folder.mkdir(parents=True, exist_ok=True)
        self.pa = pyaudio.PyAudio()
        self.started_at = time.monotonic()
        self.warnings = []
        try:
            mic = _find_device(self.pa, self.mic_name, loopback=False)
            try:
                self.tracks["mic"] = _Track(self.pa, mic, folder / "mic.wav", self.started_at)
            except OSError as e:
                raise AudioDeviceError(f"Could not open microphone '{mic['name']}' ({e}). " + MIC_HELP) from e
            try:
                if not self.mic_only:
                    spk = _find_device(self.pa, self.speaker_name, loopback=True)
                    self.tracks["system"] = _Track(self.pa, spk, folder / "system.wav", self.started_at)
            except Exception as e:  # meeting can still be captured from the mic alone
                log.exception("Loopback capture unavailable")
                self.warnings.append(f"Could not capture speaker audio ({e}); recording microphone only.")
            for t in self.tracks.values():
                t.stream.start_stream()
        except Exception:
            self._cleanup()
            raise
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="recorder", daemon=True)
        self._thread.start()
        log.info("Recording started: %s", {k: t.device["name"] for k, t in self.tracks.items()})

    def _loop(self) -> None:
        while not self._stop.wait(0.2):
            for t in self.tracks.values():
                try:
                    t.drain()
                except Exception:
                    log.exception("Error writing %s", t.path.name)

    def stop(self) -> dict[str, Path]:
        if self._thread is None:
            return {}
        self._stop.set()
        self._thread.join(timeout=5)
        end = time.monotonic()
        paths = {}
        for name, t in self.tracks.items():
            t.close(end)
            paths[name] = t.path
        self._cleanup()
        log.info("Recording stopped after %.0fs", end - self.started_at)
        return paths

    def _cleanup(self) -> None:
        self._thread = None
        self.tracks = {}
        if self.pa is not None:
            self.pa.terminate()
            self.pa = None


def load_mono_16k(path: Path) -> np.ndarray:
    """Read a 16-bit WAV and return float32 mono at 16 kHz (what Whisper expects)."""
    from .resample import to_16k

    with wave.open(str(path), "rb") as w:
        rate, channels = w.getframerate(), w.getnchannels()
        raw = w.readframes(w.getnframes())
    audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1)
    return to_16k(audio, rate)
