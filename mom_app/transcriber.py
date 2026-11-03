"""Local speech-to-text with faster-whisper (Claude takes text, not audio).

Runs on your PC, so meeting audio never leaves the machine; only the text transcript goes to Claude.

Why not one `model.transcribe(whole_file)`: Whisper then guesses ONE language for the whole track, and when it
guesses English for Hindi/Gujarati speech it silently *translates* into made-up English. Instead we cut the
audio into short utterances at pauses, pick the language of each utterance (only English/Hindi/Gujarati are
allowed) and transcribe it in that language. Whisper often mistakes Gujarati and Hindi for English, so an
utterance is only treated as English when the model is confident; Gujarati written in Devanagari is still
readable for Claude, mistranslated English is not.

Speed: on a laptop CPU the model needs about as long as the speech itself, so LiveTranscriber works through
the recording *while the meeting is running* (at low priority, so Teams keeps the CPU it needs). When the
meeting ends only the last minute or so is left to do.
"""

import ctypes
import logging
import threading
import time
from pathlib import Path
from typing import Callable

import numpy as np

log = logging.getLogger(__name__)

SR = 16000
MAX_CHUNK_S = 28  # seconds of speech per window (Whisper's window is 30 s)
ENGLISH_MIN_PROB = 0.8  # below this, Indic speech is too often mis-detected as English
CPU_THREADS = 4  # more threads are not faster for this model on laptop CPUs, only hotter
# Languages Whisper confuses with Hindi/Gujarati
LANGUAGE_MAP = {"en": "en", "hi": "hi", "gu": "gu", "ur": "hi", "mr": "hi", "ne": "hi", "pa": "hi", "sa": "hi"}
# A sample sentence in each language's usual meeting style (English terms in Latin script) primes Whisper
# to write mixed speech the same way. The glossary terms are appended.
PROMPTS = {
    "en": "Today's meeting covered the project status, tasks and next steps.",
    "hi": "आज की meeting में हमने project status, task और next steps के बारे में बात की।",
    "gu": "આજની meeting માં આપણે project status, task અને next steps વિશે વાત કરી.",
}

# One model user at a time: the live transcriber of a new meeting waits while the previous one finishes.
_model_lock = threading.Lock()
_loaded: dict = {}  # {"key": (name, device), "model": WhisperModel}


def _fmt(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m:02d}:{s:02d}"


def _set_low_priority(low: bool) -> None:
    """Below-normal priority while the meeting runs (Teams first), normal afterwards (finish fast)."""
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), 0x4000 if low else 0x20)
    except Exception:
        log.debug("Could not change process priority", exc_info=True)


# ---------- model ----------
def model_ready(name: str) -> bool:
    """The speech model is already on this PC (no download needed)."""
    from faster_whisper.utils import download_model

    try:
        download_model(name, local_files_only=True)
        return True
    except Exception:
        return False


def download_speech_model(name: str, progress: Callable[[int, int], None] | None = None) -> None:
    """Download the speech model (~1.6 GB for large-v3-turbo) once, reporting (bytes done, bytes total)."""
    import huggingface_hub
    from faster_whisper.utils import _MODELS, download_model

    repo = _MODELS.get(name, name)
    done = threading.Event()
    if progress is not None:
        def watch():
            try:
                info = huggingface_hub.HfApi().model_info(repo, files_metadata=True)
                total = sum(f.size or 0 for f in info.siblings if f.rfilename == "model.bin" or
                            f.rfilename.endswith(".json") or f.rfilename.startswith("vocabulary"))
            except Exception:
                total = 0
            folder = Path(huggingface_hub.constants.HF_HUB_CACHE) / ("models--" + repo.replace("/", "--"))
            while not done.wait(1):
                size = sum(f.stat().st_size for f in folder.rglob("*") if f.is_file()) if folder.exists() else 0
                progress(size, total)

        threading.Thread(target=watch, daemon=True).start()
    try:
        download_model(name)
    finally:
        done.set()


