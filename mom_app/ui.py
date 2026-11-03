"""Tkinter dialogs: optional settings and paste-transcript. Nothing here is needed for the one-click flow."""

import threading
import tkinter as tk
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from . import APP_NAME
from .config import Settings, get_api_key, set_api_key


def _place(win: tk.Toplevel, w: int, h: int) -> None:
    sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
    w, h = min(w, sw - 40), min(h, sh - 80)
    win.geometry(f"{w}x{h}+{(sw - w) // 2}+{max(0, (sh - h) // 2 - 20)}")


def _bring_to_front(win: tk.Toplevel) -> None:
    win.deiconify()
    win.lift()
    win.attributes("-topmost", True)
    win.after(300, lambda: win.attributes("-topmost", False))
    win.focus_force()


class PasteDialog:
    def __init__(self, root: tk.Tk, on_submit):
        self.win = tk.Toplevel(root)
        self.win.title("Paste meeting transcript")
        _place(self.win, 800, 600)
        ttk.Label(self.win, text="Paste the Teams transcript (or your notes) below:", padding=8).pack(anchor="w")
        self.box = ScrolledText(self.win, wrap="word", font=("Consolas", 10))
        self.box.pack(fill="both", expand=True, padx=8)
        bar = ttk.Frame(self.win, padding=8)
        bar.pack(fill="x")
        ttk.Button(bar, text="Create MoM", command=lambda: self._submit(on_submit)).pack(side="right")
        ttk.Button(bar, text="Cancel", command=self.win.destroy).pack(side="right", padx=6)
        _bring_to_front(self.win)

    def _submit(self, on_submit) -> None:
        text = self.box.get("1.0", "end").strip()
        if not text:
            messagebox.showwarning(APP_NAME, "The transcript is empty.", parent=self.win)
            return
        self.win.destroy()
        on_submit(text)


