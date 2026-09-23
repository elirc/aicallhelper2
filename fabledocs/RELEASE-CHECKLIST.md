# AI Call Assistant — Production Release Checklist

Use this for every build that leaves a developer machine. It turns the
review's release criterion ("truthful state and preservation of user work
under failure, followed by demonstrated latency and answer quality";
`PROJECT-REVIEW-2026-09-19.md` §6) into steps with exact commands and a
record that identifies the exact artifact tested.

The offline gates (section 2) prove the logic with fakes. They **cannot**
prove capture exclusion in real call apps, placement on real monitors, real
device behavior, WebView2 behavior, or provider behavior. Section 4 is the
native/manual matrix for exactly those; a release is not done until it is
filled in for the artifact being shipped.

All commands run from the repository root in PowerShell.

---

## 0. Release record (fill in, keep with the release)

Copy this block into the release notes or a `release-<version>.md` next to
the artifacts.

```text
Version:                 (python tools\release_meta.py version)
Source revision:         (git rev-parse HEAD)          dirty? must be "false"
Artifact:                AICallAssistant-Setup-<version>.exe  SHA256: ...
                         AICallAssistant\ folder zip          SHA256: ...
Built by / where:        local build.ps1 | CI run URL
build_info.json:         (paste dist\AICallAssistant\_internal\build_info.json)
Signed:                  no | yes (certificate subject, timestamp URL)
Test machines:           Windows edition + build (winver), WebView2 version,
                         displays (count, resolution, scaling %), audio devices
Offline gates:           pass counts for pytest / vitest, ruff, mypy, tsc
Native matrix (§4):      pass/fail per row, with notes
Live provider smoke (§5): date, providers, result, observed first-word times
Known issues shipped:    ...
```

Useful one-liners for the record:

```powershell
python tools\release_meta.py version
git rev-parse HEAD; git status --porcelain --untracked-files=normal   # must print nothing after the hash
Get-FileHash dist\AICallAssistant-Setup-*.exe -Algorithm SHA256
(Get-Item dist\AICallAssistant\AICallAssistant.exe).VersionInfo | Format-List ProductVersion, FileVersion
Get-Content dist\AICallAssistant\_internal\build_info.json
[System.Environment]::OSVersion.Version; (Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion').DisplayVersion
(Get-ItemProperty 'HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}' -ErrorAction SilentlyContinue).pv   # WebView2 runtime version (per-machine install)
```

---

## 1. Prepare the source

1. Working tree clean and reviewed: `git status` shows nothing to commit.
   Review the intended diff (`git diff main...HEAD`) — do not stage with
   `git add -A` blindly.
2. Bump the version if needed, in **all** of: `pyproject.toml`
   (`[project] version`), `frontend/package.json`, the fallback
   `#define AppVersion` in `installer.iss`; then refresh the lockfile and
   verify:
   ```powershell
   npm --prefix frontend install --package-lock-only
   python tools\release_meta.py check
   ```
3. Dependency review:
   ```powershell
   npm --prefix frontend audit
   npm --prefix frontend audit --omit=dev --audit-level=high    # the CI gate: must pass
   .venv\Scripts\python -m pip install pip-audit                 # or run from a separate venv
   .venv\Scripts\python -m pip_audit --path .venv\Lib\site-packages
   ```
   Record any accepted findings (for example dev-only tooling advisories)
   in the release record with the reason.
4. Provider model check: the pinned models (`MODEL` in
   `app_core/llm/anthropic.py` and `app_core/llm/groq.py`) are still
   offered by the providers (check their model lists). A retired Groq model
   is a user-visible failure until the next release.
5. Commit, and tag after §§3–5 pass: `git tag v<version>`.

## 2. Offline gates (same as CI)

```powershell
powershell -ExecutionPolicy Bypass -File build.ps1 -Bootstrap
```

This runs, and stops on the first failure: the release-metadata check,
`ruff`, `mypy --strict`, `pytest`, `tsc`, `vitest`, the frontend build,
PyInstaller, and Inno Setup if `ISCC.exe` is installed. Equivalent CI:
push the commit and confirm every job in `.github/workflows/ci.yml` is
green on Python 3.12 **and** 3.13, including the **package** job.

## 3. Build the artifacts

