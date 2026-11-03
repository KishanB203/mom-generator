"""First-run setup (Claude Code, Claude sign-in, speech model) and the "How it works" guide."""

import logging
import os
import subprocess
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, simpledialog, ttk

from . import APP_NAME
from .config import Settings, get_api_key, set_api_key, start_with_windows
from .mom import cli_signed_in, find_claude_cli, open_cli_sign_in
from .ui import _bring_to_front, _place

log = logging.getLogger(__name__)

GREEN, GREY, RED = "#15803d", "#6b7280", "#b91c1c"
CLAUDE_INSTALL = ("irm https://claude.ai/install.ps1 | iex; Write-Host ''; "
                  "Read-Host 'Claude Code is installed. Press Enter to close this window'")

_model_download = {"thread": None, "done": 0, "total": 0, "error": ""}


def start_model_download(model_name: str) -> None:
    """Download the speech model in the background (once per PC). Safe to call repeatedly."""
    t = _model_download["thread"]
    if t is not None and t.is_alive():
        return

    def progress(done: int, total: int) -> None:
        _model_download.update(done=done, total=total)

    def run():
        from .transcriber import download_speech_model

        _model_download["error"] = ""
        try:
            download_speech_model(model_name, progress)
        except Exception as e:
            log.exception("Speech model download failed")
            _model_download["error"] = str(e) or type(e).__name__

    _model_download["thread"] = threading.Thread(target=run, daemon=True)
    _model_download["thread"].start()


def _gb(n: int) -> str:
    return f"{n / 1e9:.1f} GB"


class _Step:
    def __init__(self, parent, row: int, title: str):
        self.mark = ttk.Label(parent, text="○", width=2, font=("Segoe UI", 14), foreground=GREY)
        self.mark.grid(row=row, column=0, rowspan=2, sticky="n", padx=(0, 10), pady=(8, 0))
        ttk.Label(parent, text=title, font=("Segoe UI", 11, "bold")).grid(row=row, column=1, sticky="w", pady=(10, 0))
        self.detail = ttk.Label(parent, text="Checking...", foreground=GREY, wraplength=360)
        self.detail.grid(row=row + 1, column=1, sticky="w")
        self.button = ttk.Button(parent)
        self.button.grid(row=row, column=2, rowspan=2, sticky="e", padx=(10, 0))
        self.button.grid_remove()
        self.ok = False

    def set(self, ok: bool | None, detail: str, button: str = "", command=None) -> None:
        self.ok = bool(ok)
        self.mark.config(text={True: "✔", False: "✖", None: "…"}[ok],
                         foreground={True: GREEN, False: RED, None: GREY}[ok])
        self.detail.config(text=detail, foreground="#111827" if ok is False else GREY)
        if button:
            self.button.config(text=button, command=command)
            self.button.grid()
        else:
            self.button.grid_remove()


