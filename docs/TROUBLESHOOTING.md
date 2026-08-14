# TROUBLESHOOTING.md — symptom-driven diagnosis

Organised by what you SEE, not by subsystem. Every quoted message below is
the exact text the app shows; error boxes quote the core's message verbatim
(`app_core/errors.py` — the message is actionable copy, never a raw
traceback). When a message names an HTTP status or a close code, that is
the real status the provider returned, not a guess.

## Nothing happens when I press Record / no transcript appears

What it looks like: you press Record, and either an error box appears, the
status sticks at "Opening the microphone feed…", or recording starts but
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
(WASAPI loopback), not your microphone. It transcribes the other person's
voice as your speakers render it.

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

Text: "Windows would not hide this window from screen capture, so it may
be visible if you share your screen."

The app sets `WDA_EXCLUDEFROMCAPTURE`, then READS THE AFFINITY BACK and
tries up to 5 times (`app_core/bridge/protection.py`, applied from
`app.py`). This warning means the OS never confirmed the exclusion — it is
a verified fact, not a maybe. Treat the window as visible in any share.

Causes: Windows 10 builds older than 2004, which lack
`WDA_EXCLUDEFROMCAPTURE` entirely; a policy or graphics driver that
refuses it. Fix: update Windows. There is no in-app workaround — a user
who believes they are hidden while being broadcast is this product's worst
outcome, which is why the failure is a standing warning instead of a log
line. Conversely: no warning means Windows confirmed the exclusion.

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

## "Groq returned 404 — the model may have been retired"

Full text: "Groq returned 404 — the model may have been retired (Groq
retires models on short notice). Update the pinned model constant in
app_core/llm/groq.py."

The model is pinned in ONE constant — `MODEL = "openai/gpt-oss-120b"` in
`app_core/llm/groq.py` — precisely so this failure has a one-line fix.
Check Groq's current model list, update the constant, rebuild. Until then,
switch the answer provider to Anthropic in Settings and keep working.

## The answer is slow

The latency chip on the answer ("X.Xs to first word") is the diagnostic.
Hover it: "First word N ms after Stop · transcript finalized N ms · full
answer N.N s" (`frontend/src/format.ts`). All three run from the Stop
press; for typed questions the clock starts at submit and "transcript
finalized" is exactly 0.

Read it like this:

- **"transcript finalized" dominates** (it can reach ~5000 ms — the cap):
  Deepgram's end-of-stream flush is slow, usually your network path to
  Deepgram. This cost is bounded at 5 s by the core.
- **First word minus transcript-finalized dominates**: the LLM side. The
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
arrives — the frontend deliberately keeps waiting up to 20 s after a
refused stop instead of cancelling a session that is mid-finalize
(`STOP_RECOVERY_MS` in `frontend/src/App.tsx`). "Finalizing transcript…"
that outlives that window recovers to idle on its own.

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
  most once per 10 s (`app.py`, `HEARTBEAT_STALE_S`).

## Where the logs and settings live

Everything is in `%APPDATA%\AICallAssistant\`:

- **settings.json** — resume, job description, provider/style/hotkey
  choices, window bounds, and API keys. Keys are stored DPAPI-encrypted
  (`enc:` prefix, current-user scope); if DPAPI is ever unavailable they
  fall back to an honestly-marked `plain:<base64>`. Copying settings.json
  to another machine or user account makes `enc:` keys undecryptable —
  they read as UNSET (fail closed, `app_core/store/secrets.py`), so the
  app asks for keys again. Re-enter them; nothing else is lost.
- **crash.log** (rotated to crash.log.1 past 1 MB at boot) — timestamped
  tracebacks only: unhandled exceptions, thread exceptions, asyncio loop
  failures, and faulthandler output on a hard crash (`app.py`,
  `install_crash_logging`).

crash.log deliberately never contains transcripts, answers, your resume or
job description, or API keys — the app has no request or content logging;
the only writers of that file are the crash hooks above. It is safe to
attach to a bug report as-is.

## Reporting a bug — what to collect

1. The exact error text, verbatim — it encodes the real cause (HTTP
   status, WebSocket close code, provider name).
2. What the status line said and what you pressed, in order ("Recording
   call audio…" → Stop → stuck at "Finalizing transcript…" is a different
   bug than never leaving "Opening the microphone feed…").
3. `%APPDATA%\AICallAssistant\crash.log` (safe to share — see above).
4. For slowness: the three numbers from the latency chip tooltip.
5. For capture problems: your audio setup (default output device, headset
   or speakers, what the call app plays through), whether the level meter
   moved, and whether the silence hint appeared.
6. For the screen-capture warning: your Windows version (`winver` —
   build 2004 is the floor for capture exclusion).
7. Whether you ran the packaged exe or from source, and the app
   version/commit.
