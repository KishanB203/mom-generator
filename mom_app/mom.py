"""Turn a (messy, code-switched) meeting transcript into plain-text Minutes of Meeting with Claude.

Two ways to reach Claude:
- "cli": the Claude Code CLI already signed in on this PC with your Claude account (no API key needed)
- "api": the Anthropic API with an API key
"auto" uses the API when a key is configured, otherwise the CLI.
"""

import json
import logging
import os
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

from .config import APP_DIR, Settings, get_api_key

CLI_CONFIG_DIR = APP_DIR / "claude-login"

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You write Minutes of Meeting (MoM) for a software team's work meetings (stand-ups, planning, client calls, reviews, discussions), for people such as a manager who could not attend.

About the input:
- Usually you get <english_transcript>: the meeting already cleaned up and translated into English, one "Name: text" line per turn. Base the MoM on it. If <original_transcript> is also given (Hindi/Gujarati/English as spoken), use it only to check a point that reads oddly in English.
- Otherwise the transcript is machine-generated from a meeting where people switch between English, Hindi, Gujarati and Hinglish mid-sentence. Each utterance was transcribed in the language it was detected as, so you will see Devanagari (Hindi), Gujarati script, Latin text and mixtures. Gujarati is often written in Devanagari letters (e.g. "पण एमा transcript detect तो थाय छे" = Gujarati "but the transcript does get detected in it"). English technical words may be spelled phonetically in Devanagari or Gujarati script (e.g. "डेवलोप्मेंट" = development, "कांशक्शन मास्टर" = construction master).
- The Hindi and Gujarati parts usually carry most of the substance (status, problems, decisions). Read them carefully, reconstruct the meaning from context, the roster and the glossary, and include it in the MoM translated into clear professional English. Do not drop a point just because it was spoken in Hindi/Gujarati or is spelled oddly; if you are unsure of the meaning, give your best reading with "(?)".
- Speaker labels: the recorder's own name = their microphone. Names on remote lines come from the Teams meeting screen (who was shown as speaking) and are usually right, but can be off by one line at speaker changes. "Remote participants" = someone in the meeting whose name could not be read from the screen: infer the speaker from context (who was asked to give an update, names mentioned, roster, the Teams screen text) and only attribute when reasonably confident. If the recorder used speakers instead of headphones, remote speech may also appear duplicated under the recorder's name; de-duplicate it.
- <teams_screen> (when present) lists text read from the Teams meeting window: participant names, roster, chat messages and Teams' own live captions. Use it for the attendee list and speaker names. Teams' captions are poor for Hindi/Gujarati: trust their speaker names and timing, not their text, unless the recorded transcript is missing that part.
- A Teams transcript file may also be provided; it has better speaker names but worse text. Combine sources when both exist.
- Ignore greetings, small talk, filler ("Mhm", "Okay") and obvious recognition junk (a phrase repeated many times, random unrelated English sentences).

Rules:
- Never invent work items, dates, numbers or commitments. If something is unclear, write your best reading followed by "(?)" and list it under NEEDS CONFIRMATION.
- Keep it short and scannable: someone who missed the meeting should understand the outcome in under a minute.
- Use names exactly as written in the roster.
- Output PLAIN TEXT only (it is saved as a .txt file and pasted into Teams/email): no Markdown, no tables, no # or ** symbols. Use exactly this layout, writing "None" under a heading that has nothing:

MINUTES OF MEETING
Date: <date>
Meeting: <meeting name>
Project: <project>
Attendees: <names>
Prepared by: <recorder>

SUMMARY
- <2-4 points: purpose and outcome of the meeting, anything the reader must know or act on>

DISCUSSION POINTS
- <topic>: <what was discussed and concluded>

PERSON-WISE UPDATES
<Only when people gave status updates (e.g. a stand-up); otherwise write "None". For each person:>
<Name>
  Done: <...>
  Next: <...>
  Blockers: <... or None>

DECISIONS
- <...>

ACTION ITEMS
1. <action> - Owner: <name> - Due: <date or "Not set">

RISKS / OPEN ISSUES
- <...>

NEEDS CONFIRMATION
- <points where the transcript was unclear; the recorder will verify before sending>

Reply with the MoM text only - no preamble, no closing remarks."""


TRANSCRIPT_PROMPT = """You clean up a machine-generated transcript of a work meeting (software team) where people switch between English, Hindi and Gujarati mid-sentence. The result is the official meeting transcript, read by people who know all three languages.