def _load_model(name: str, device: str):
    from faster_whisper import WhisperModel

    model = None
    if device in ("auto", "cuda"):
        try:
            model = WhisperModel(name, device="cuda", compute_type="float16")
        except Exception as e:
            if device == "cuda":
                raise
            log.info("CUDA not available (%s), using CPU", e)
    if model is None:
        model = WhisperModel(name, device="cpu", compute_type="int8", cpu_threads=CPU_THREADS)
    _reuse_language_detection_encoding(model)
    return model


def _reuse_language_detection_encoding(model) -> None:
    """detect_language() and transcribe() each run the expensive audio encoder on the same window (their
    inputs differ only in padding). After model.remember_next_encoding(), the next encoding is kept and
    handed to the following encode call instead of computing it again."""
    encode = model.encode
    state: dict = {"store": False, "out": None}

    def encode_once(features):
        if state["out"] is not None:
            out, state["out"] = state["out"], None
            return out
        out = encode(features)
        if state["store"]:
            state["store"], state["out"] = False, out
        return out

    model.encode = encode_once
    model.remember_next_encoding = lambda: state.update(store=True, out=None)


def _get_model(name: str, device: str):
    if _loaded.get("key") != (name, device):
        _loaded.clear()
        _loaded.update(key=(name, device), model=_load_model(name, device))
    return _loaded["model"]


# ---------- audio ----------
class _Window:
    """Speech clips packed into one Whisper window (silences removed), with a map back to track time.

    Whisper pads every input to 30 s, so transcribing 3-second clips one by one costs ten times more than
    packing them together.
    """

    GAP = SR // 5  # 0.2 s of silence between packed clips

    def __init__(self):
        self.spans: list[tuple[int, int]] = []  # (start, end) in the track, samples
        self.length = 0

    def add(self, a: int, b: int) -> None:
        self.spans.append((a, b))
        self.length += (b - a) + self.GAP

    def audio(self, track: np.ndarray, offset: int = 0) -> np.ndarray:
        """`track` holds the track from sample `offset` on."""
        gap = np.zeros(self.GAP, dtype=np.float32)
        return np.concatenate([part for a, b in self.spans for part in (track[a - offset:b - offset], gap)])

    def track_time(self, t: float) -> float:
        """Seconds inside the packed window -> seconds in the track."""
        pos = int(t * SR)
        for a, b in self.spans:
            if pos <= (b - a) + self.GAP:
                return (a + min(pos, b - a)) / SR
            pos -= (b - a) + self.GAP
        return self.spans[-1][1] / SR


