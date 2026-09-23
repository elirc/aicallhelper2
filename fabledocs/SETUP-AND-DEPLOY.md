# AI Call Assistant — Setup, Build and Deploy Guide

This guide takes a fresh Windows machine from nothing to a running,
packaged app. It covers three audiences: someone who just wants to **use**
the app, a developer who wants to **run it from source**, and whoever
**builds the release** that gets handed to other people.

Originally verified against the repository on 2026-09-18 (version 3.1);
revised 2026-09-22 for the declared/pinned dependency workflow, the
packaging CI job and the review corrections in
`PROJECT-REVIEW-2026-09-19.md` §4. The release procedure itself is
[RELEASE-CHECKLIST.md](RELEASE-CHECKLIST.md). Where a step depends on your
own accounts (API keys) it says so.

---

## 1. Requirements

| Item | Needed for | Notes |
| --- | --- | --- |
| Windows 10 2004+ or Windows 11 | Everything | Screen-capture exclusion uses `WDA_EXCLUDEFROMCAPTURE`, which needs Windows 10 build 2004 or later. Older builds still run the app but show a standing warning. |
| Microsoft Edge WebView2 Runtime | Everything | Present on all current Windows 11 and most Windows 10 machines. If the window opens blank, install it from Microsoft's WebView2 page. |
| Speakers or a headset that Windows lists as the **default output device** | Recording | The app captures everything the default output plays (loopback) — the call, but also notifications or music. It never opens a microphone. See "Audio routing" in the User Guide. |
| Deepgram API key | Recording / transcription | console.deepgram.com |
| Anthropic API key | Answers (default provider) | platform.claude.com |
| Groq API key | Optional "fastest" preset | console.groq.com |
| Python 3.12 or 3.13 | Running from source / building | CI tests both; release builds use 3.13. |
| Node.js 22 + npm | Building the frontend | Only for source runs and release builds. |
| Inno Setup 6 | Optional installer | Only if you want a `Setup.exe`; the PyInstaller folder build is a complete app on its own. `winget install JRSoftware.InnoSetup`. |

---

## 2. Just use the app (packaged build)