The input lines look like "[mm:ss] Speaker: text" and come from speech recognition:
- Each utterance was recognised in the language it was detected as, so Hindi appears in Devanagari, English in Latin letters, and Gujarati often wrongly in Devanagari letters (e.g. "पण एमा transcript detect तो थाई छे" is Gujarati: "પણ એમાં transcript detect તો થાય છે").
- English technical words may be spelled phonetically in Devanagari/Gujarati (e.g. "डेवलोप्मेंट" = development, "कांशक्शन मास्टर" = construction master), and some Hindi/Gujarati speech may have been wrongly turned into odd English sentences.
- Speaker names come from the microphone (the recorder) and from the Teams meeting screen (who was shown as speaking); they can be off by a line at speaker changes. "Remote participants" means the name could not be read.

Write the corrected transcript:
- One line per turn, exactly "Name: text". Use first names (e.g. "Aarav", "Riya"); use full names only when two people share a first name.
- Keep lines short like a chat: join an input line with the next only when it continues the same sentence of the same person, and never put more than about three sentences on one line - continue on a new line with the same name. Start a new line wherever the speaker changes; under "Remote participants" several people may be talking (a question and its answer are two turns).
- Keep each sentence in the language it was spoken, in that language's own script: Hindi in Devanagari, Gujarati in Gujarati script, English in Latin letters. Write English words inside Hindi/Gujarati sentences in Latin letters, as people type them in chat (e.g. "मैं vendor data check कर लूँगा", "હા, મેં construction master જોઈ લીધું છે").
- Fix recognition errors from context, the roster and the glossary (names, project terms, obvious mishearings). Do not add, summarise or drop content; do not translate. Mark words you cannot recover with (?).
- Replace "Remote participants" with the right name when context makes it clear (someone is addressed by name and answers, roster, Teams screen text); otherwise keep "Remote participants". Remove only pure noise: recognition junk, a phrase repeated many times, a line duplicated across microphone and speakers.

