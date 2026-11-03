"""Reads the Teams meeting window while recording: who is in the meeting, who is speaking, live captions.

New Teams is a WebView2 app. Its page content is only exposed to UI Automation after an accessibility client
asks for it (WM_GETOBJECT on the render window), so we send that once, then poll the window's element names.

Teams' UI labels change between versions, so the watcher keeps two things:
  - parsed events (speaker changes, caption lines) used to put names on transcript lines, and
  - every distinct short UI string with when it was first/last seen (screen.json), which Claude uses to find
    attendee names and which helps fix the parsing when Teams changes.
"""

import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

POLL_SECONDS = 1.0
SPEAKING = re.compile(r"^(?P<name>[^,]{2,60}?)\s*(?:,|\s)\s*(?:is\s+)?speaking\b", re.I)
# Generic Teams controls that are never names (keeps screen.json small)
UI_NOISE = re.compile(
    r"^(leave|hang up|mic|camera|share|more|react|raise|view|chat|people|rooms|apps|close|minimize|maximize|"
    r"restore|system|settings|search|back|forward|copilot|notes|record|turn |show |hide |open |pin |mute|unmute|"
    r"profile picture|to get missing image)", re.I,
)


def _uia():
    import comtypes
    import comtypes.client

    comtypes.CoInitialize()
    comtypes.client.GetModule("UIAutomationCore.dll")
    from comtypes.gen import UIAutomationClient as U

    return U, comtypes.client.CreateObject(U.CUIAutomation, interface=U.IUIAutomation)


def _wake_accessibility(hwnd: int) -> None:
    """Ask Chromium/WebView2 to build its accessibility tree for this window."""
    import ctypes

    import win32gui

    kids = []
    win32gui.EnumChildWindows(hwnd, lambda h, _: kids.append(h) or True, None)
    for h in kids:
        if "RenderWidgetHost" in win32gui.GetClassName(h):
            ctypes.windll.user32.SendMessageTimeoutW(h, 0x003D, 0, 0xFFFFFFFC, 0x0002, 500, None)  # OBJID_CLIENT


def meeting_window(U, uia, windows: list[tuple[int, str]]) -> tuple[int | None, str]:
    """(hwnd, title) of the Teams window that has a Leave button = you are in a meeting/call."""
    cond = uia.CreatePropertyCondition(U.UIA_ControlTypePropertyId, U.UIA_ButtonControlTypeId)
    for hwnd, title in windows:
        buttons = uia.ElementFromHandle(hwnd).FindAll(U.TreeScope_Descendants, cond)
        for i in range(buttons.Length):
            name = (buttons.GetElement(i).CurrentName or "").strip().lower()
            if name.startswith(("leave", "hang up")):
                return hwnd, title
    return None, ""


class MeetingDetector:
    """Watches Teams in the background and reports when you join and leave a meeting or call.

    Joining = a Teams window shows a Leave button for two checks in a row (~10 s; the pre-join lobby has none).
    Leaving = no Leave button for END_AFTER_S, so a brief glitch (e.g. switching to screen sharing) does not
    end the recording.
    """

    POLL_S = 5
    END_AFTER_S = 30

    def __init__(self, on_join: Callable[[str], None], on_leave: Callable[[], None]):
        self.on_join, self.on_leave = on_join, on_leave
        self.in_meeting = False
        self._stop = threading.Event()
        threading.Thread(target=self._run, daemon=True, name="MeetingDetector").start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        from .teams import teams_windows

        try:
            U, uia = _uia()
        except Exception:
            log.exception("UI Automation is not available; Teams meetings will not be detected")
            return
        woken: set[int] = set()
        hits, last_seen = 0, 0.0
        while not self._stop.wait(self.POLL_S):
            try:
                windows = teams_windows()
                for hwnd, _title in windows:
                    if hwnd not in woken:
                        _wake_accessibility(hwnd)
                        woken.add(hwnd)
                hwnd, title = meeting_window(U, uia, windows) if windows else (None, "")
            except Exception as e:
                log.debug("Meeting detection failed: %s", e)
                continue
            now = time.monotonic()
            if hwnd:
                hits, last_seen = hits + 1, now
                if not self.in_meeting and hits >= 2:
                    self.in_meeting = True
                    log.info("Teams meeting joined: %r", title)
                    self._call(self.on_join, title)
            else:
                hits = 0
                if self.in_meeting and now - last_seen >= self.END_AFTER_S:
                    self.in_meeting = False
                    log.info("Teams meeting left")
                    self._call(self.on_leave)

    @staticmethod
    def _call(fn, *args) -> None:
        try:
            fn(*args)
        except Exception:
            log.exception("Meeting callback failed")