Either use the CI **package** job's artifact
(`AICallAssistant-<version>-<sha>`) or build locally:

```powershell
powershell -ExecutionPolicy Bypass -File build.ps1 -SkipChecks   # only if §2 just passed on this commit
```

Confirm:

- `dist\AICallAssistant\_internal\build_info.json` has the tagged
  revision and `"dirty": false`.
- Explorer → `AICallAssistant.exe` → Properties → Details shows the
  version.
- Optional signing (see SETUP-AND-DEPLOY.md §5): sign the exe **before**
  running `iscc`, then sign the installer; verify with
  `signtool verify /pa /v dist\AICallAssistant-Setup-<version>.exe`.
  Signing does not guarantee a SmartScreen-free first run.

## 4. Native / manual validation matrix

Run on the **packaged** artifact, installed with the installer, on a
machine that has never had a development setup (a clean Windows VM or
spare machine is best). Use a throwaway settings folder: rename
`%APPDATA%\AICallAssistant` first and restore it afterwards.

### 4.1 Install and first run

| # | Step | Pass when |
| --- | --- | --- |
| I1 | Run the installer without admin rights | No UAC prompt; installs under `%LOCALAPPDATA%\Programs\AI Call Assistant`; Start-menu entry exists |
| I2 | First launch | SmartScreen behavior recorded (expected for unsigned/new builds); window opens top-centre of the primary display; status shows the first-run hint |
| I3 | Before keys are saved | Protection indicator is visible; Record reports the missing key, nothing crashes |
| I4 | Settings → paste keys → Save | "Saved"; `settings.json` contains `enc:` values and no plaintext key (`Select-String -Path $env:APPDATA\AICallAssistant\settings.json -Pattern 'plain:'` returns nothing) |
| I5 | Upgrade: install the new version over the previous release | Single entry in Apps & features; settings, profiles and keys survive |
| I6 | Uninstall | Program folder removed; `%APPDATA%\AICallAssistant` kept (documented) |

### 4.2 Screen-capture exclusion (the moat — never skip)

For each call app, share in each mode, and look at what the **remote
side** receives (a second machine or a second account in the meeting — the
local preview is not proof). The app window must be absent from the
received picture in every "must hide" cell, and the app's indicator must
read "Hidden from screen capture". Test both the full window and the
prompter strip, and with Settings open.

| App (record version) | Entire screen | Single window (other app) | Browser tab | Notes |
| --- | --- | --- | --- | --- |
| Zoom desktop | must hide | n/a (window not shared) | n/a | also check Zoom "share computer sound" does not echo into capture |
| Microsoft Teams (new) | must hide | n/a | n/a | |
| Google Meet in Edge | must hide | n/a | must hide | |
| Google Meet in Chrome | must hide | n/a | must hide | |
| OBS Studio Display Capture | must hide | — | — | also Window Capture of another app over the strip |
| Windows Snipping Tool / Win+Shift+S / PrintScreen | must hide | — | — | quick local sanity check only |

Also:

| # | Step | Pass when |
| --- | --- | --- |
| P1 | Start a share, then launch the app | Indicator goes unknown → hidden; window never appears in the share |
| P2 | Toggle prompter mode during an active share | Strip never appears |
| P3 | Renderer reload during a share (kill the `msedgewebview2.exe` child owned by the app, or wait for the watchdog) | Window stays hidden; the page shows the verdict again after reload |
| P4 | On a Windows build / VM where display affinity is refused (for example a Windows 10 build older than 2004, or an RDP session) | The alert "Windows would not hide this window…" is shown in full view, prompter and Settings |

Known limits to record, not fix: remote-desktop sessions, hardware
capture cards and cameras pointed at the screen are not covered by display
affinity.

### 4.3 Displays and DPI

| # | Setup | Step | Pass when |
| --- | --- | --- | --- |
| D1 | Single display at 100 %, 125 %, 150 % | Launch, move, resize, quit, relaunch | Geometry restored; text crisp; window fully reachable |
| D2 | Two displays, mixed scaling (e.g. 100 % + 150 %) | Full view on A, prompter on B, switch back and forth 5 times | Each layout returns to its own place on its own display |
| D3 | Secondary display LEFT of / ABOVE the primary (negative coordinates) | Same as D2 | Correct placement, no off-screen window |
| D4 | Full view on A, prompter on B, then unplug A; switch to full view | Window docks on a connected display with a reachable title bar |
| D5 | Unplug the display the window is on while the app is closed; relaunch | Window appears on a remaining display |
| D6 | Prompter at minimum width (380 px) with history, a long error, and A+ at 28 px | Stop, Exit and Dock stay reachable; text scrolls with keyboard (Page Down / Space) |
| D7 | Minimize, quit from the taskbar, relaunch | Restores last visible geometry, not (-32000, -32000) |

