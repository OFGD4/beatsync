# BeatSync

Beat-align any song to any video. It matches the tempo, locks the song's kick drum to the video's beat,
and adds beat-synced effects, color grading and export options.

Runs as a **Windows desktop app**, or as a local web app in your browser.

---

## Use the desktop app

1. Download `BeatSync-Setup-x.y.z.exe` from the **Releases** page and run it. It needs no admin rights.
2. On first launch, BeatSync offers to download its free tools (about 215 MB, one time):
   - **ffmpeg**: required
   - **yt-dlp + Deno**: only needed for links (YouTube, TikTok, …)
3. That's it. yt-dlp updates itself once a day, so links keep working when YouTube changes.

Windows may show "Windows protected your PC" because the app isn't code-signed yet.
Click **More info → Run anyway**.

### Where things live

| What | Where |
|---|---|
| App | `%LOCALAPPDATA%\Programs\BeatSync` |
| Tools, settings, cookies | `%LOCALAPPDATA%\BeatSync` |
| Temp files | `%LOCALAPPDATA%\BeatSync\temp`, deleted on close and on every start |
| Log | `%LOCALAPPDATA%\BeatSync\logs\beatsync.log` (Settings → Open logs) |

---

## Run from source

```bat
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements-desktop.txt
.venv\Scripts\python desktop.py            REM desktop window
.venv\Scripts\python desktop.py --headless REM engine only → http://127.0.0.1:5002
```

The browser-only version still works: `pip install -r requirements.txt`, then `python server.py`.

---

## Build the Windows app yourself

Requirements: Windows 10/11, **Python 3.12** (python.org), and optionally **Inno Setup 6**
(jrsoftware.org) for the installer.

```bat
build\build.bat
```

Output:
- `dist\BeatSync\BeatSync.exe`: portable app folder
- `dist\BeatSync-Setup-<version>.exe`: installer (only if Inno Setup is installed)

---

## Publish releases automatically (GitHub)

1. Create a public repository on GitHub, e.g. `beatsync`, and push this folder to it.
2. In `app_info.py`, set `GITHUB_REPO = 'your-username/beatsync'`. This turns on the in-app update check.
3. Tag a version and push it:
   ```bat
   git tag v3.0.0
   git push origin v3.0.0
   ```
4. GitHub Actions (`.github/workflows/release.yml`) builds the app, runs a smoke test, and publishes
   the installer plus a portable zip on the Releases page. Every new tag becomes a new release, and the
   app tells users when one is available.

You can also start a build without a tag: **Actions → Build Windows app → Run workflow**.

### Code signing (optional, free for open source)

Apply to **SignPath.io**'s open-source program with the repository link. Once approved, add their GitHub
Action step where the workflow's comment says. The "Unknown publisher" warning then goes away as the app
builds reputation.

---

## License

GPL v3. See `LICENSE` and `THIRD_PARTY_NOTICES.md`.
