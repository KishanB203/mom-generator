"""System tray app. Left-click the tray icon (or press the hotkey) to start recording; click again to stop.
After stopping, everything runs by itself: transcribe -> Claude writes the MoM -> saved as
<output_dir>\\DD_MM_YYYY_<meeting name>.txt and opened in Notepad for your final review.

Threads: Tk owns the main thread (dialogs only); pystray, the hotkey listener and workers run in background
threads and hand UI work to Tk through `self.ui(...)`.
"""

import logging
import os
import queue
import shutil
import sys
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox

import pystray
from PIL import Image, ImageDraw

from . import APP_NAME
from .config import APP_DIR, LOG_PATH, RECORDINGS_DIR, Settings, get_api_key, start_with_windows
from .mom import (MoMError, build_user_message, clean_and_translate, cli_signed_in, find_claude_cli,
                  generate_mom, meeting_context)
from .recorder import AudioDeviceError, Recorder, room_microphone
from .screen import MeetingDetector, MeetingWatcher, looks_like_name, screen_summary, speaker_at
from .teams import detect_meeting_name, meeting_name_from_title, safe_filename
from .transcripts import read_transcript_file
from .setup_window import GuideWindow, SetupWindow, promote_tray_icon
from .ui import PasteDialog, SettingsDialog

log = logging.getLogger(__name__)

BLUE, RED, ORANGE = (37, 99, 235), (220, 38, 38), (234, 140, 0)


