"""Settings storage (%APPDATA%\\MoMGenerator\\config.json) and API key storage (Windows Credential Manager)."""

import json
import logging
import os
import sys
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

log = logging.getLogger(__name__)

APP_DIR = Path(os.environ.get("APPDATA", Path.home())) / "MoMGenerator"
CONFIG_PATH = APP_DIR / "config.json"
LOG_PATH = APP_DIR / "app.log"
RECORDINGS_DIR = APP_DIR / "recordings"  # audio + raw transcripts, kept out of the notes folder
CRED_TARGET = "MoMGenerator/AnthropicApiKey"


def downloads_folder() -> Path:
    """The user's Downloads folder, also when it was moved to another drive."""
    try:
        import ctypes
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [("a", wintypes.DWORD), ("b", wintypes.WORD), ("c", wintypes.WORD), ("d", ctypes.c_ubyte * 8)]

        downloads = GUID(0x374DE290, 0x123F, 0x4565, (ctypes.c_ubyte * 8)(0x91, 0x64, 0x39, 0xC4, 0x92, 0x5E, 0x46, 0x7B))
        path = ctypes.c_wchar_p()
        if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(downloads), 0, None, ctypes.byref(path)) == 0:
            folder = Path(path.value)
            ctypes.windll.ole32.CoTaskMemFree(path)
            return folder
    except Exception:
        log.debug("Downloads folder lookup failed", exc_info=True)
    return Path.home() / "Downloads"


@dataclass
class Settings:
    my_name: str = ""
    manager_name: str = ""
    manager_email: str = ""
    project_name: str = ""
    # One member per line: "Full Name | role | aliases (optional)"
    team_members: str = ""
    # Project words the speech model and Claude should know (one per line or comma separated)
    glossary: str = ""
    # How to reach Claude: auto (API if a key is saved, else CLI) | cli (your Claude login) | api (API key)
    claude_backend: str = "auto"
    claude_cli_path: str = ""  # "" = find claude.exe automatically
    cli_model: str = ""  # "" = the CLI's default model; e.g. "opus" or "sonnet"
    model: str = "claude-opus-5-5"  # API backend only
    effort: str = "high"  # API backend only
    # Languages spoken in meetings (Whisper codes): en English, hi Hindi, gu Gujarati, mr Marathi, ...
    languages: str = "en, hi, gu"
    whisper_model: str = "large-v3-turbo"
    whisper_device: str = "auto"  # auto | cpu | cuda
    mic_device: str = ""  # "" = Windows default
    speaker_device: str = ""  # "" = Windows default (loopback)
    # In-person meetings (no Teams): one microphone that hears the whole room. "" = the laptop's built-in mic
    room_mic_device: str = ""
    # Start recording by itself when you join a Teams meeting, stop when you leave it
    auto_record_teams: bool = True
    output_dir: str = field(default_factory=lambda: str(downloads_folder() / "MoM_Notes"))
    default_meeting_name: str = "Meeting"  # used when no Teams meeting window is found
    open_after_save: bool = True
    hotkey: str = "<ctrl>+<alt>+m"
    keep_audio: bool = True
    include_previous_mom: bool = True
    setup_done: bool = False  # the first-run setup window (Claude sign-in, speech model) was completed

    @classmethod
    def load(cls) -> "Settings":
        if not CONFIG_PATH.exists():
            return cls()
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            log.exception("Could not read config, using defaults")
            return cls()
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

    def save(self) -> None:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(asdict(self), indent=2, ensure_ascii=False), encoding="utf-8")

    def glossary_terms(self) -> list[str]:
        raw = self.glossary.replace(",", "\n").splitlines()
        return [t.strip() for t in raw if t.strip()]

    def language_codes(self) -> tuple[str, ...]:
        codes = tuple(c.strip().lower() for c in self.languages.replace(";", ",").split(",") if c.strip())
        return codes or ("en", "hi", "gu")

    def member_names(self) -> list[str]:
        names = []
        for line in self.team_members.splitlines():
            name = line.split("|")[0].strip()
            if name:
                names.append(name)
        return names


def get_api_key() -> str:
    """Environment variable wins; otherwise the key saved in Windows Credential Manager."""
    env = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if env:
        return env
    try:
        import win32cred

        cred = win32cred.CredRead(CRED_TARGET, win32cred.CRED_TYPE_GENERIC)
        blob = cred["CredentialBlob"]
        return (blob.decode("utf-16-le") if isinstance(blob, bytes) else str(blob)).strip()
    except Exception:
        return ""


def set_api_key(key: str) -> None:
    import win32cred

    key = key.strip()
    if not key:
        try:
            win32cred.CredDelete(CRED_TARGET, win32cred.CRED_TYPE_GENERIC)
        except Exception:
            pass
        return
    win32cred.CredWrite(
        {
            "Type": win32cred.CRED_TYPE_GENERIC,
            "TargetName": CRED_TARGET,
            "UserName": "anthropic",
            "CredentialBlob": key,
            "Persist": win32cred.CRED_PERSIST_LOCAL_MACHINE,
        },
        0,
    )


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def app_command() -> str:
    """Command line that starts this app: the installed MoMGenerator.exe, or pythonw run.pyw from source."""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    pyw = Path(sys.executable).with_name("pythonw.exe")
    return f'"{pyw}" "{Path(__file__).resolve().parent.parent / "run.pyw"}"'


def start_with_windows(enable: bool | None = None) -> bool:
    """Read (enable=None) or change whether the app starts when you sign in to Windows."""
    import winreg

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
        if enable is True:
            winreg.SetValueEx(key, "MoM Generator", 0, winreg.REG_SZ, app_command())
        elif enable is False:
            try:
                winreg.DeleteValue(key, "MoM Generator")
            except FileNotFoundError:
                pass
        try:
            winreg.QueryValueEx(key, "MoM Generator")
            return True
        except FileNotFoundError:
            return False
