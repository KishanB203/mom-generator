# MoM Generator

A small Windows tray tool that writes **Minutes of Meeting (MoM)** for Teams meetings (stand-ups, planning, client calls, reviews) and for in-person meetings around your laptop.

**Teams meetings are recorded automatically:** recording starts when you join a meeting or call and stops when you leave it. For an **in-person meeting**, click the tray icon to start and again to stop; it records the room with the laptop's built-in microphone. Either way, a few minutes later you get **two files**: a full transcript showing who said what, and a clean English MoM. It works even when people switch between **English, Hindi and Gujarati**, which Teams' own transcript can't handle.

```
Join Teams  ─►  🔴 records your mic + the meeting audio, and reads the Teams meeting window
 (or click)        (who is in the meeting and who is speaking); speech is turned into text as you talk
Leave       ─►  🟠 1. the last minute of speech to text, each sentence in the language it was spoken
 (or click)
                   2. Claude cleans it into a "who said what" transcript (Hindi in हिन्दी, Gujarati in ગુજરાતી)
                   3. Claude translates the transcript into English
                   4. Claude writes the MoM from it (your Claude account, no API key needed)
            ─►  Downloads\MoM_Notes\02_11_2026_<meeting>_transcript.txt   (as spoken + English)
                Downloads\MoM_Notes\02_11_2026_<meeting>.txt              (the MoM, opened for your review)
```

The transcript looks like this:
```
User_1: did you check vendor data?
User_2: હા, મેં vendor data જોઈ લીધો છે
User_3: मैं construction master का mapping कल तक finish कर दूँगा
```

---

## Install (users)