def make_icon(color=BLUE, dot: bool = False, size: int = 64) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((2, 2, size - 2, size - 2), radius=size // 5, fill=color)
    # simple "document lines" glyph
    for i, w in enumerate((0.62, 0.62, 0.42)):
        y = int(size * (0.30 + i * 0.18))
        d.rounded_rectangle((int(size * 0.2), y, int(size * (0.2 + w)), y + size // 10), radius=size // 20, fill="white")
    if dot:
        d.ellipse((size - 26, 4, size - 4, 26), fill="white")
        d.ellipse((size - 22, 8, size - 8, 22), fill=color)
    return img


def _write_note(path: Path, text: str) -> None:
    path.write_text(text.replace("\r\n", "\n"), encoding="utf-8-sig")  # BOM: Notepad shows all scripts correctly


def _speaker_timeline(screen: dict, me: str = "") -> list:
    """Who Teams showed as speaking over time (falls back to live-caption speakers), for naming the
    speaker-audio track. You are never on that track, so your own name is left out."""
    speaking = screen.get("speaking", [])
    if not any(who for _, who in speaking):
        speaking = []
        for t, spk, _text in screen.get("captions", []):
            if looks_like_name(spk):
                # a caption shows up a moment after the words were said
                speaking.append((max(0.0, t - 3), spk))
    timeline = []
    for t, who in speaking:
        who = "" if me and who == me else who
        if not timeline or timeline[-1][1] != who:
            timeline.append((t, who))
    return timeline


class Job:
    """One meeting: its recording folder, detected name and inputs."""

    def __init__(self, meeting_time: datetime | None = None):
        self.meeting_time = meeting_time or datetime.now()
        self.folder = RECORDINGS_DIR / f"{self.meeting_time:%Y-%m-%d_%H%M%S}"
        self.folder.mkdir(parents=True, exist_ok=True)
        self.meeting_name = ""
        self.transcript = ""
        self.teams_transcript = ""
        self.watcher: MeetingWatcher | None = None
        self.live = None  # LiveTranscriber, transcribing during the meeting
        self.screen: dict = {}  # what the watcher read from the Teams meeting window
        self.in_person = False  # one laptop microphone in a room, no Teams
        self.auto = False  # started by joining a Teams meeting: stops by itself when you leave it


class TrayApp:
    def __init__(self):
        self.settings = Settings.load()
        self.root = tk.Tk()
        self.root.withdraw()
        self._ui_queue: queue.Queue = queue.Queue()
        self.recorder: Recorder | None = None
        self.job: Job | None = None
        self.processing = 0
        self.hotkey_listener = None
        self._toggle_lock = threading.Lock()
        self.detector: MeetingDetector | None = None
        # you stopped/cancelled during a Teams meeting: don't start again until that meeting ends
        self._skip_this_meeting = False
        self._state_lock = threading.Lock()
        self._setup_window: SetupWindow | None = None
        self.icon = pystray.Icon("mom_generator", make_icon(), APP_NAME, menu=self._menu())

    # ---------- plumbing ----------
    def ui(self, fn, *args) -> None:
        self._ui_queue.put((fn, args))

    def _pump(self) -> None:
        while True:
            try:
                fn, args = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                fn(*args)
            except Exception:
                log.exception("UI task failed")
        self.root.after(100, self._pump)

    def notify(self, message: str) -> None:
        log.info("Notify: %s", message)
        try:
            self.icon.notify(message[:250], APP_NAME)
        except Exception:
            pass

    def error(self, message: str) -> None:
        log.error(message)
        self.ui(self._show_error, message)

    def _show_error(self, message: str) -> None:
        # a hidden topmost parent keeps the dialog from opening behind other windows
        parent = tk.Toplevel(self.root)
        parent.withdraw()
        parent.attributes("-topmost", True)
        messagebox.showerror(APP_NAME, message, parent=parent)
        parent.destroy()

    @staticmethod
    def _later(seconds: float, fn) -> None:
        t = threading.Timer(seconds, fn)
        t.daemon = True
        t.start()

    def _is_recording(self, *_) -> bool:
        return self.recorder is not None and self.recorder.is_recording

    def _menu(self) -> pystray.Menu:
        return pystray.Menu(
            pystray.MenuItem(
                lambda item: f"Stop && create MoM ({int(self.recorder.elapsed() // 60)} min)" if self._is_recording() else "Start recording",
                self.toggle_recording,
                default=True,
            ),
            pystray.MenuItem("Record in-person meeting (laptop mic)", lambda: self._locked(self.start_recording, True),
                             visible=lambda item: not self._is_recording()),
            pystray.MenuItem("Cancel recording", self.cancel_recording, visible=self._is_recording),
            pystray.MenuItem("Auto-record Teams meetings", self.toggle_auto_record,
                             checked=lambda item: self.settings.auto_record_teams),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Create MoM from Teams transcript file...", lambda: self.ui(self.from_file)),
            pystray.MenuItem("Create MoM from pasted text...", lambda: self.ui(self.from_paste)),
            pystray.MenuItem("Open notes folder", lambda: self.ui(self.open_folder)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("How it works", lambda: self.ui(self.show_guide)),
            pystray.MenuItem("Sign in to Claude...", lambda: self.ui(self.show_setup)),
            pystray.MenuItem("Settings (optional)...", lambda: self.ui(self.open_settings)),
            pystray.MenuItem("Start with Windows", lambda: start_with_windows(not start_with_windows()),
                             checked=lambda item: start_with_windows()),
            pystray.MenuItem("Open log", lambda: os.startfile(LOG_PATH) if LOG_PATH.exists() else None),
            pystray.MenuItem("Quit", lambda: self.ui(self.quit)),
        )

    def _refresh_icon(self) -> None:
        if self._is_recording():
            self.icon.icon, self.icon.title = make_icon(RED, dot=True), f"{APP_NAME} - recording (click to stop)"
        elif self.processing:
            self.icon.icon, self.icon.title = make_icon(ORANGE), f"{APP_NAME} - creating MoM..."
        else:
            self.icon.icon, self.icon.title = make_icon(), f"{APP_NAME} - click to record"
        self.icon.update_menu()

    # ---------- recording ----------
    def _locked(self, fn, *args) -> None:
        # tray click, hotkey and the meeting detector arrive on different threads
        if not self._toggle_lock.acquire(blocking=False):
            return
        try:
            fn(*args)
        finally:
            self._toggle_lock.release()

    def toggle_recording(self, *_):
        self._locked(lambda: self.stop_recording(manual=True) if self._is_recording() else self.start_recording())

    def toggle_auto_record(self, *_) -> None:
        self.settings.auto_record_teams = not self.settings.auto_record_teams
        self.settings.save()
        self.icon.update_menu()
        self.notify("Teams meetings will be recorded automatically." if self.settings.auto_record_teams
                    else "Automatic recording is off. Click the tray icon to record.")

    def _in_teams_meeting(self) -> bool:
        return bool((self.detector and self.detector.in_meeting) or detect_meeting_name())

    # ---------- automatic recording of Teams meetings ----------
    def _on_meeting_joined(self, title: str) -> None:
        if not self.settings.auto_record_teams or self._skip_this_meeting or self._is_recording():
            return
        self._locked(self.start_recording, False, True)

    def _on_meeting_left(self) -> None:
        self._skip_this_meeting = False
        if self._is_recording() and self.job is not None and self.job.auto:
            self._locked(self.stop_recording)

    def start_recording(self, in_person: bool | None = None, auto: bool = False) -> None:
        """in_person=None: a Teams meeting if one is open, otherwise an in-person meeting (laptop mic)."""
        if self._is_recording():
            return
        s = self.settings
        if in_person is None:
            in_person = not self._in_teams_meeting()
        try:
            self.job = Job()
            self.job.in_person, self.job.auto = in_person, auto
            if in_person:
                self.recorder = Recorder(room_microphone(s.room_mic_device), mic_only=True)
            else:
                self.recorder = Recorder(s.mic_device, s.speaker_device)
            self.recorder.start(self.job.folder)
        except AudioDeviceError as e:
            self.recorder = None
            self.error(str(e))
            return
        except Exception as e:
            self.recorder = None
            log.exception("Could not start recording")
            self.error(f"Could not start recording: {e}")
            return
        if in_person:
            self.job.meeting_name = "In-person meeting"
        else:
            self.job.meeting_name = detect_meeting_name()
            self.job.watcher = MeetingWatcher(self.job.folder).start()
        from .transcriber import LiveTranscriber

        # transcribes while the meeting runs, so only the last minute is left after it ends
        self.job.live = LiveTranscriber(
            {k: t.path for k, t in self.recorder.tracks.items()}, s.whisper_model, s.whisper_device,
            s.member_names() + s.glossary_terms(), s.language_codes(),
        ).start()
        self._refresh_icon()
        if in_person:
            mic = self.recorder.tracks["mic"].device["name"]
            msg = f"Recording the room with '{mic}'. Click the tray icon again when the meeting ends."
        elif auto:
            msg = (f"Teams meeting detected - recording '{self.job.meeting_name or 'meeting'}'. It stops by itself "
                   "when you leave. Right-click > Cancel recording to discard.")
        elif self.job.meeting_name:
            msg = f"Recording '{self.job.meeting_name}'. Click the tray icon again when it ends."
        else:
            msg = "Recording. Click the tray icon again when the meeting ends."
        if not get_api_key() and not find_claude_cli(self.settings.claude_cli_path):
            msg += " Warning: Claude CLI not found - the MoM step will fail (audio is kept)."
        self.notify((msg + " " + " ".join(self.recorder.warnings)).strip())
        self._later(8, self._check_mic)
        self._later(30, self._tick)

    def _tick(self) -> None:
        """While recording: refresh the elapsed time and retry meeting-name detection (Teams window may open late)."""
        if self._is_recording():
            if self.job is not None and not self.job.meeting_name:
                self.job.meeting_name = detect_meeting_name()
            self.icon.update_menu()
            self._later(30, self._tick)

    def _check_mic(self) -> None:
        if self._is_recording() and self.recorder.mic_peak() == 0:
            self.notify("Your microphone is silent. Check that it isn't muted and that Windows microphone privacy allows desktop apps.")

    def cancel_recording(self, *_):
        if self.recorder is None:
            return
        if self.job is not None and not self.job.in_person and self._in_teams_meeting():
            self._skip_this_meeting = True
        self.recorder.stop()
        self.recorder = None
        if self.job is not None:
            if self.job.watcher:
                self.job.watcher.stop()
            if self.job.live:
                self.job.live.cancel()
            shutil.rmtree(self.job.folder, ignore_errors=True)
            self.job = None
        self._refresh_icon()
        self.notify("Recording cancelled and deleted.")

    def stop_recording(self, manual: bool = False) -> None:
        recorder, job = self.recorder, self.job
        if manual and not job.in_person and self._in_teams_meeting():
            self._skip_this_meeting = True  # you stopped it: don't restart until this meeting ends
        if job.auto and not manual and recorder.elapsed() < 60:
            # joined by mistake / a quick call: not worth a MoM
            self.cancel_recording()
            return
        self.recorder, self.job = None, None
        try:
            tracks = recorder.stop()
        except Exception as e:
            log.exception("Stop failed")
            self.error(f"Error while stopping the recording: {e}")
            tracks = {}
        if job.watcher:
            job.screen = job.watcher.stop()
        # the window with the Leave button is the meeting; other Teams windows can be chats or the share bar
        job.meeting_name = (meeting_name_from_title(job.screen.get("meeting_title", ""))
                            or job.meeting_name or detect_meeting_name())
        if not tracks:
            self._refresh_icon()
            self.error("Nothing was recorded.")
            return
        self.notify("Meeting recorded. Creating the MoM - it will open when ready (a few minutes).")
        self._start_worker(job, tracks)

    # ---------- processing ----------
    def _start_worker(self, job: Job, tracks: dict) -> None:
        with self._state_lock:
            self.processing += 1
        self._refresh_icon()
        threading.Thread(target=self._process, args=(job, tracks), daemon=True).start()

    def _process(self, job: Job, tracks: dict) -> None:
        try:
            self._process_inner(job, tracks)
        except Exception as e:
            log.exception("Processing failed")
            self.error(f"Unexpected error: {e}\nFiles are kept in {job.folder}")
        finally:
            with self._state_lock:
                self.processing -= 1
            self._refresh_icon()

    def _process_inner(self, job: Job, tracks: dict) -> None:
        """Recording -> raw transcript -> clean transcript (who said what, each language in its own script)
        -> English translation -> MoM. Every stage is saved before the next starts, so nothing is lost."""
        s = self.settings
        name = job.meeting_name or ("In-person meeting" if job.in_person else s.default_meeting_name)
        stem = self._note_stem(job, name)
        english = ""
        if tracks:
            from .transcriber import LiveTranscriber, format_transcript

            self.icon.title = f"{APP_NAME} - transcribing the last part..."
            me = s.my_name or job.screen.get("me", "") or "Me (recorder)"
            labels = {"mic": "Room" if job.in_person else me, "system": "Remote participants"}
            timeline = _speaker_timeline(job.screen, s.my_name or job.screen.get("me", ""))
            try:
                live = job.live or LiveTranscriber(tracks, s.whisper_model, s.whisper_device,
                                                   s.member_names() + s.glossary_terms(), s.language_codes())
                job.transcript = format_transcript(
                    live.finish(), labels,
                    {"system": lambda a, b: speaker_at(timeline, a, b)} if timeline else None,
                )
            except Exception as e:
                log.exception("Transcription failed")
                self.error(f"Transcription failed: {e}\nThe audio is kept in {job.folder}")
                return
            (job.folder / "transcript.txt").write_text(job.transcript, encoding="utf-8")
            if not s.keep_audio:
                for p in tracks.values():
                    p.unlink(missing_ok=True)
            if not job.transcript.strip():
                self.error("No speech was detected in the recording. Check your microphone/speaker settings.")
                return

            transcript_path = self._out_dir() / f"{stem}_transcript.txt"
            header = f"TRANSCRIPT\nMeeting: {name}\nDate: {job.meeting_time:%d %B %Y, %H:%M}\n\n"
            _write_note(transcript_path, header + "----- RAW (speech recognition, not yet cleaned up) -----\n"
                        + job.transcript)
            context = meeting_context(s, name, me, screen_summary(job.screen), job.in_person)
            try:
                self.icon.title = f"{APP_NAME} - Claude is writing the transcript and English version..."
                job.transcript, english = clean_and_translate(s, job.transcript, context)
                _write_note(transcript_path, header + "----- AS SPOKEN -----\n" + job.transcript
                            + "\n\n----- ENGLISH -----\n" + english)
            except MoMError as e:
                # keep going with what we have: the MoM step reads mixed-language text too
                log.error("Transcript clean-up/translation failed: %s", e)

        self.icon.title = f"{APP_NAME} - Claude is writing the MoM..."
        message = build_user_message(s, job.meeting_time, name, job.transcript, job.teams_transcript,
                                     self._previous_mom(), screen_summary(job.screen), english, job.in_person)
        try:
            mom_text, failed = generate_mom(s, message), False
        except MoMError as e:
            log.error("MoM generation failed: %s", e)
            failed = True
            mom_text = (
                f"MoM COULD NOT BE CREATED: {e}\n\nFix the problem, then use the tray menu "
                f"'Create MoM from pasted text' with the transcript below"
                + (f" or with {stem}_transcript.txt" if tracks else "") + ".\n\n----- TRANSCRIPT -----\n"
                + (english or job.transcript or job.teams_transcript)
            )

        path = self._out_dir() / (stem + ("_FAILED" if failed else "") + ".txt")
        _write_note(path, mom_text)
        if s.open_after_save:
            os.startfile(path)
        self.notify(("MoM FAILED - see " if failed else "MoM saved: ") + path.name)

    def _out_dir(self) -> Path:
        out = Path(self.settings.output_dir)
        out.mkdir(parents=True, exist_ok=True)
        return out

    def _note_stem(self, job: Job, name: str) -> str:
        """DD_MM_YYYY_<meeting>; adds the time when that meeting already has notes today."""
        stem = f"{job.meeting_time:%d_%m_%Y}_{safe_filename(name)}"
        if any(p.name.startswith(stem) for p in self._out_dir().glob("*.txt")):
            stem += f"_{job.meeting_time:%H%M}"
        return stem

    def _previous_mom(self) -> str:
        if not self.settings.include_previous_mom:
            return ""
        out = Path(self.settings.output_dir)
        notes = sorted((p for p in out.glob("*.txt") if "_FAILED" not in p.name and "_transcript" not in p.name), key=lambda p: p.stat().st_mtime, reverse=True)
        if notes and datetime.now().timestamp() - notes[0].stat().st_mtime < 7 * 86400:
            return notes[0].read_text(encoding="utf-8-sig", errors="replace")[:20000]
        return ""

    # ---------- other entry points ----------
    def _from_text(self, text: str, name: str = "") -> None:
        job = Job()
        job.teams_transcript = text
        job.meeting_name = name
        (job.folder / "teams_transcript.txt").write_text(text, encoding="utf-8")
        self.notify("Creating the MoM - it will open when ready.")
        self._start_worker(job, {})

    def from_file(self) -> None:
        path = filedialog.askopenfilename(
            title="Teams transcript (.vtt / .docx / .txt)",
            filetypes=[("Teams transcript", "*.vtt *.docx *.txt"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            text = read_transcript_file(Path(path))
        except Exception as e:
            self._show_error(f"Could not read {path}: {e}")
            return
        self._from_text(text, Path(path).stem)

    def from_paste(self) -> None:
        PasteDialog(self.root, self._from_text)

    # ---------- setup / guide ----------
    def show_setup(self) -> None:
        """The setup window: Claude Code, Claude sign-in, speech model. Opens by itself on the first start."""
        if self._setup_window is not None and self._setup_window.win.winfo_exists():
            self._setup_window.win.lift()
            return
        first = not self.settings.setup_done
        self._setup_window = SetupWindow(self.root, self.settings, lambda: self._setup_finished(first))

    def _setup_finished(self, first: bool) -> None:
        self._setup_window = None
        if first:
            self.show_guide()

    def show_guide(self) -> None:
        GuideWindow(self.root, self.settings, {"open_folder": self.open_folder, "settings": self.open_settings})

    def _check_sign_in(self) -> None:
        if self.settings.claude_backend == "api" or (self.settings.claude_backend == "auto" and get_api_key()):
            return
        if not cli_signed_in(self.settings):
            self.notify("Claude is not signed in - needed to write the MoM. Finish the setup window.")
            self.ui(self.show_setup)

    def open_folder(self) -> None:
        folder = Path(self.settings.output_dir)
        folder.mkdir(parents=True, exist_ok=True)
        os.startfile(folder)

    def open_settings(self) -> None:
        SettingsDialog(self.root, self.settings, self._settings_saved)

    def _settings_saved(self) -> None:
        self._start_hotkey()
        self.notify("Settings saved.")

    def _start_hotkey(self) -> None:
        if self.hotkey_listener is not None:
            self.hotkey_listener.stop()
            self.hotkey_listener = None
        if not self.settings.hotkey.strip():
            return
        try:
            from pynput import keyboard

            self.hotkey_listener = keyboard.GlobalHotKeys({self.settings.hotkey: self.toggle_recording})
            self.hotkey_listener.start()
        except Exception as e:
            log.exception("Hotkey failed")
            self.notify(f"Hotkey '{self.settings.hotkey}' could not be registered: {e}")

    def quit(self) -> None:
        if self._is_recording():
            if not messagebox.askyesno(APP_NAME, "Recording in progress. Stop and discard it?"):
                return
            self.cancel_recording()
        if self.processing and not messagebox.askyesno(APP_NAME, "A MoM is still being created. Quit anyway?"):
            return
        if self.hotkey_listener is not None:
            self.hotkey_listener.stop()
        if self.detector is not None:
            self.detector.stop()
        self.icon.stop()
        self.root.quit()

    def run(self) -> None:
        self.icon.run_detached()
        self._start_hotkey()
        self.root.after(100, self._pump)
        self.detector = MeetingDetector(self._on_meeting_joined, self._on_meeting_left)
        ready = "Ready. Teams meetings are recorded automatically; " if self.settings.auto_record_teams else "Ready. "
        self.root.after(1500, lambda: self.notify(
            ready + f"click the tray icon or press {self.settings.hotkey} to record an in-person meeting."))
        if not self.settings.setup_done:
            self.root.after(500, self.show_setup)
            if getattr(sys, "frozen", False):  # installed app: show the icon on the taskbar, not under ^
                self._later(4, lambda: promote_tray_icon(Path(sys.executable).name))
        else:
            self._later(3, self._check_sign_in)
        self.root.mainloop()


def _single_instance() -> bool:
    import win32api
    import win32event
    import winerror

    global _mutex
    _mutex = win32event.CreateMutex(None, False, "MoMGenerator_SingleInstance")
    return win32api.GetLastError() != winerror.ERROR_ALREADY_EXISTS


def _selftest() -> int:
    """MoMGenerator.exe --selftest: checks that the packaged app has everything it needs (results in the log)."""
    import numpy as np

    def teams_reader():
        from .screen import _uia

        _uia()

    def audio_devices():
        from .recorder import list_devices

        return list_devices()

    def voice_detection():
        from .transcriber import _speech_spans

        return _speech_spans(np.zeros(16000, dtype=np.float32), 0)

    def speech_engine():
        from faster_whisper import WhisperModel

        model = WhisperModel("tiny", device="cpu", compute_type="int8")
        return list(model.transcribe(np.zeros(16000, dtype=np.float32))[0])

    failed = 0
    for check in (teams_reader, audio_devices, voice_detection, speech_engine, find_claude_cli):
        try:
            log.info("selftest %s: ok %s", check.__name__, str(check())[:200])
        except Exception:
            failed += 1
            log.exception("selftest %s: FAILED", check.__name__)
    return failed


def main() -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(LOG_PATH, encoding="utf-8")],
    )
    if sys.stdout is None or sys.stderr is None:  # windowed exe: libraries that print must not crash
        sys.stdout = sys.stderr = open(os.devnull, "w", encoding="utf-8")
    sys.excepthook = lambda *exc: log.critical("Uncaught error", exc_info=exc)
    threading.excepthook = lambda a: log.critical("Uncaught thread error", exc_info=(a.exc_type, a.exc_value, a.exc_traceback))
    if len(sys.argv) == 3 and sys.argv[1] == "--transcribe":
        # run.pyw --transcribe path\to\audio.wav  ->  writes audio.txt next to it
        from .transcriber import transcribe_tracks

        s, wav = Settings.load(), Path(sys.argv[2])
        text = transcribe_tracks({"audio": wav}, {"audio": "Speaker"}, s.whisper_model, s.whisper_device,
                                 s.member_names() + s.glossary_terms(), progress=log.info)
        wav.with_suffix(".txt").write_text(text, encoding="utf-8")
        return
    if len(sys.argv) == 2 and sys.argv[1] == "--selftest":
        sys.exit(_selftest())
    if not _single_instance():
        messagebox.showinfo(APP_NAME, "Already running - look for the icon in the taskbar tray (^ arrow).")
        return
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # crisp text on high-DPI screens
    except Exception:
        pass
    TrayApp().run()