class SetupWindow:
    """Checks everything the app needs and walks you through what is missing.

    1. Claude Code installed   2. signed in to Claude (browser)   3. speech model downloaded.
    The window finishes as soon as Claude works; the model can keep downloading in the background.
    """

    def __init__(self, root: tk.Tk, settings: Settings, on_done):
        self.settings, self.on_done = settings, on_done
        self.win = tk.Toplevel(root)
        self.win.title(f"{APP_NAME} - Setup")
        _place(self.win, 600, 470)
        self.win.protocol("WM_DELETE_WINDOW", self._close)
        self._checking = False
        self._claude: tuple | None = None  # (claude.exe path, signed in) from the last background check
        self._alive = True

        f = ttk.Frame(self.win, padding=20)
        f.pack(fill="both", expand=True)
        f.columnconfigure(1, weight=1)
        ttk.Label(f, text=f"Welcome to {APP_NAME}", font=("Segoe UI", 16, "bold")).grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(f, text="A few one-time steps. Claude writes your MoM with your own Claude account.",
                  foreground=GREY).grid(row=1, column=0, columnspan=3, sticky="w", pady=(2, 8))
        self.cli = _Step(f, 2, "1. Claude Code")
        self.login = _Step(f, 4, "2. Sign in to Claude")
        self.model = _Step(f, 6, "3. Speech model")
        self.bar = ttk.Progressbar(f, mode="determinate", maximum=1000)
        self.bar.grid(row=8, column=1, columnspan=2, sticky="ew", pady=(6, 0))

        bottom = ttk.Frame(self.win, padding=(20, 0, 20, 16))
        bottom.pack(fill="x")
        self.note = ttk.Label(bottom, text="", foreground=GREY, wraplength=380)
        self.note.pack(side="left")
        self.finish = ttk.Button(bottom, text="Finish", command=self._finish, state="disabled")
        self.finish.pack(side="right")

        start_model_download(settings.whisper_model)
        self._check()
        self._tick_model()
        _bring_to_front(self.win)

    # ---------- Claude ----------
    def _uses_api_key(self) -> bool:
        return self.settings.claude_backend == "api" or (self.settings.claude_backend == "auto" and bool(get_api_key()))

    def _check(self) -> None:
        """Runs on the Tk thread: shows the last result and starts the next check in the background
        ('claude auth status' takes a few seconds; Tk must not be touched from that thread)."""
        if not self._alive:
            return
        if self._claude is not None:
            self._show_claude(*self._claude)
            self._claude = None
        if not self._checking:
            self._checking = True

            def run():
                exe = find_claude_cli(self.settings.claude_cli_path)
                signed = bool(exe) and cli_signed_in(self.settings)
                self._claude, self._checking = (exe, signed), False

            threading.Thread(target=run, daemon=True).start()
        self.win.after(500, self._check)

    def _show_claude(self, exe: str, signed: bool) -> None:
        if self._uses_api_key():
            self.cli.set(True, "Not needed: an Anthropic API key is saved.")
            self.login.set(True, "Using your API key.")
        elif not exe:
            self.cli.set(False, "Claude Code is not installed. It lets this app use your Claude account (no API key).",
                         "Install", self._install_cli)
            self.login.set(None, "After Claude Code is installed.", "Use API key", self._ask_api_key)
        else:
            self.cli.set(True, "Installed.")
            if signed:
                self.login.set(True, "Signed in.")
            else:
                self.login.set(False, "Click Sign in: your browser opens, log in with your Claude account and approve.",
                               "Sign in", self._sign_in)
        self._update_finish()

    def _install_cli(self) -> None:
        subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", CLAUDE_INSTALL],
                         creationflags=subprocess.CREATE_NEW_CONSOLE)
        self.cli.set(None, "Installing in the window that opened (about a minute)...")

    def _sign_in(self) -> None:
        try:
            open_cli_sign_in(self.settings)
            self.login.set(None, "Finish the sign-in in your browser. This turns green by itself when you're done.",
                           "Sign in again", self._sign_in)
        except Exception as e:
            messagebox.showerror(APP_NAME, str(e), parent=self.win)

    def _ask_api_key(self) -> None:
        key = simpledialog.askstring(APP_NAME, "Anthropic API key (from console.anthropic.com):", show="*", parent=self.win)
        if key and key.strip():
            set_api_key(key)
            self.settings.claude_backend = "auto"
            self.settings.save()

    # ---------- speech model ----------
    def _tick_model(self) -> None:
        if not self._alive:
            return
        t, m = _model_download["thread"], _model_download
        if m["error"]:
            self.model.set(False, f"Download failed: {m['error'][:120]}", "Retry",
                           lambda: start_model_download(self.settings.whisper_model))
        elif t is not None and not t.is_alive():
            self.model.set(True, "Downloaded. Speech is turned into text on this PC.")
            self.bar.config(value=1000)
        else:
            done, total = m["done"], m["total"]
            if total:
                self.bar.config(value=min(1000, done * 1000 // total))
                self.model.set(None, f"Downloading {_gb(min(done, total))} of {_gb(total)} (only once)...")
            else:
                self.model.set(None, "Downloading (about 1.6 GB, only once)...")
        self._update_finish()
        self.win.after(1000, self._tick_model)

    # ---------- finish ----------
    def _update_finish(self) -> None:
        ready = self.cli.ok and self.login.ok
        self.finish.config(state="normal" if ready else "disabled")
        if ready and not self.model.ok:
            self.note.config(text="You can finish now; the speech model keeps downloading in the background.")
        elif not ready:
            self.note.config(text="")
        else:
            self.note.config(text="All set!")

    def _finish(self) -> None:
        self._alive = False
        self.settings.setup_done = True
        self.settings.save()
        self.win.destroy()
        self.on_done()

    def _close(self) -> None:
        if not messagebox.askyesno(APP_NAME, "Setup is not finished. Close it anyway?\n"
                                   "It opens again the next time you start MoM Generator.", parent=self.win):
            return
        self._alive = False
        self.win.destroy()


class GuideWindow:
    """How to use the app. Shown after setup and from the tray menu (How it works)."""

    def __init__(self, root: tk.Tk, settings: Settings, actions: dict):
        self.settings = settings
        self.win = tk.Toplevel(root)
        self.win.title(f"{APP_NAME} - How it works")
        _place(self.win, 640, 560)

        f = ttk.Frame(self.win, padding=20)
        f.pack(fill="both", expand=True)
        f.columnconfigure(1, weight=1)
        ttk.Label(f, text="You're ready", font=("Segoe UI", 16, "bold")).grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(f, text="MoM Generator runs as a small blue icon in the taskbar, next to the clock "
                          "(if you don't see it, click the ^ arrow).", foreground=GREY, wraplength=580
                  ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(2, 10))

        hotkey = settings.hotkey.replace("<", "").replace(">", "").title()
        notes = Path(settings.output_dir)
        rows = [
            ("🎥", "Teams meetings - automatic",
             "Join a Teams meeting or call: recording starts by itself (the icon turns red). "
             "Leave the meeting: it stops and the MoM is created. Nothing to click."),
            ("🎙", "In-person meetings - laptop microphone",
             f"Click the tray icon (or press {hotkey}) to start recording the room. Click again to stop. "
             "Put the laptop in the middle of the table."),
            ("🟠", "Creating the MoM",
             "The icon turns orange for a few minutes. Then the MoM opens in Notepad for your final review."),
            ("📁", "Your notes",
             f"Saved in {notes}:\n  DD_MM_YYYY_<meeting>_transcript.txt - who said what (backup)\n"
             "  DD_MM_YYYY_<meeting>.txt - the MoM"),
            ("⚙", "More",
             "Right-click the icon for Settings (team names, project words), Cancel recording, "
             "and turning automatic Teams recording off."),
        ]
        r = 2
        for icon, title, text in rows:
            ttk.Label(f, text=icon, font=("Segoe UI Emoji", 16)).grid(row=r, column=0, rowspan=2, sticky="n", padx=(0, 12), pady=(6, 0))
            ttk.Label(f, text=title, font=("Segoe UI", 11, "bold")).grid(row=r, column=1, sticky="w", pady=(6, 0))
            ttk.Label(f, text=text, wraplength=540, justify="left").grid(row=r + 1, column=1, sticky="w")
            r += 2

        self.autostart = tk.BooleanVar(value=start_with_windows())
        ttk.Checkbutton(f, text="Start MoM Generator when Windows starts (needed to catch Teams meetings automatically)",
                        variable=self.autostart, command=lambda: start_with_windows(self.autostart.get())
                        ).grid(row=r, column=0, columnspan=2, sticky="w", pady=(14, 0))

        bar = ttk.Frame(self.win, padding=(20, 0, 20, 16))
        bar.pack(fill="x")
        ttk.Button(bar, text="Got it", command=self.win.destroy).pack(side="right")
        ttk.Button(bar, text="Open notes folder", command=actions["open_folder"]).pack(side="right", padx=6)
        ttk.Button(bar, text="Settings", command=lambda: (self.win.destroy(), actions["settings"]())).pack(side="right")
        _bring_to_front(self.win)


def promote_tray_icon(exe_name: str) -> None:
    """Windows 11 hides new tray icons under the ^ arrow. Show this app's icon on the taskbar instead
    (the same switch as Settings > Personalization > Taskbar > Other system tray icons)."""
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Control Panel\NotifyIconSettings") as root:
            for i in range(winreg.QueryInfoKey(root)[0]):
                name = winreg.EnumKey(root, i)
                with winreg.OpenKey(root, name, 0, winreg.KEY_READ | winreg.KEY_SET_VALUE) as key:
                    try:
                        path = winreg.QueryValueEx(key, "ExecutablePath")[0]
                    except OSError:
                        continue
                    if os.path.basename(path).lower() == exe_name.lower():
                        winreg.SetValueEx(key, "IsPromoted", 0, winreg.REG_DWORD, 1)
    except OSError:
        log.debug("Could not promote the tray icon", exc_info=True)