### 4.4 Audio devices

| # | Step | Pass when |
| --- | --- | --- |
| A1 | Speakers as default output; play a YouTube interview; Record → Stop | Live transcript; answer streams; the last word before Stop is in the transcript |
| A2 | Headset as default output, call on headset | Same as A1 |
| A3 | Call routed to a non-default device | "No call audio detected yet" hint after ~5 s |
| A4 | Unplug the default output device mid-recording | Silence hint appears; Stop still works; no crash; next Record uses the new default |
| A5 | No output device at all (disable all in Sound settings) | "Could not open the system audio device…" |
| A6 | Bluetooth headset switching between hands-free and stereo profiles | Record still captures after the switch (start a fresh recording if needed); record the behavior |
| A7 | Notification sound during recording | Appears in the transcript (expected: all output audio is captured) — confirms the documented behavior |
| A8 | Very short recording (< 0.2 s of speech) and Stop immediately after Record | No hang; either a transcript or "No speech detected" |
| A9 | Let a recording reach the 120 s cap | Countdown matches; auto-stop; the answer arrives |

### 4.5 Shell and lifecycle

| # | Step | Pass when |
| --- | --- | --- |
| S1 | Global hotkey with another app focused | Recording toggles |
| S2 | Hotkey already taken by another app | "already taken" notice, Record button still works |
| S3 | Launch the exe a second time | First instance comes to the front |
| S4 | Close the window with unsaved Settings edits | Prompted once; a second close within 10 s closes |
| S5 | Corrupt `settings.json` (write `{` into it), launch, move the window, quit | A `settings.json.*.bak` holds the original bytes; the app told the user |
| S6 | Make `%APPDATA%\AICallAssistant` read-only, launch, change a setting | Save fails with a clear message; no crash |
| S7 | Rename `_internal\frontend` in the install folder, launch | Fails visibly (blank or error), `crash.log` explains; restore afterwards |
| S8 | Leave the app minimized 10+ minutes, restore | Page not reloaded needlessly; history intact |
| S9 | Keyboard only: Tab through the main view, Settings, prompter; Narrator on | Controls reachable and announced |

## 5. Live provider smoke test (costs a few cents)

Use real keys on the test machine, never CI secrets. Budget: well under
US$0.10 per run.

| # | Step | Pass when |
| --- | --- | --- |
| L1 | Anthropic: Record a spoken question (A1), Stop | Answer streams; chip shows first-word time; record it |
| L2 | Anthropic: Ask a typed question; Regenerate | Two history entries; drain/finalize metrics are 0 for the typed ask |
| L3 | Groq: switch provider, repeat L1 and L2 | Same; if Groq returns the retired-model message, the pinned model must be updated before release |
| L4 | Wrong Anthropic key | "Anthropic rejected the API key (401)…" |
| L5 | Wrong Deepgram key | "Deepgram closed the connection (code 1008 …)" |
| L6 | Disconnect the network after Record | Actionable error, UI returns to idle, Record works after reconnecting |
| L7 | Detailed style with a question inviting a long answer | Answer completes, or is kept and marked as cut off at the length limit |
| L8 | Five consecutive answers, each profile/call type in use | Record first-word times (p50 / worst) in the release record; compare with the previous release |

Do not claim a latency number in release notes that §5 did not measure on
the recorded hardware.

## 6. Ship

1. Tag and push: `git tag v<version>; git push origin v<version>`.
2. Publish the installer and/or zip with their SHA256 hashes and the
   release record.
3. Keep the exact artifacts that were tested; never rebuild "the same
   version" for distribution.

## 7. After release

- Watch for SmartScreen / antivirus reports; submit false positives with
  the exact file.
- A provider model retirement is handled by a new release (update the
  pinned model, re-run §5).