class MeetingWatcher:
    def __init__(self, folder: Path):
        self.path = folder / "screen.json"
        self._stop = threading.Event()
        self._t0 = time.monotonic()
        self.meeting_title = ""
        self.me = ""  # signed-in Teams user, from the profile picture
        self.strings: dict[str, list[float]] = {}  # UI string -> [first_seen, last_seen]
        self.speaking: list[tuple[float, str]] = []  # (t, name) whenever the active speaker changes
        self.captions: list[tuple[float, str, str]] = []  # (t, speaker, text)
        self._thread = threading.Thread(target=self._run, daemon=True, name="MeetingWatcher")

    def start(self) -> "MeetingWatcher":
        self._thread.start()
        return self

    def stop(self) -> dict:
        self._stop.set()
        self._thread.join(timeout=5)
        result = self.result()
        try:
            self.path.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError:
            log.exception("Could not save screen.json")
        log.info("Screen watcher: title=%r, %d strings, %d speaker changes, %d captions",
                 self.meeting_title, len(self.strings), len(self.speaking), len(self.captions))
        return result

    def result(self) -> dict:
        return {
            "meeting_title": self.meeting_title,
            "me": self.me,
            "speaking": self.speaking,
            "captions": self.captions,
            "strings": {k: [round(a), round(b)] for k, (a, b) in self.strings.items()},
        }

    # ---------- polling ----------
    def _run(self) -> None:
        try:
            U, uia = _uia()
        except Exception:
            log.exception("UI Automation is not available; speaker names will not be detected")
            return
        hwnd, woken, last_find = None, set(), -60.0
        while not self._stop.is_set():
            try:
                # look for the meeting window every 5 s until found, then re-check every 15 s
                if time.monotonic() - last_find > (15 if hwnd else 5):
                    hwnd, last_find = self._find_meeting_window(U, uia, woken), time.monotonic()
                if hwnd:
                    self._snapshot(U, uia, hwnd)
            except Exception as e:
                log.debug("Screen poll failed: %s", e)
                hwnd = None
            self._stop.wait(POLL_SECONDS)

    def _find_meeting_window(self, U, uia, woken: set):
        """The meeting window is the Teams window that has a Leave button."""
        from .teams import teams_windows

        windows = teams_windows()
        for hwnd, _title in windows:
            if hwnd not in woken:
                _wake_accessibility(hwnd)
                woken.add(hwnd)
        # your name is in the main window's header, which can be listed after the meeting window
        for hwnd, _title in windows:
            if self.me:
                break
            self.me = self._my_name(U, uia, uia.ElementFromHandle(hwnd))
        hwnd, title = meeting_window(U, uia, windows)
        if hwnd and not self.meeting_title:
            self.meeting_title = title
            log.info("Meeting window found: %r", title)
        return hwnd

    @staticmethod
    def _my_name(U, uia, root) -> str:
        """Main window header: button "Your profile, ..." followed by image "Profile picture of <name>."."""
        cond = uia.CreatePropertyCondition(U.UIA_ControlTypePropertyId, U.UIA_ImageControlTypeId)
        images = root.FindAll(U.TreeScope_Descendants, cond)
        for i in range(images.Length):
            name = images.GetElement(i).CurrentName or ""
            parent = uia.ControlViewWalker.GetParentElement(images.GetElement(i))
            if name.startswith("Profile picture of ") and (parent.CurrentName or "").startswith("Your profile"):
                return name[len("Profile picture of "):].strip().rstrip(".")
        return ""

    def _snapshot(self, U, uia, hwnd) -> None:
        cr = uia.CreateCacheRequest()
        cr.AddProperty(U.UIA_NamePropertyId)
        cr.AddProperty(U.UIA_ControlTypePropertyId)
        elements = uia.ElementFromHandle(hwnd).FindAllBuildCache(U.TreeScope_Descendants, uia.CreateTrueCondition(), cr)
        now = time.monotonic() - self._t0
        names = []
        for i in range(elements.Length):
            name = " ".join((elements.GetElement(i).CachedName or "").split())
            if name:
                names.append(name)

        speakers = []
        for name in names:
            m = SPEAKING.match(name)
            if m:
                speakers.append(m.group("name").strip())
            if not self.me and name.startswith("Myself video, "):  # your own tile: "Myself video, <name>, Unmuted"
                self.me = name.split(",")[1].strip()
            if 2 <= len(name) <= 160 and not UI_NOISE.match(name):
                seen = self.strings.setdefault(name, [now, now])
                seen[1] = now
        current = ", ".join(sorted(set(speakers)))
        if current != (self.speaking[-1][1] if self.speaking else ""):
            self.speaking.append((now, current))
        self._read_captions(names, now)

    def _read_captions(self, names: list[str], now: float) -> None:
        """Live captions appear as: <container 'Live captions'> then speaker name / caption text pairs."""
        try:
            start = next(i for i, n in enumerate(names) if n.lower() in ("live captions", "captions"))
        except StopIteration:
            return
        block = names[start + 1:start + 61]
        recent = {(spk, txt) for _, spk, txt in self.captions[-40:]}
        # the caption list also holds Teams' own buttons/labels: take "<person name>, <text>" pairs only
        known = {spk for _, spk, _ in self.captions}
        pairs = [(a, b) for a, b in zip(block, block[1:])
                 if looks_like_name(a) and (not looks_like_name(b) or (a in known and b not in known))]
        for spk, txt in pairs:
            if (spk, txt) not in recent:
                # a caption grows while the person talks: replace the unfinished line instead of adding
                if self.captions and self.captions[-1][1] == spk and txt.startswith(self.captions[-1][2][:20]):
                    self.captions[-1] = (self.captions[-1][0], spk, txt)
                else:
                    self.captions.append((now, spk, txt))