Reply with the transcript lines only - no heading, no notes."""

TRANSLATE_PROMPT = """Translate this meeting transcript into clear, natural English. It is a work meeting of a software team; lines are "Name: text" in English, Hindi (Devanagari) and Gujarati (Gujarati script), often mixed in one sentence.
- Keep exactly the same lines and speaker names, one line per turn, as "Name: text".
- Translate the meaning faithfully: keep every detail, number, name and technical term; do not summarise or add anything. Lines already in English stay as they are (fix only obvious grammar).
- Keep (?) markers where the original was unclear.
Reply with the translated lines only."""


class MoMError(RuntimeError):
    pass


IN_PERSON_NOTE = (
    "This was an in-person conversation recorded with one laptop microphone, so everyone is on the same "
    "track, labelled \"Room\". Start a new line wherever the speaker changes. Use a person's name only when "
    "the conversation makes it clear (someone is addressed by name and answers, someone introduces themselves, "
    "the roster); otherwise call them Speaker 1, Speaker 2, ... and keep the same number for the same voice."
)


def meeting_context(settings: Settings, meeting_name: str, recorder_name: str = "", teams_screen: str = "",
                    in_person: bool = False) -> str:
    """Names and terms that help Claude fix recognition errors and speaker names."""
    parts = [
        f"Meeting name: {meeting_name}",
        IN_PERSON_NOTE if in_person else f"Recorder (the microphone speaker): {settings.my_name or recorder_name or 'me'}",
        "<team_roster>\n" + (settings.team_members.strip() or "not provided") + "\n</team_roster>",
        "<project_glossary>\n" + (", ".join(settings.glossary_terms()) or "not provided") + "\n</project_glossary>",
    ]
    if teams_screen.strip():
        parts.append("<teams_screen>\n" + teams_screen.strip() + "\n</teams_screen>")
    return "\n\n".join(parts)


def _chunks(lines: list[str], size: int) -> list[str]:
    return ["\n".join(lines[i:i + size]) for i in range(0, len(lines), size)]


def clean_and_translate(settings: Settings, raw: str, context: str, part_lines: int = 80) -> tuple[str, str]:
    """Raw recognition output -> ("Name: text" lines with each language in its own script, English version).

    Long meetings are split into parts so no answer gets cut off; each part is cleaned and then translated
    on its own, and up to 4 parts run at the same time.
    """
    from concurrent.futures import ThreadPoolExecutor

    parts = _chunks([ln for ln in raw.splitlines() if ln.strip()], part_lines)

    def run(i_part: tuple[int, str]) -> tuple[str, str]:
        i, part = i_part
        note = f"(Part {i + 1} of {len(parts)} of the meeting.)\n\n" if len(parts) > 1 else ""
        clean = ask_claude(settings, TRANSCRIPT_PROMPT, f"{context}\n\n{note}<transcript>\n{part}\n</transcript>")
        english = ask_claude(settings, TRANSLATE_PROMPT,
                             f"Translate this transcript.\n\n<transcript>\n{clean}\n</transcript>")
        return clean.strip(), english.strip()

    with ThreadPoolExecutor(max_workers=4) as pool:
        done = list(pool.map(run, enumerate(parts)))
    return "\n".join(c for c, _ in done), "\n".join(e for _, e in done)


def build_user_message(
    settings: Settings,
    meeting_time: datetime,
    meeting_name: str,
    transcript: str,
    teams_transcript: str = "",
    previous_mom: str = "",
    teams_screen: str = "",
    english_transcript: str = "",
    in_person: bool = False,
) -> str:
    parts = [
        f"Meeting date: {meeting_time:%A, %d %B %Y, %H:%M}",
        f"Meeting name: {meeting_name}",
        f"Project: {settings.project_name or 'not specified'}",
        f"Recorder (microphone speaker): {settings.my_name or 'me'}",
        "Type: in-person meeting (recorded with one microphone, not a Teams call)" if in_person else "Type: Teams meeting",
        f"Manager receiving this MoM: {settings.manager_name or 'manager'}",
        "<team_roster>\n" + (settings.team_members.strip() or "not provided - take names from the transcript") + "\n</team_roster>",
        "<project_glossary>\n" + (", ".join(settings.glossary_terms()) or "not provided") + "\n</project_glossary>",
    ]
    if previous_mom:
        parts.append(
            "<previous_mom>\nThe most recent earlier MoM (possibly a different meeting), for continuity only: "
            "if it is related, mention carried-over action items that come up again. Do not copy anything "
            "into this MoM unless it was discussed in this meeting.\n"
            + previous_mom
            + "\n</previous_mom>"
        )
    if english_transcript.strip():
        parts.append("<english_transcript>\n" + english_transcript.strip() + "\n</english_transcript>")
        if transcript.strip():
            parts.append("<original_transcript>\n" + transcript.strip() + "\n</original_transcript>")
    elif transcript.strip():
        parts.append("<recorded_transcript>\n" + transcript.strip() + "\n</recorded_transcript>")
    if teams_screen.strip():
        parts.append("<teams_screen>\n" + teams_screen.strip() + "\n</teams_screen>")
    if teams_transcript.strip():
        parts.append("<teams_transcript>\n" + teams_transcript.strip() + "\n</teams_transcript>")
    parts.append("Write the MoM for this meeting now.")
    return "\n\n".join(parts)


def find_claude_cli(configured: str = "") -> str:
    candidates = [configured, shutil.which("claude") or "", str(Path.home() / ".local" / "bin" / "claude.exe")]
    claude_desktop = Path(os.environ.get("APPDATA", "")) / "Claude" / "claude-code"
    if claude_desktop.is_dir():  # CLI bundled with the Claude desktop app, newest first
        candidates += [str(p) for p in sorted(claude_desktop.glob("*/*/claude.exe"), key=lambda p: p.stat().st_mtime, reverse=True)]
    for c in candidates:
        if c and Path(c).is_file():
            return c
    return ""


def cli_env() -> dict:
    """Environment for the Claude CLI with this tool's own sign-in folder.

    Claude Desktop writes its own placeholder into ~/.claude/.credentials.json, so a separate
    CLAUDE_CONFIG_DIR keeps this tool's login from being overwritten. Session variables inherited
    from Claude Code / Claude Desktop (e.g. CLAUDE_CODE_SIMPLE disables account login) are dropped.
    """
    env = {k: v for k, v in os.environ.items()
           if not (k.startswith(("CLAUDECODE", "CLAUDE_CODE_", "CLAUDE_AGENT_SDK_")) or k == "CLAUDE_PID")}
    CLI_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    env["CLAUDE_CONFIG_DIR"] = str(CLI_CONFIG_DIR)
    return env


def cli_signed_in(settings: Settings) -> bool:
    exe = find_claude_cli(settings.claude_cli_path)
    if not exe:
        return False
    try:
        proc = subprocess.run([exe, "auth", "status"], capture_output=True, text=True, encoding="utf-8",
                              errors="replace", env=cli_env(),
                              timeout=60, creationflags=subprocess.CREATE_NO_WINDOW)
        return bool(json.loads(proc.stdout or "{}").get("loggedIn"))
    except Exception:
        log.exception("auth status failed")
        return False


def open_cli_sign_in(settings: Settings) -> None:
    """Open a console window running 'claude auth login' with this tool's sign-in folder."""
    exe = find_claude_cli(settings.claude_cli_path)
    if not exe:
        raise MoMError("Claude Code CLI not found. Install it from https://claude.com/claude-code first.")
    subprocess.Popen(
        ["cmd", "/c", exe, "auth", "login", "&&", "echo.", "&&", "echo Signed in. You can close this window.", "&&", "pause"],
        env=cli_env(), creationflags=subprocess.CREATE_NEW_CONSOLE,
    )


def generate_mom(settings: Settings, user_message: str) -> str:
    return ask_claude(settings, SYSTEM_PROMPT, user_message)


