# TROUBLESHOOTING.md — symptom-driven diagnosis

Organised by what you SEE, not by subsystem. Every quoted message below is
the exact text the app shows; error boxes quote the core's message verbatim
(`app_core/errors.py` — the message is actionable copy, never a raw
traceback). When a message names an HTTP status or a close code, that is
the real status the provider returned, not a guess.

## Nothing happens when I press Record / no transcript appears

What it looks like: you press Record, and either an error box appears, the
status sticks at "Starting system-audio capture…", or recording starts but
the "Question heard" panel stays on "Listening…".

Check in this order:

1. **An error box names the cause.** "No Deepgram API key saved. Open
   Settings (gear icon) and add it." means exactly that — recording never
   starts without both a Deepgram key and a key for the selected answer
   provider (the key checks run before anything else,
   `app_core/session/machine.py`).
2. **"Timed out connecting to Deepgram. Check the API key and your
   network."** — the WebSocket connect has a 5 s cap. Network problem or a
   proxy/firewall blocking `wss://api.deepgram.com`.
3. **"Deepgram closed the connection (code 1008 …). Check the API key and
   your network."** — Deepgram rejects a bad key by CLOSING the socket,
   usually with a 1008 DATA-xxxx code and often no error frame at all
   (`app_core/stt/client.py`). A close code in the message plus no
   transcript ever = re-check the Deepgram key in Settings.
4. **"Could not open the system audio device. Check that a default output
   device exists, then try again."** — Windows has no default output device
   (or it vanished). Plug one in / pick a default output in Windows sound
   settings, then press Record again.
5. **Recording runs, timer ticks, but the level meter never moves** — you
   are capturing silence, not failing. See the silent-capture section
   below; the hint appears on its own after ~5 s.

Note the app captures what plays through your DEFAULT OUTPUT device
(WASAPI loopback) and never opens your microphone. It captures EVERYTHING
that device plays — the other person's voice, but also notifications, music,
or any other app's audio — and all of it is sent to Deepgram for
transcription while you record.

## "No speech detected in the recording" at Stop

Exact text: "No speech detected in the recording. Make sure call audio is
playing." The finalized transcript came back empty, and the core refuses
to send an empty prompt to the LLM (rule 7, `_finalize_and_answer` in
`app_core/session/machine.py`).

Most likely causes, in order:

1. The call audio played somewhere other than the default output — a
   headset, a second monitor's speakers, a muted call. The level meter
   would have stayed flat and the silence hint shown during recording.
2. Audio played but contained no speech (hold music, a silent screen
   share).
3. You recorded a stretch where nobody spoke. Pressing Record early is
   fine — frames captured before the Deepgram socket opens are buffered
   (~15 s) and flushed in order — but silence in is silence out.

Fix: make the call play through the device Windows lists as the default
output, then record again. The meter moving during recording is your
confirmation.

## The silent-capture hint appears mid-recording

Text: "No call audio detected yet — check that the call is playing through
your speakers, not a headset or another output device."

It appears when no audible frame (RMS above 0.003) has arrived for ~5
seconds of recording, and clears the instant audio flows
(`frontend/src/App.tsx`, `SILENCE_HINT_AFTER_S`). It tracks WHEN audio was
last heard, not merely whether it ever was — so it also fires when a
device is unplugged mid-question or Windows switches the default output:
the capture stream stays bound to the dead endpoint and delivers silence
with no error.

Causes, in order: call audio routed to a headset or a non-default device;
the wrong default output selected in Windows; the call itself muted; the
output device unplugged mid-recording. Capture binds the default output at
Record time, so after changing devices, stop and start a fresh recording.

## "The shortcut is off"

Two different notices under the Record button, for two different problems
(`HotkeyStatus` in `app_core/bridge/hotkey.py` — deliberately not one
message):

- **"… isn't a shortcut Windows understands, so it's off — try something
  like Ctrl+Shift+Space in Settings."** — `invalid`: the accelerator text
  does not parse. Typos, two non-modifier keys, a non-ASCII key, F-keys
  past F24. Fixing the text in Settings fixes it; there is no conflict to
  hunt for.
- **"… is already taken by another app, so the shortcut is off — record
  from this window, or pick a different one in Settings."** —
  `unavailable`: the accelerator is well-formed but Win32 `RegisterHotKey`
  refused it, which means another running app owns that exact combination.
  Close the other app or pick a different combination.

No notice appears when the hotkey field is empty — that is the deliberate
"disabled" state, not a failure. The Record button in the window always
works regardless of hotkey status.

## The screen-capture warning appears

The protection status is shown in every view (full window, prompter strip
and Settings) as one of three states (`frontend/src/components/ProtectionNotice.tsx`):