NOT_NAMES = {"system", "minimize", "restore", "close", "maximize", "more options"}


def looks_like_name(text: str) -> bool:
    """"Riya Shah", "Aarav Mehta [アーラヴ]" - not a caption sentence or a Teams control."""
    words = text.split()
    return (
        1 <= len(words) <= 5
        and len(text) <= 60
        and text.lower() not in NOT_NAMES
        and "caption" not in text.lower()
        and not text.rstrip().endswith((".", "?", "!", ","))
        and all(w[0].isalpha() or w[0] in "[(" for w in words)
        and all(w[0].isupper() or not w[0].isascii() or w[0] in "[(" for w in words)
    )


def speaker_at(speaking: list, start: float, end: float) -> str:
    """The person shown as speaking for most of [start, end], or ""."""
    totals: dict[str, float] = {}
    for i, (t, who) in enumerate(speaking):
        t_next = speaking[i + 1][0] if i + 1 < len(speaking) else float("inf")
        overlap = min(end, t_next) - max(start, t)
        if who and overlap > 0:
            totals[who] = totals.get(who, 0) + overlap
    return max(totals, key=totals.get) if totals else ""


def screen_summary(result: dict, max_strings: int = 250) -> str:
    """Compact text for Claude: names seen in the meeting window and who spoke when."""
    if not result:
        return ""
    lines = []
    if result.get("meeting_title"):
        lines.append(f"Meeting window title: {result['meeting_title']}")
    strings = sorted(result.get("strings", {}).items(), key=lambda kv: kv[1][0])[:max_strings]
    if strings:
        lines.append("Text seen in the Teams meeting window (participant tiles, roster, chat; first seen mm:ss):")
        lines += [f"  [{int(a) // 60:02d}:{int(a) % 60:02d}] {s}" for s, (a, _b) in strings]
    if result.get("captions"):
        lines.append("Teams live captions (speaker names are reliable; the caption text may be wrong for Hindi/Gujarati):")
        lines += [f"  [{int(t) // 60:02d}:{int(t) % 60:02d}] {spk}: {txt}" for t, spk, txt in result["captions"]]
    return "\n".join(lines)