1. Download **MoMGenerator-Setup.exe** from the download page and double-click it.
   If Windows shows *Windows protected your PC*, click **More info > Run anyway** (the installer isn't code-signed yet).
2. Click **Install**. No admin rights are needed. The app starts by itself when the install finishes.
3. **The setup window** checks three things and helps with whatever is missing:
   - **Claude Code**: if it's missing, click **Install**. A window installs it in about a minute.
   - **Sign in to Claude**: click **Sign in**. Your browser opens; log in with your Claude account and approve. The step turns green by itself.
     *(No Claude plan with Claude Code? Click **Use API key** and paste an Anthropic API key instead.)*
   - **Speech model**: downloads by itself (1.6 GB, only once). You can click **Finish** before it's done; it keeps downloading in the background.
4. **The "How it works" guide** opens. Tick **Start MoM Generator when Windows starts** if you want Teams meetings caught automatically without starting the app first.

The blue icon sits on the taskbar next to the clock. You can reopen the guide any time: right-click the icon > **How it works**.
**Requirements:** Windows 10 or 11 (64-bit), about 2 GB of free disk space, and a Claude account with Claude Code access (Pro, Max, Team or Enterprise), or an API key.

## How to use

| To do this | Do this |
|---|---|
| **Teams meeting** | Nothing to do: joining starts the recording (🔴) and leaving stops it (🟠). The MoM opens in Notepad when ready (a few minutes). Calls shorter than 1 minute are discarded |
| **In-person meeting** | Left-click the tray icon (or press **Ctrl+Alt+M**) to start; it records the room with the laptop's built-in mic. Click again to stop and create the MoM. Put the laptop in the middle of the table |
| Stop a Teams recording early | Left-click the icon. It won't restart until that meeting ends |
| Turn automatic Teams recording on/off | Right-click > *Auto-record Teams meetings* |
| Cancel a recording | Right-click > *Cancel recording* (the audio is deleted) |
| MoM from a Teams transcript file | Right-click > *Create MoM from Teams transcript file...* (`.vtt` / `.docx`) |
| MoM from text you copied | Right-click > *Create MoM from pasted text...* |
| See all notes | Right-click > *Open notes folder* |

**Final review:** always read the MoM before you send it. Unclear parts are marked **(?)** and listed under **NEEDS CONFIRMATION**. Fix them in Notepad, save, then copy the MoM into Teams or email.

**The transcript file is your backup.** Each step is saved before the next one starts. If the MoM ever misses something, the full conversation is still in `..._transcript.txt`. You can give it to Claude yourself, or use right-click > *Create MoM from pasted text...*.

**Speaker names** come from the Teams meeting window, from the tile Teams highlights while someone talks, and from your own Teams profile for your microphone. Keep the meeting window open (it can be behind other windows, but not minimized). Turning on Teams **live captions** (More > Language and speech > Show live captions) gives the most reliable names; their text doesn't matter. Lines whose speaker couldn't be read show as *Remote participants*.
In **in-person meetings** everyone is on one microphone, so people are named only when the conversation makes it clear (someone is addressed by name and answers); otherwise they appear as *Speaker 1*, *Speaker 2*, ...

**File names:** the meeting name comes from the Teams meeting window, for example `02_11_2026_Sprint_Planning.txt`. If no meeting window is found, the name is `Meeting`. If something goes wrong, a `..._FAILED.txt` file is saved with the reason and the full transcript, so nothing is lost.

### Recommended: Settings (2 minutes, makes the MoM much more accurate)
Right-click the icon > **Settings**:
- **Your name**: used for your own voice in the transcript (if empty, your Teams name is used)
- **Team members**: one per line, for example `Aarav Mehta | Developer`
- **Project glossary**: module, client and feature names, for example `construction master, vendor, work item, license`
- **Meeting languages**: `en, hi, gu` by default. Add others if your meetings use them, e.g. `mr` for Marathi

Click **Test audio (3 s)** while some sound is playing. Both levels should be above 0%.

## Tips
- **Use headphones.** With speakers, your mic also picks up everyone else, and their speech gets duplicated.
- If Teams uses a different audio device than Windows' default, choose it in Settings > *Speaker to capture*.
- In-person meetings use the laptop's built-in microphone, even if a headset is connected (a headset mic only hears you). To use another mic, choose it in Settings > *In-person microphone*.
- On a slow PC, set Settings > *Speech model* to `medium`. It's faster but less accurate.
- Speech is converted to text **during** the meeting (at low priority, so Teams isn't slowed down). After you click stop, only the last minute or so is left, plus 1–3 minutes for Claude. Keep the PC awake and plugged in during long meetings.

## Troubleshooting
| Problem | Fix |
|---|---|
| "Claude is not signed in" | Right-click the icon > *Sign in to Claude...* (opens the setup window) |
| "Microphone is silent" | Go to Windows Settings > Privacy & security > Microphone and turn on *Let desktop apps access your microphone* |
| Icon not visible | Click the **^** arrow in the taskbar, or start *MoM Generator* from the Start menu |
| Anything else | Right-click the icon > *Open log* and share it with the person who gave you the tool |

**Update:** run the newest MoMGenerator-Setup.exe over the old version. Your notes and settings stay.
**Uninstall:** Windows Settings > Apps > *MoM Generator* > Uninstall. Your notes in `Downloads\MoM_Notes` and settings in `%APPDATA%\MoMGenerator` are kept; delete them yourself if you want.

## Privacy policy
MoM Generator has no telemetry and no server of its own. It connects only to these services:

- **Claude (Anthropic):** the text transcript is sent to write the MoM. It goes through the Claude account you sign in with, or your own API key. Audio is never uploaded; it is converted to text **on your PC**.
- **Hugging Face:** the speech model is downloaded once, during setup.
- **claude.ai:** Claude Code is installed from here, only when you click **Install** in the setup window.

Recordings, transcripts and MoMs stay on your PC: `Downloads\MoM_Notes` and `%APPDATA%\MoMGenerator`.

Always tell participants the meeting is being recorded, and follow your company's recording policy.

**Changes to Windows settings**, all visible and reversible in the "How it works" window:
- **Start with Windows:** off unless you tick it.
- **Always show the icon on the taskbar:** on by default; untick to turn it off.

## Code signing policy
Free code signing provided by [SignPath.io](https://about.signpath.io), certificate by [SignPath Foundation](https://signpath.org).

- **Committers and reviewers:** [KishanB203](https://github.com/KishanB203)
- **Approvers:** [KishanB203](https://github.com/KishanB203)

Only `MoMGenerator.exe` and `MoMGenerator-Setup.exe` are signed. Both are built by GitHub Actions from this repository ([release.yml](.github/workflows/release.yml)), and each release is approved by hand before it is signed. The bundled open-source libraries are shipped as published by their maintainers.

## License
[MIT](LICENSE)

---

## For maintainers

### Folder structure
| Path | What it is |
|---|---|
| `mom_app\` | The app (Python): tray, recording, speech-to-text, Teams reader, Claude, setup window |
| `run.pyw` | Starts the app from source |
| `requirements.txt` | Python packages |
| `mom.ico` | App icon |
| `installer\MoMGenerator.spec` | PyInstaller: packs the app into `build\dist\MoMGenerator\MoMGenerator.exe` |
| `installer\MoMGenerator.iss` | Inno Setup: turns that folder into one installer |
| `installer\build.ps1` | Builds `dist\MoMGenerator-Setup.exe` (both steps above) |
| `website\` | The download page (one HTML file + icons) |
| `.github\workflows\release.yml` | GitHub builds the installer and publishes a release when you push a version tag |
| `.github\workflows\pages.yml` | GitHub publishes `website\` on GitHub Pages |

Where the installed app keeps things:
- **Program:** `%LOCALAPPDATA%\Programs\MoM Generator`
- **Notes:** `Downloads\MoM_Notes`
- **Settings, log, Claude sign-in and recordings:** `%APPDATA%\MoMGenerator`
- **Speech model:** `%USERPROFILE%\.cache\huggingface`

### Run from source
```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\pythonw run.pyw
```

### Build the installer on this PC
Needs `pip install pyinstaller` and Inno Setup 6 (`winget install JRSoftware.InnoSetup`).
```powershell
powershell -ExecutionPolicy Bypass -File installer\build.ps1
```
To check the packaged app, run `build\dist\MoMGenerator\MoMGenerator.exe --selftest`. Results go to the log, and the exit code is the number of failed checks.

### Publish (free: GitHub Releases + GitHub Pages)
One time:
1. Create a **public** repository on GitHub, e.g. `mom-generator`, and push this folder to it. Free Pages and public downloads need a public repository. If the code must stay private, keep it in a private repository and publish releases and the website from a second, public one.
2. In the repository, go to **Settings > Pages > Source** and choose **GitHub Actions**.

Each release:
1. Set the new version in `mom_app\__init__.py` (`__version__ = "1.0.1"`).
2. Commit, then run `git tag v1.0.1` and `git push origin main v1.0.1`.
3. GitHub builds and self-tests `MoMGenerator-Setup.exe` and attaches it to the release (about 10 minutes).

The download page is `https://<owner>.github.io/<repo>/`. Its button always points to the newest release:
`https://github.com/<owner>/<repo>/releases/latest/download/MoMGenerator-Setup.exe`