def ask_claude(settings: Settings, system_prompt: str, user_message: str) -> str:
    backend = settings.claude_backend
    if backend == "auto":
        backend = "api" if get_api_key() else "cli"
    if backend == "api":
        text = _via_api(settings, system_prompt, user_message)
    else:
        text = _via_cli(settings, system_prompt, user_message)
    text = text.strip()
    if not text:
        raise MoMError("Claude returned an empty response.")
    return text


def _via_cli(settings: Settings, system_prompt: str, user_message: str) -> str:
    exe = find_claude_cli(settings.claude_cli_path)
    if not exe:
        raise MoMError(
            "Claude Code CLI not found. Install it (https://claude.com/claude-code), run 'claude' once in a "
            "terminal and sign in with your Claude account, or add an API key in Settings."
        )
    # Drop session variables inherited when launched from inside Claude Code / Claude Desktop
    # (e.g. CLAUDE_CODE_SIMPLE turns off account login), so the CLI uses your own sign-in.
    env = cli_env()
    env.setdefault("CLAUDE_CODE_MAX_OUTPUT_TOKENS", "32000")  # Hindi/Gujarati text takes many tokens
    if settings.claude_backend == "cli":
        env.pop("ANTHROPIC_API_KEY", None)  # use the Claude account login, not a stray key
    with tempfile.TemporaryDirectory() as work:  # empty folder: no project files or CLAUDE.md picked up
        prompt_file = Path(work) / "system.txt"
        prompt_file.write_text(system_prompt, encoding="utf-8")
        cmd = [exe, "-p", "--output-format", "json", "--system-prompt-file", str(prompt_file),
               "--tools", "", "--no-session-persistence", "--disable-slash-commands", "--strict-mcp-config"]
        if settings.cli_model:
            cmd += ["--model", settings.cli_model]
        try:
            proc = subprocess.run(
                cmd, input=user_message, capture_output=True, text=True, encoding="utf-8", errors="replace",
                cwd=work, env=env, timeout=900, creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except subprocess.TimeoutExpired as e:
            raise MoMError("Claude CLI took longer than 15 minutes.") from e
    out, err = proc.stdout.strip(), proc.stderr.strip()
    log.info("Claude CLI exit=%s stderr=%s", proc.returncode, err[:500])
    try:
        data = json.loads(out.splitlines()[-1]) if out else {}
    except json.JSONDecodeError:
        data = {}
    if proc.returncode != 0 or data.get("is_error") or not data.get("result"):
        detail = data.get("result") or err or out or f"exit code {proc.returncode}"
        if "auth" in detail.lower() or "login" in detail.lower() or "expired" in detail.lower():
            raise MoMError(f"Claude is not signed in ({detail}). Right-click the tray icon > 'Sign in to Claude...'.")
        raise MoMError(f"Claude CLI failed: {detail[:400]}")
    return data["result"]


def _via_api(settings: Settings, system_prompt: str, user_message: str) -> str:
    import anthropic

    api_key = get_api_key()
    if not api_key:
        raise MoMError("No Anthropic API key configured. Add one in Settings or switch Claude access to 'cli'.")
    client = anthropic.Anthropic(api_key=api_key, max_retries=3, timeout=600)
    try:
        with client.beta.messages.stream(
            model=settings.model,
            max_tokens=32000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": settings.effort},
            system=system_prompt,
            messages=[{"role": "user", "content": user_message}],
        ) as stream:
            final = stream.get_final_message()
    except anthropic.AuthenticationError as e:
        raise MoMError("The Anthropic API key was rejected. Check it in Settings.") from e
    except anthropic.PermissionDeniedError as e:
        raise MoMError(f"API key has no access to model '{settings.model}': {e.message}") from e
    except anthropic.NotFoundError as e:
        raise MoMError(f"Model '{settings.model}' not found. Check the model name in Settings.") from e
    except anthropic.RateLimitError as e:
        raise MoMError("Rate limited by the Claude API. Try again in a minute.") from e
    except anthropic.BadRequestError as e:
        raise MoMError(f"Claude API rejected the request: {e.message}") from e
    except anthropic.APIStatusError as e:
        raise MoMError(f"Claude API error {e.status_code}: {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise MoMError("Could not reach the Claude API. Check your internet/VPN/proxy.") from e

    log.info("MoM generated: stop=%s usage=%s request=%s", final.stop_reason, final.usage, final._request_id)
    if final.stop_reason == "refusal":
        raise MoMError("Claude declined to process this transcript.")
    # If a fallback model took over mid-stream, keep only the text written after the switch.
    blocks = list(final.content)
    last_fallback = max((i for i, b in enumerate(blocks) if b.type == "fallback"), default=-1)
    text = "".join(b.text for b in blocks[last_fallback + 1 :] if b.type == "text")
    if final.stop_reason == "max_tokens":
        text += "\n\n(Note: output was cut off because it was too long.)"
    return text