1. Unzip or install the release (`AICallAssistant-Setup-<version>.exe`, or
   the `AICallAssistant\` folder). The installer is per-user: no admin
   rights, installs under `%LOCALAPPDATA%\Programs`.
2. Run `AICallAssistant.exe`. Windows SmartScreen may show "Windows
   protected your PC" for a new or unsigned download; see §5.
3. The window opens **docked at the top-centre of your primary display** on
   first run — that is the camera line, on purpose.
4. First run shows *"First run: open Settings (gear icon) and add your API
   keys"*. Click the gear, paste your Deepgram and Anthropic keys, press
   **Save**, then **Back**. Keys are encrypted with Windows DPAPI for your
   Windows user account and are never displayed again. If Windows cannot
   encrypt a key, the save fails with an explicit error and nothing is
   written — the app never stores a new key unencrypted. Profiles (resume,
   job description, notes) are stored as ordinary, unencrypted JSON.
5. Read the [User Guide](USER-GUIDE.md) for the recording flow, prompter
   mode and profiles.

Where things live:

| Path | What |
| --- | --- |
| `%APPDATA%\AICallAssistant\settings.json` | Settings, profiles (plain JSON), DPAPI-encrypted keys, window geometry |
| `%APPDATA%\AICallAssistant\settings.json.*.bak` | Only if settings.json could not be read at startup: the original bytes, preserved before the app wrote anything |
| `%APPDATA%\AICallAssistant\crash.log` | Crash and background-error log. Its size is checked at startup only: past 1 MB it becomes `crash.log.1` (one previous generation kept). |

Uninstalling (installer build) or deleting the folder (zip build) removes the
program; delete `%APPDATA%\AICallAssistant` to remove your settings and keys.

What leaves the machine: while recording, the captured system audio goes to
Deepgram (already-captured audio may finish uploading just after Stop); on
each answer, the prompt (active profile, call type, style and the question)
goes to the answer provider you chose. The app also sends an
unauthenticated warm-up request to that provider's public models endpoint
on Record, Ask and Stop so a connection is ready. There is no server of
ours and no telemetry.

---

## 3. Run from source (developer setup)

Python dependencies are declared in `pyproject.toml` and pinned, including
transitive ones, in `constraints.txt`. CI installs with exactly the same
commands, so a green local run and a green CI run test the same set.

```powershell
cd C:\path\to\aicallhelper2

# 1. Python core (pinned)
python -m venv .venv
.venv\Scripts\python -m pip install -c constraints.txt -e ".[dev,build]"

# 2. Frontend (lockfile-exact install, then a build the app loads from disk)
npm --prefix frontend ci
npm --prefix frontend run build

# 3. Run
.venv\Scripts\python app.py
```

Or run steps 1–2 with `powershell -ExecutionPolicy Bypass -File build.ps1 -Bootstrap`.

Development loop with hot reload (two terminals):

```powershell
# terminal 1
npm --prefix frontend run dev    # Vite dev server on http://localhost:5173

# terminal 2
$env:AICA_DEV_URL = "http://localhost:5173"
.venv\Scripts\python app.py
```

Set `$env:AICA_DEBUG = "1"` to open the window with WebView2 devtools enabled.

To change a dependency: edit the range in `pyproject.toml` if needed, change
the pin in `constraints.txt`, reinstall with the command above, run the full
gate, and commit both files together. `tools/release_meta.py check` fails if
a declared dependency has no pin.

### Checks before you commit

```powershell
.venv\Scripts\python tools\release_meta.py check   # one version everywhere, deps pinned
.venv\Scripts\python -m ruff check app_core app.py tests tools
.venv\Scripts\python -m mypy                       # strict, on the core + tools
.venv\Scripts\python -m pytest tests -q            # core tests (no network, no audio device)
npm --prefix frontend run typecheck                # TypeScript strict
npm --prefix frontend test                         # frontend tests (jsdom)
```

`build.ps1` runs all of these and then builds; see section 4.

### If startup or the test suite feels very slow on your machine

On one development machine used for this project, `import app` alone took
12–16 s and the core suite took 2.5 minutes. That was diagnosed as
machine-specific: the system Python at `C:\Python313\Lib` had no compiled
bytecode cache (the folder is not user-writable) and every file open was
slow (real-time antivirus scanning was the suspected cause). How fast a
normal machine starts has **not** been measured; do not quote a number.

Measure before changing anything:

```powershell
Measure-Command { .venv\Scripts\python -c "import app" }
.venv\Scripts\python -X importtime -c "import app" 2> importtime.txt
```

If the standard library shows no `__pycache__`, compiling it once from an
**elevated** PowerShell is safe and narrowly scoped:

```powershell
C:\Python313\python.exe -m compileall -q -j 0 C:\Python313\Lib
```

Antivirus exclusions are **not** recommended as routine setup. If you
suspect scanning, confirm it first (for example by briefly pausing
real-time protection and re-measuring), and if you decide an exclusion is
worth it on a developer machine, keep it to the narrowest folder
(`.venv`), never to end users' data folders. No project step changes
system protection settings.

The packaged build (section 4) is affected much less: PyInstaller ships
compiled bytecode inside one archive, so it opens far fewer files.

---

## 4. Build a release

The full procedure, including what to record and the native validation
matrix, is [RELEASE-CHECKLIST.md](RELEASE-CHECKLIST.md). The build itself
is one command:

```powershell
powershell -ExecutionPolicy Bypass -File build.ps1 -Bootstrap   # fresh clone
powershell -ExecutionPolicy Bypass -File build.ps1              # existing .venv
# -SkipChecks skips the test/lint gates (never for a release);
# -SkipInstaller skips Inno Setup even if it is installed.
```

Output: `dist\AICallAssistant\AICallAssistant.exe` plus its `_internal`
folder — a complete, portable app — and, when Inno Setup 6 is installed,
`dist\AICallAssistant-Setup-<version>.exe`.

What the build does, step by step (see `build.ps1` and `aica.spec`):

1. `tools/release_meta.py check` — fails on version drift or an unpinned
   dependency, before anything is built.
2. `ruff`, `mypy --strict`, `pytest`, `tsc --noEmit`, `vitest` — any failure stops the build.
3. `npm run build` in `frontend/` → `frontend/dist/` (the page the app loads).
4. `python -m PyInstaller aica.spec --noconfirm` — a windowed (no console)
   **onedir** build. `frontend/dist` is bundled as data; `pyaudiowpatch`,
   the `win32*` modules and pywebview's `edgechromium`/`winforms` backends
   are hidden imports because the app imports them lazily. The spec stamps
   the version from `pyproject.toml` into the exe's Windows version
   resource and bundles `_internal\build_info.json` (version, git
   revision, whether the working tree was dirty, build time, Python
   version, pinned dependency set).
5. `ISCC.exe /DAppVersion=<version> installer.iss`, if Inno Setup is found.

**The version lives in one place: `pyproject.toml`.** `frontend/package.json`,
its lockfile and the fallback `#define AppVersion` in `installer.iss` must
match it; `tools/release_meta.py check` (CI, `build.ps1`,
`tests/test_release_metadata.py`) fails if they do not. To bump: change
all four, run `npm --prefix frontend install --package-lock-only`, then the
check.

A build from a dirty working tree records `"dirty": true` in
`build_info.json`. Do not ship one: commit first so the revision identifies
the source exactly.

### Verifying a build before shipping it

Follow [RELEASE-CHECKLIST.md](RELEASE-CHECKLIST.md). The four checks that
matter most for a release:

1. Share your screen in each supported call app and sharing mode — the
   window must not appear in the share, and the app must show "Hidden from
   screen capture", not the "Windows would not hide this window" alert.
2. Press the global hotkey with another app focused — recording toggles.
3. Launch the exe a second time — the first instance comes to the front.
4. Enter prompter mode, quit, relaunch — the strip comes back where you
   left it.

---

## 5. Deploying to other people

There is no server component. Each user runs the app locally with their
own API keys. Their audio goes to Deepgram and their prompt (profile +
question) to the answer provider they chose, as described in §2.

Hand-out options, from simplest to most polished:

| Option | What you ship | Notes |
| --- | --- | --- |
| Zip of `dist\AICallAssistant\` | One folder | Users run `AICallAssistant.exe` directly. Unsigned: SmartScreen will usually warn on first launch. |
| `AICallAssistant-Setup-<version>.exe` | Inno Setup installer | Per-user install (`PrivilegesRequired=lowest`, no UAC prompt), Start-menu and optional desktop shortcut. Unsigned unless you add a certificate. |
| Signed installer | Same, code-signed | Sign `AICallAssistant.exe` after PyInstaller and before `iscc`, then sign the installer (or add `SignTool=` to `installer.iss`). |

**Code signing and SmartScreen are separate things.** Signing proves who
published the file and that it was not altered; it does **not** by itself
remove the SmartScreen warning. SmartScreen decides from the reputation of
the file and its signing certificate, which builds up as users download
and run it; a newly signed file, or a file signed with a new certificate,
can still be warned about. What helps: sign every release with the same
certificate, timestamp the signature (`signtool sign /fd sha256 /tr
<timestamp-url> /td sha256 ...`), keep the publisher name stable, and
submit false positives to Microsoft. See Microsoft's SmartScreen
reputation guidance before promising users a warning-free install.

Antivirus: an unsigned PyInstaller bundle is occasionally flagged
heuristically. If that happens, submit the exact file to the vendor as a
false positive; do not tell users to disable protection or add broad
exclusions.

Things to tell recipients:

- Add their own API keys in Settings; keys are DPAPI-encrypted per Windows
  user and cannot be copied between machines (a copied `settings.json`
  shows the keys as "not set" and asks again — by design). Profile text is
  not encrypted.
- The app captures **everything** played on the default output device, not
  only the other person. Mute notifications and other audio while
  recording. Headset users should make the headset the default output; the
  app shows "No call audio detected" after ~5 s of silence to catch this.
- Screen-capture exclusion is what Windows reports for the window, not a
  guarantee for every capture method. Check the status indicator ("Hidden
  from screen capture") before sharing, and test the call app they use
  once. If the app ever shows the "may be visible" alert, or says
  protection is not confirmed yet, do not rely on it being hidden.
- Answers use only the active profile and the current question; earlier
  answers are not context. At most 20 profiles can be saved.

---

## 6. Continuous integration

`.github/workflows/ci.yml` runs on every push and pull request:

- **metadata** — `tools/release_meta.py check` (stdlib only).
- **core** on `windows-latest`, Python **3.12 and 3.13** — installs
  `-c constraints.txt -e ".[dev]"`, then ruff, mypy, pytest.
- **frontend** on `ubuntu-latest` — `npm ci`, `npm audit --omit=dev
  --audit-level=high`, typecheck, vitest, build.
- **package** on `windows-latest`, after the three above — installs the
  pinned `.[build]` set (PyInstaller) and Inno Setup (`choco install
  innosetup`) explicitly, builds the frontend, the exe and the installer,
  checks the exe's `ProductVersion` equals `pyproject.toml`, and uploads
  `AICallAssistant-<version>-<commit>` as a workflow artifact.

No job touches the network beyond package installation, a live provider,
or an audio device, so CI needs no secrets. CI does **not** launch the
packaged app, test capture exclusion, or talk to real providers; those are
manual release steps in [RELEASE-CHECKLIST.md](RELEASE-CHECKLIST.md). The
uploaded artifact is unsigned.

---

## 7. Troubleshooting setup

| Symptom | Cause | Fix |
| --- | --- | --- |
| Window opens but stays blank/dark | WebView2 runtime missing or blocked; or (source run) `frontend/dist` not built | Install the Evergreen WebView2 Runtime; run `npm --prefix frontend run build`; check `crash.log`. |
| "Could not open the system audio device" | No default output device, or PyAudioWPatch missing from the venv | Pick a default output in Windows Sound settings; reinstall with the pinned command in §3. |
| "Ctrl+Shift+Space is already taken by another app" | Another program registered the same global hotkey | Change it in Settings (any `Ctrl/Alt/Shift/Win + key` combination). |
| "Windows would not hide this window" alert | Windows older than 10 2004, or a policy/driver blocking display affinity | Update Windows; otherwise treat the window as visible in shares. |
| "The app is still starting up" when you press Record | The heavy core (audio + network stacks) loads on a background thread after the window appears | Wait a moment. If it instead says "The app core failed to start…", read `crash.log` (source installs: a missing dependency). |
| "Windows could not encrypt the API key, so it was NOT saved…" | DPAPI refused (rare; profile or system issue) | Try again; restart Windows. Your previously saved key is unchanged. |
| Save in Settings fails with "Something went wrong inside the app core" | A generic internal error — an unexpected exception or a timed-out command, not one specific cause | Check `crash.log` for a traceback at that time and that `%APPDATA%\AICallAssistant` is writable. See `docs/TROUBLESHOOTING.md`. |
| `pip install` fails resolving pins | A pin has no wheel for your Python | Use Python 3.12 or 3.13 (what CI tests). |

For runtime problems during a call, see `docs/TROUBLESHOOTING.md`.