class SettingsDialog:
    """Everything is optional. Team names and glossary make the MoM noticeably more accurate."""

    def __init__(self, root: tk.Tk, settings: Settings, on_save):
        from .recorder import list_devices

        self.settings = settings
        self.on_save = on_save
        self.win = tk.Toplevel(root)
        self.win.title(f"{APP_NAME} - Settings (all optional)")
        _place(self.win, 720, 780)

        f = ttk.Frame(self.win, padding=12)
        f.pack(fill="both", expand=True)
        f.columnconfigure(1, weight=1)
        self.vars: dict[str, tk.Variable | tk.Text] = {}
        row = 0

        def entry(label, key, show=None, value=None):
            nonlocal row
            ttk.Label(f, text=label).grid(row=row, column=0, sticky="w", pady=3, padx=(0, 8))
            var = tk.StringVar(value=getattr(settings, key) if value is None else value)
            ttk.Entry(f, textvariable=var, show=show).grid(row=row, column=1, sticky="ew", pady=3)
            self.vars[key] = var
            row += 1

        def combo(label, key, values):
            nonlocal row
            ttk.Label(f, text=label).grid(row=row, column=0, sticky="w", pady=3, padx=(0, 8))
            var = tk.StringVar(value=getattr(settings, key))
            ttk.Combobox(f, textvariable=var, values=values).grid(row=row, column=1, sticky="ew", pady=3)
            self.vars[key] = var
            row += 1

        def textbox(label, key, height):
            nonlocal row
            ttk.Label(f, text=label).grid(row=row, column=0, columnspan=2, sticky="w", pady=(8, 2))
            row += 1
            box = tk.Text(f, height=height, wrap="word", font=("Segoe UI", 10))
            box.insert("1.0", getattr(settings, key))
            box.grid(row=row, column=0, columnspan=2, sticky="ew")
            self.vars[key] = box
            row += 1

        def section(title):
            nonlocal row
            ttk.Label(f, text=title, font=("Segoe UI", 10, "bold")).grid(row=row, column=0, columnspan=2, sticky="w", pady=(12, 2))
            row += 1

        section("Meeting (improves accuracy)")
        entry("Your name", "my_name")
        entry("Manager name", "manager_name")
        entry("Project name", "project_name")
        textbox("Team members - one per line:  Full Name | role (optional)", "team_members", 5)
        textbox("Project glossary - module, client and feature names (comma or one per line)", "glossary", 3)

        section("Output")
        entry("Notes folder", "output_dir")
        entry("Name when no Teams meeting is found", "default_meeting_name")
        self.open_after = tk.BooleanVar(value=settings.open_after_save)
        ttk.Checkbutton(f, text="Open the MoM in Notepad when it is ready (final review)", variable=self.open_after).grid(row=row, column=0, columnspan=2, sticky="w")
        row += 1

        section("Claude access")
        combo("Use", "claude_backend", ["auto", "cli", "api"])
        ttk.Label(f, text="cli = your Claude login via Claude Code (no key)   api = API key   auto = api if a key is set",
                  foreground="#666").grid(row=row, column=0, columnspan=2, sticky="w")
        row += 1
        entry("API key (only for 'api')", "api_key", show="*", value=get_api_key())

        section("Audio")
        try:
            mics, speakers = list_devices()
        except Exception:
            mics, speakers = [], []
        combo("Teams microphone (empty = default)", "mic_device", [""] + mics)
        combo("Speaker to capture (empty = default)", "speaker_device", [""] + speakers)
        combo("In-person microphone (empty = laptop's built-in)", "room_mic_device", [""] + mics)
        combo("Speech model", "whisper_model", ["large-v3-turbo", "large-v3", "medium", "small"])
        entry("Meeting languages (en, hi, gu, mr, ...)", "languages")
        entry("Global hotkey (start/stop)", "hotkey")

        bar = ttk.Frame(self.win, padding=12)
        bar.pack(fill="x")
        self.test_label = ttk.Label(bar, text="")
        self.test_label.pack(side="left")
        ttk.Button(bar, text="Save", command=self.save).pack(side="right")
        ttk.Button(bar, text="Cancel", command=self.win.destroy).pack(side="right", padx=6)
        ttk.Button(bar, text="Test audio (3 s)", command=self.test_audio).pack(side="right", padx=6)
        _bring_to_front(self.win)

    def _value(self, key):
        v = self.vars[key]
        return v.get("1.0", "end").strip() if isinstance(v, tk.Text) else v.get().strip()

    def test_audio(self) -> None:
        import tempfile
        import time
        from pathlib import Path

        from .recorder import Recorder

        self.test_label.config(text="Recording 3 s - speak and play some sound...")
        mic, spk = self._value("mic_device"), self._value("speaker_device")

        def run():
            try:
                rec = Recorder(mic, spk)
                with tempfile.TemporaryDirectory() as tmp:
                    rec.start(Path(tmp))
                    time.sleep(3)
                    peaks = {k: t.peak for k, t in rec.tracks.items()}
                    rec.stop()
                mic_pct = peaks.get("mic", 0) * 100 // 32768
                sys_pct = peaks.get("system", 0) * 100 // 32768 if "system" in peaks else None
                msg = f"Mic {mic_pct}%  |  Speaker " + (f"{sys_pct}%" if sys_pct is not None else "unavailable")
                if mic_pct == 0:
                    msg += "  - mic is silent!"
            except Exception as e:
                msg = f"Audio test failed: {e}"
            self.win.after(0, lambda: self.test_label.config(text=msg))

        threading.Thread(target=run, daemon=True).start()

    def save(self) -> None:
        s = self.settings
        for key in ("my_name", "manager_name", "project_name", "team_members", "glossary", "output_dir",
                    "default_meeting_name", "claude_backend", "mic_device", "speaker_device", "room_mic_device",
                    "whisper_model", "languages", "hotkey"):
            setattr(s, key, self._value(key))
        s.open_after_save = self.open_after.get()
        try:
            set_api_key(self._value("api_key"))
            s.save()
        except Exception as e:
            messagebox.showerror(APP_NAME, f"Could not save settings: {e}", parent=self.win)
            return
        self.win.destroy()
        self.on_save()