- **"Hidden from screen capture"** — Windows reported the exclusion as set.
- **"Screen-share protection not confirmed yet"** — Windows has not
  answered yet (normal for a moment at startup or after a page reload).
  Do not share your screen until it changes.
- **Alert: "Windows would not hide this window from screen capture, so it
  may be visible if you share your screen."** (in the prompter: "Not hidden
  from screen share — Windows refused to protect this window.")

The app sets `WDA_EXCLUDEFROMCAPTURE`, then READS THE AFFINITY BACK and
retries (`app_core/bridge/protection.py`, applied from `app.py`). The alert
means the OS never confirmed the exclusion. Treat the window as visible in
any share.

Causes: Windows 10 builds older than 2004, which lack
`WDA_EXCLUDEFROMCAPTURE` entirely; a policy or graphics driver that
refuses it. Fix: update Windows. There is no in-app workaround — a user
who believes they are hidden while being broadcast is this product's worst
outcome, which is why the failure is a standing warning instead of a log
line.

"Hidden" is what Windows reports, not a guarantee for every capture
method: Microsoft documents display affinity as protection against
ordinary screen capture, and a capture path that ignores it (some
hardware capture, remote-desktop setups, a phone camera pointed at the
screen) still sees the window. Check the call app and sharing mode you
actually use once before an important call; the release checklist
(`fabledocs/RELEASE-CHECKLIST.md`) lists the combinations verified for a
release.

## Auth errors, per provider

Every message reports the ACTUAL status — a 403 labelled 401 sends you
debugging the wrong thing.

**Deepgram** never returns an HTTP auth status — it closes the WebSocket.
A bad key looks like "Deepgram closed the connection (code 1008 …)", even
if it appears only when you press Stop (a close before any transcript
keeps its connect-failure classification, `app_core/stt/client.py`).

**Anthropic** (`app_core/llm/anthropic.py`):

- 401 "Anthropic rejected the API key (401). Check it in Settings." — the
  key is wrong, revoked, or truncated in paste. Re-enter it.
- 403 "Anthropic refused the request (403): this API key is not allowed to
  use the model. Check the key in Settings." — the key is REAL but its
  workspace cannot use `claude-haiku-4-5`. Fix on the Anthropic console,
  not in the app.
- 429 "Anthropic rate limit hit (429). Wait a moment and try again, and
  check your credit balance." — Anthropic returns 429 both for request
  rate and for exhausted credit; the balance is the usual culprit.
- 529 "Anthropic is overloaded (529). Try again in a moment." — their side;
  retry.

**Groq** (`app_core/llm/groq.py`): 401 and 403 both read "Groq rejected
the API key (…). Check it in Settings." with the real status shown; 429 is
"Groq rate limit hit (429). Wait a moment and try again."

Paste hygiene: keys are trimmed on save and must be plain ASCII — Settings
refuses a key containing smart quotes or hidden characters at save time
("API keys must be plain ASCII…"), because a non-ASCII key would otherwise
fail uselessly mid-answer inside an HTTP header.

## "Groq no longer offers the model this version of the app uses"

Full text: "Groq no longer offers the model this version of the app uses
(openai/gpt-oss-120b); Groq retires models on short notice. Switch the
answer provider to Claude in Settings, or install the latest version of the
app."

Shown for a Groq 404, or a 400 whose body says the model is gone
(`app_core/llm/groq.py`). **Users:** switch the answer provider to Claude
in Settings and keep working; update the app when a new version is out.
**Maintainers:** the model is pinned in ONE constant, `MODEL` in
`app_core/llm/groq.py`. Check Groq's current model list, update the
constant, run the gates and the live Groq smoke test from
`fabledocs/RELEASE-CHECKLIST.md`, and ship a new release.

## The answer ends with an error instead of text

HTTP 200 is not treated as success on its own. The provider adapters check
the provider's own end-of-answer signal (Anthropic `message_stop`; Groq
`finish_reason` / `[DONE]`), so these now surface as errors rather than as a
blank or silently cut-off answer:

- "Anthropic/Groq reported an error during the answer. …" — the provider
  sent an error event inside an otherwise successful stream (for example
  Anthropic's `overloaded_error`). Try again in a moment.
- "The answer stopped before … finished it (the stream ended early). Try
  again." — the connection ended before the provider's terminal event. Any
  text that already streamed stays visible.
- "… finished without writing an answer. Try again." / "… hit its length
  limit before writing any answer text." / "… declined to answer this one.
  Try rephrasing the question." — the stream finished but contained no
  usable text; the reason is in the wording.

None of these is retried automatically once text has reached the screen
(a second attempt would splice two answers). An answer that DID produce
text but hit the 1,024-token limit is kept, and `llm:done` reports
`finish: "truncated"` for it.

## The answer is slow

The latency chip on the answer ("X.Xs to first word") is the diagnostic.
Hover it for the breakdown (`frontend/src/format.ts`). The numbers come
from `llm:done`'s `metrics` (`app_core/session/machine.py`):

- `firstTokenMs` / `totalMs` run from the moment the core ACCEPTED Stop to
  the first answer delta / the end of the answer. They deliberately include
  the audio drain and device teardown, because the user waits for those.
- `audioDrainMs` — Stop acceptance until every pre-Stop audio sample was
  delivered and the capture device closed (bounded at 2 s).
- `sttFinalizeMs` — drain complete until Deepgram's final transcript
  (bounded at ~5 s).

For typed questions the clock starts at submit and both `audioDrainMs` and
`sttFinalizeMs` are 0. The numbers are measured in the core: bridge
transit and rendering are not included, so the first word appears on
screen slightly later than the chip says.

Read it like this:

- **The drain dominates**: a slow audio device stop. Rare; note the device
  in a bug report.
- **Transcript finalization dominates** (it can reach ~5000 ms — the cap):
  Deepgram's end-of-stream flush is slow, usually your network path to
  Deepgram.
- **First word minus (drain + finalization) dominates**: the LLM side. The
  provider origin is pre-warmed at Record AND again at Stop so a pooled
  TLS connection should be waiting; a proxy, a network that drops idle
  connections, or provider-side load raises it. Groq is the "fastest"
  preset if Anthropic is consistently slow for you.
- **Full answer long but first word fast**: the answer is just long.
  Detailed style produces longer answers; completions are capped at 1024
  tokens either way.

Hard stops: "The answer didn't start streaming within 10 seconds. Try
again." and "The answer took longer than 60 seconds and was stopped."
(`app_core/session/machine.py`, `Timeouts`).

Related: if you press Stop at the same moment the 120 s cap fires, the
status shows "Reached the 120s limit — answering now" and the answer still
arrives — the frontend keeps waiting after a refused stop instead of
cancelling a session that is mid-finalize, and stops waiting as soon as the
answer starts streaming (`frontend/src/App.tsx`). The 120 s cap is counted
from the moment capture is actually running, and the core tells the page
the deadline (`session:recording`), so the on-screen timer and the core's
cap agree.

## The window opens in the wrong place, or geometry isn't remembered

The app cannot restore off-screen: on launch a saved position is kept only
if at least 40 px of the window lands on some display's work area on both
axes; otherwise the position is dropped and Windows centers the window —
this is the unplugged-monitor path (`app_core/store/bounds.py`). Negative
coordinates are valid (monitors left of the primary).

If geometry is not remembered:

- Saves are debounced 0.5 s after move/resize and flushed on close, so
  even a killed process keeps the last arrangement. Quitting while
  MINIMIZED does not lose it either: Windows reports minimized windows at
  (-32000, -32000) and the app refuses to persist that
  (`plausible_bounds` in `app.py`).
- A corrupt `windowBounds` in settings.json is dropped as a unit — you get
  a centered default-size window, and the rest of your settings survive.

If you cannot see the window at all, launch the exe again: the second
launch focuses and restores the first instance (see below).

## The app doesn't start, or a second launch does nothing

A second launch doing "nothing" is by design: the app is single-instance
(mutex `Local\AICallAssistantV3Mutex`, `app.py`). Launch #2 signals launch
#1 to restore and focus its window, then exits. If no window appears, the
first instance is wedged — end it in Task Manager and relaunch.

If the app never starts:

- Read `%APPDATA%\AICallAssistant\crash.log` — startup exceptions land
  there (see below).
- Running from source: the window loads `frontend/dist/index.html`, which
  exists only after `cd frontend && npm run build` (or set `AICA_DEV_URL`
  to a running Vite dev server). A missing dist is a blank window.
- A white/frozen page mid-session heals itself: the frontend heartbeats
  every 3 s, and a watchdog reloads the page after 15 s of silence, at
  most once per 10 s (`app.py`, `HEARTBEAT_STALE_S`). A stale heartbeat
  alone is not enough: the watchdog first probes the page with a direct
  `evaluate_js` and only reloads when that fails or hangs. Chromium
  throttles timers in a hidden page (once per minute after five minutes
  minimized), so a minimized window's heartbeat goes quiet while the page
  is perfectly healthy — reloading it would wipe the history.

## Where the logs and settings live

Everything is in `%APPDATA%\AICallAssistant\`:

- **settings.json** — profiles (resume, job description, focus, notes),
  provider/style/hotkey choices, window bounds, and API keys. Profile text
  is ordinary, unencrypted JSON. Keys are stored DPAPI-encrypted (`enc:`
  prefix, current-user scope). If Windows cannot encrypt a key, the save
  FAILS with "Windows could not encrypt the API key, so it was NOT saved…"
  and the previously saved key is left unchanged — this version never
  writes a new key in plaintext. Keys that an older build stored in its
  marked `plain:<base64>` fallback still work and are re-encrypted on the
  next successful save. Copying settings.json to another machine or user
  account makes `enc:` keys undecryptable — they read as UNSET (fail
  closed, `app_core/store/secrets.py`), so the app asks for keys again.
  Re-enter them; nothing else is lost.
- **settings.json.*.bak** — if settings.json exists but could not be read
  or parsed at startup, the app runs on defaults and, before its first
  write of any kind (including the automatic window-position save), copies
  the original bytes to a uniquely named `.bak` file next to it. If that
  copy cannot be made, the write is refused instead ("…a backup copy of it
  could not be made, so it was NOT overwritten…"). Recover profiles or
  keys from the `.bak` by hand.
- **crash.log** — timestamped tracebacks only: unhandled exceptions,
  thread exceptions, asyncio loop failures, and faulthandler output on a
  hard crash (`app.py`, `install_crash_logging`). The size is checked ONCE
  at startup: past 1 MB it is renamed to crash.log.1 (replacing the
  previous one), so a single long session can grow it beyond 1 MB.

crash.log deliberately never contains transcripts, answers, your resume or
job description, or API keys — the app has no request or content logging;
the only writers of that file are the crash hooks above. It is safe to
attach to a bug report as-is.

## Reporting a bug — what to collect

1. The exact error text, verbatim — it encodes the real cause (HTTP
   status, WebSocket close code, provider name).
2. What the status line said and what you pressed, in order ("Recording
   call audio…" → Stop → stuck at "Finalizing transcript…" is a different
   bug than never leaving "Starting system-audio capture…").
3. `%APPDATA%\AICallAssistant\crash.log` (safe to share — see above).
4. For slowness: the three numbers from the latency chip tooltip.
5. For capture problems: your audio setup (default output device, headset
   or speakers, what the call app plays through), whether the level meter
   moved, and whether the silence hint appeared.
6. For the screen-capture warning: your Windows version (`winver` —
   build 2004 is the floor for capture exclusion).
7. Whether you ran the packaged exe or from source, and the app
   version/commit. A packaged build records both in
   `_internal\build_info.json` next to the exe (also: Properties →
   Details on AICallAssistant.exe shows the version).


## The prompter strip is on the wrong monitor, or too small to read

The strip docks to the top-centre of **the display it is on**. Drag it onto
the monitor with the webcam and press **⤒ Dock under camera**. Resize it
freely (minimum 380x160); its size is remembered separately from the full
window's. Use **A+** for larger text (up to 28 px). If the strip ever lands
off-screen (a monitor was unplugged), relaunching docks it back on the
primary display.

## I ran an older build and my extra profiles disappeared

Older builds read only the top-level `resume`/`jobDescription` keys, which
this version keeps as mirrors of the **active** profile, and they rewrite
the whole file on every window move — so an older build that saves keeps
only the active profile. Restore the others from a backup of
`%APPDATA%\AICallAssistant\settings.json` if you have one; otherwise
re-create them in Settings. Don't run two builds against the same profile.

## "The app is still starting up — try again in a moment"

You pressed Record (or Ask) within the first seconds after launch. The
window appears before the audio and network stacks have loaded; commands
wait for them. If the core failed to load at all, commands say so
directly instead: "The app core failed to start, so recording and answers
are unavailable. Restart the app; details are in crash.log." Check
`crash.log` for a `core:` entry — a failed import (for example a missing
PyAudioWPatch in a source install) is logged there.

## "Something went wrong inside the app core." (for example on Save)

This is the catch-all for an unexpected exception or a command that
timed out inside the core; it does NOT by itself mean antivirus. Check, in
order: `crash.log` for a traceback at that time; that
`%APPDATA%\AICallAssistant` is writable and not full; whether a Settings
save reported a more specific message (encryption failure, profile limit,
backup failure). Only if saves intermittently fail with a sharing or
access-denied traceback in crash.log is real-time antivirus scanning of
`settings.json` a candidate — test that by briefly pausing scanning, not
by adding permanent broad exclusions.

## Stop pressed right after Record seems to "do nothing" for a moment

That is expected and safe: if you press Stop while the app is still
connecting to Deepgram, the audio captured so far is kept and the transcript
is finalized the instant the connection opens. (Older versions refused the
stop and sat in "Finalizing…" for 20 s; that is fixed.)