def _speech_spans(audio: np.ndarray, offset: int) -> list[tuple[int, int]]:
    """Speech clips (Silero VAD) in track samples."""
    from faster_whisper.vad import VadOptions, get_speech_timestamps

    spans = get_speech_timestamps(
        audio, VadOptions(min_silence_duration_ms=500, speech_pad_ms=200, max_speech_duration_s=MAX_CHUNK_S)
    )
    return [(s["start"] + offset, s["end"] + offset) for s in spans if s["end"] - s["start"] >= SR // 4]


def _pack(spans: list[tuple[int, int]]) -> list[_Window]:
    """Pack clips in order into windows of at most MAX_CHUNK_S (gaps included, so always < 30 s)."""
    windows: list[_Window] = []
    for a, b in spans:
        if not windows or windows[-1].length + (b - a) > MAX_CHUNK_S * SR:
            windows.append(_Window())
        windows[-1].add(a, b)
    return windows


class _GrowingWav:
    """Reads a 16-bit PCM WAV that the recorder is still writing, as 16 kHz mono float32."""

    HEADER = 44  # Python's wave module writes a plain 44-byte header

    def __init__(self, path: Path):
        self.path = path
        self.rate = self.channels = 0
        self.read_bytes = 0

    def read_new(self, final: bool = False) -> np.ndarray:
        from .resample import to_16k

        if not self.rate:
            import wave

            try:
                with wave.open(str(self.path), "rb") as w:
                    self.rate, self.channels = w.getframerate(), w.getnchannels()
            except (OSError, EOFError, wave.Error):
                return np.zeros(0, dtype=np.float32)  # header not written yet
        frame = 2 * self.channels
        with open(self.path, "rb") as f:
            f.seek(self.HEADER + self.read_bytes)
            raw = f.read()
        raw = raw[: len(raw) // frame * frame]
        if not raw:
            return np.zeros(0, dtype=np.float32)
        self.read_bytes += len(raw)
        audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        if self.channels > 1:
            audio = audio.reshape(-1, self.channels).mean(axis=1)
        return to_16k(audio, self.rate)


class _Track:
    """Audio of one track not yet transcribed: buffer starts at track sample `offset`."""

    def __init__(self, key: str, path: Path):
        self.key = key
        self.wav = _GrowingWav(path)
        self.buffer = np.zeros(0, dtype=np.float32)
        self.offset = 0

    @property
    def end(self) -> int:
        return self.offset + len(self.buffer)

    def pull(self) -> None:
        new = self.wav.read_new()
        if len(new):
            self.buffer = np.concatenate([self.buffer, new])

    def drop_before(self, sample: int) -> None:
        cut = max(0, min(sample - self.offset, len(self.buffer)))
        self.buffer = self.buffer[cut:]
        self.offset += cut


# ---------- transcription ----------
def _pick_language(model, chunk: np.ndarray, allowed: set[str]) -> str:
    model.remember_next_encoding()  # transcribe() reuses this encoding
    _, _, probs = model.detect_language(chunk)
    score: dict[str, float] = {}
    for code, p in probs:
        lang = LANGUAGE_MAP.get(code, code)
        if lang in allowed:
            score[lang] = score.get(lang, 0.0) + p
    if not score:
        return next(iter(sorted(allowed)))
    if "en" in score and len(score) > 1 and score["en"] < ENGLISH_MIN_PROB:
        score.pop("en")
    return max(score, key=score.get)


def _transcribe_window(model, window: _Window, audio: np.ndarray, offset: int, allowed: set[str],
                       prompts: dict) -> list:
    """`audio` holds the track from sample `offset` on. -> [(start_s, end_s, text)] in track time."""
    chunk = window.audio(audio, offset)
    lang = _pick_language(model, chunk, allowed)
    segments, _ = model.transcribe(
        chunk,
        language=lang,
        initial_prompt=prompts.get(lang),
        condition_on_previous_text=False,  # avoids repetition loops on code-switched speech
        beam_size=5,
        # Hindi/Gujarati always scores "uncertain"; fewer retries keep it from decoding each clip 6 times
        temperature=(0.0, 0.4),
        log_prob_threshold=-1.5,
        vad_filter=False,  # already cut at speech
    )
    out = []
    for seg in segments:
        text = seg.text.strip()
        if not text or (seg.no_speech_prob > 0.6 and seg.avg_logprob < -1.0):
            continue
        if seg.compression_ratio > 2.6 or text in prompts.get(lang, ""):
            continue  # repetition loop, or the prompt echoed back
        if out and out[-1][2] == text:
            continue
        out.append((window.track_time(seg.start), window.track_time(seg.end), text))
    return out


class LiveTranscriber:
    """Transcribes the recorder's WAV files while they are being written.

    start() runs in the background during the meeting; finish() transcribes what is left and returns
    {track: [(start_s, end_s, text)]}. Can also be used on finished files: just call finish().
    """

    POLL_S = 10
    MIN_WINDOW_S = 20  # wait for this much speech before transcribing (fewer, fuller windows = faster)
    MAX_WAIT_S = 90  # ...but don't let speech wait longer than this

    def __init__(self, tracks: dict[str, Path], model_name: str, device: str, hint_words: list[str],
                 languages: tuple[str, ...] = ("en", "hi", "gu"), progress: Callable[[str], None] = log.info):
        self.tracks = [_Track(k, Path(p)) for k, p in tracks.items()]
        self.model_name, self.device = model_name, device
        self.allowed = set(languages)
        hints = ", ".join(hint_words[:40])
        self.prompts = {lang: p + (" " + hints if hints else "") for lang, p in PROMPTS.items()}
        self.progress = progress
        self.results: dict[str, list] = {t.key: [] for t in self.tracks}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> "LiveTranscriber":
        _set_low_priority(True)
        self._thread = threading.Thread(target=self._run, daemon=True, name="LiveTranscriber")
        self._thread.start()
        return self

    def cancel(self) -> None:
        self._stop.set()
        _set_low_priority(False)

    def finish(self) -> dict[str, list]:
        self._stop.set()
        if self._thread:
            self._thread.join()  # at most the window in progress
        _set_low_priority(False)
        with _model_lock:
            model = _get_model(self.model_name, self.device)
            for t in self.tracks:
                t.pull()
                self._process(model, t, final=True)
            _loaded.clear()  # free ~1.5 GB of RAM until the next meeting
        return self.results

    def _run(self) -> None:
        try:
            while not self._stop.wait(self.POLL_S):
                with _model_lock:
                    if self._stop.is_set():
                        return
                    model = _get_model(self.model_name, self.device)
                    for t in self.tracks:
                        t.pull()
                        self._process(model, t, final=False)
        except Exception:
            log.exception("Live transcription failed; the rest is transcribed after the meeting")

    def _process(self, model, track: _Track, final: bool) -> None:
        if not len(track.buffer):
            return
        spans = _speech_spans(track.buffer, track.offset)
        if not final:
            # the last clip may still be going on: leave it for the next round
            spans = [(a, b) for a, b in spans if b < track.end - SR]
            if not spans:
                if len(track.buffer) > 60 * SR:
                    track.drop_before(track.end - 5 * SR)  # long silence
                return
        windows = _pack(spans)
        for i, window in enumerate(windows):
            last = i == len(windows) - 1
            if not final and last:
                waited = (track.end - window.spans[0][0]) / SR
                if window.length < self.MIN_WINDOW_S * SR and waited < self.MAX_WAIT_S:
                    track.drop_before(window.spans[0][0])
                    return
            if not final and self._stop.is_set():
                return  # finish() takes over from here
            started = time.monotonic()
            self.results[track.key] += _transcribe_window(model, window, track.buffer, track.offset,
                                                          self.allowed, self.prompts)
            track.drop_before(window.spans[-1][1])
            self.progress(f"Transcribed {track.key} up to {_fmt(window.spans[-1][1] / SR)} "
                          f"({'final' if final else 'live'}, {time.monotonic() - started:.0f}s)")
        if final:
            track.drop_before(track.end)



def format_transcript(results: dict[str, list], labels: dict[str, str],
                      speaker_for: dict[str, Callable[[float, float], str]] | None = None) -> str:
    """Merge the tracks into "[mm:ss] Speaker: text" lines ordered by time.

    `speaker_for[track](start, end)` may return a person's name (e.g. from the Teams screen) to use instead
    of the track's label.
    """
    speaker_for = speaker_for or {}
    lines = []
    for key, segments in results.items():
        for start, end, text in segments:
            who = speaker_for.get(key, lambda *_: "")(start, end) or labels.get(key, key)
            lines.append((start, f"[{_fmt(start)}] {who}: {text}"))
    lines.sort(key=lambda x: x[0])
    return "\n".join(text for _, text in lines)


def transcribe_tracks(tracks: dict[str, Path], labels: dict[str, str], model_name: str, device: str,
                      hint_words: list[str], progress: Callable[[str], None] = log.info,
                      languages: tuple[str, ...] = ("en", "hi", "gu")) -> str:
    """Transcribe finished recordings in one go."""
    live = LiveTranscriber(tracks, model_name, device, hint_words, languages, progress)
    return format_transcript(live.finish(), labels)
