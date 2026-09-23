# AI Call Assistant — User Guide

AI Call Assistant listens to your computer's sound output during a video
call, shows you what the other person just said, and suggests what to say back — grounded in your own
resume, the job you are interviewing for, or the notes for a sales call.
The suggested answer appears **at the top-centre of your screen, right
under your webcam**, so you can read it while still looking at the camera.

This guide assumes the app is installed and your API keys are saved. If
not, see [SETUP-AND-DEPLOY.md](SETUP-AND-DEPLOY.md) first.

---

## 1. Two-minute tour

```
┌──────────────────────────────────────────────────────────┐
│ ● AI Call Assistant   Claude Haiku 4.5   ⤒  ⊤  ⚙          │  header
│ Ready — press Record while the other person is speaking   │  status
│ ┌ SUGGESTED ANSWER ──────────────── A− A+ Regenerate Copy ┐│
│ │ Your AI-suggested answer will stream here.              ││  ← answer FIRST,
│ └─────────────────────────────────────────────────────────┘│    at the camera line
│ ┌ QUESTION HEARD ────────────────────────────────────────┐ │
│ │ The live transcript will appear here while you record. │ │
│ └────────────────────────────────────────────────────────┘ │
│ [           Record            ]  Ctrl+Shift+Space          │
│ [ Type a question instead…                       ] [Ask]   │
│ [ Profile ▾ ] [ Call type ▾ ]                              │
│ (Brief) (Balanced) (Detailed)                              │
└──────────────────────────────────────────────────────────┘
```

1. **Record** while the other person is asking their question. The live
   transcript appears in *Question heard* as they speak.
2. **Stop & Answer** the moment they finish. The answer starts streaming,
   typically within a second or two (the target is about one second); the
   green chip shows how long the app measured from Stop to the first word.
3. Say it. **Copy** puts the answer on the clipboard; **Regenerate** asks
   again (useful after changing the style or call type).

The window stays on top of everything and asks Windows to **exclude it from
screen capture**. The small indicator in every view tells you the result:
*Hidden from screen capture* (Windows confirmed it), *Screen-share protection
not confirmed yet*, or a red alert that Windows refused. Exclusion works for
ordinary screen and window sharing in the apps we test (see the release
checklist), but it is what Windows reports, not a guarantee for every capture
method — do one test share in the call app you use before an important call.

**What the app hears:** everything playing on your default output device —
the other person, but also notification sounds, music or a video in another
tab. It never opens your microphone, so your own voice is not captured
unless your call app plays it back. Mute other audio while you record.

---

## 2. Reading at eye level: prompter mode

During a call, click **⤒ Enter prompter mode** (top-right). The window
turns into a short, wide strip and docks itself at the top-centre of your
display — directly under a laptop or monitor-top webcam:

```
┌──────────────────────────────────────────────────────────────────────────┐
│ ● [Stop & Answer] 0:42  "Tell me about a time you…"  (Brief)(Balanced)(Detailed)  ‹ 2/3 ›  A− A+  ⤒  ⤢ │
│                                                                          │
│      Sure. At Acme I owned the checkout redesign when conversion         │
│      dropped after a platform migration. I pulled the funnel data,       │
│      found the drop was on mobile Safari, and …                          │
│                                                                ▼ more    │
└──────────────────────────────────────────────────────────────────────────┘
```

What changes in the strip:

- **Only the answer.** Large text (18 px by default) in a comfortable
  column width, so your eyes stay near the centre where the camera is.
- **The text does not jump.** While an answer streams in, the strip stays
  anchored at the top so you can read the opening while you say it. Scroll
  down (mouse wheel, Page Down or Space) at your own pace; **▼ more** tells
  you there is more below.
- **A−/A+** change the text size (14–28 px) and remember it.
- **⤒ Dock under camera** re-centres the strip at the top of whichever
  display it is on, if you have dragged it away.
- **⤢ Exit prompter** (or **Esc**) returns to the full window where and
  how big it was before (if that display is still connected; otherwise it
  docks on the current one).
- The record button, timer, the one-line question, style chips and history
  arrows are all still there. The global hotkey works too.

The strip's size and position are remembered separately from the full
window's, so you can set each up once.

**Tip for external webcams:** if your camera sits on a second monitor,
drag the strip onto that monitor and press ⤒ — it docks to the top-centre
of the display it is on, not always the primary.

**Full-window alternative:** the full layout also puts the answer first,
right under the header. Press **⊤ Dock under camera** to move the whole
window to the top-centre of the screen; on first run it starts there.

---

## 3. Profiles and call types

Different calls need different answers. A behavioral interview wants a
story from your experience; a technical screen wants the fact answered
correctly first; a sales call must never invent pricing. A **profile**
bundles everything the app needs for one kind of call:

| Field | What to put in it |
| --- | --- |
| **Profile name** | e.g. "Acme — senior backend", "Recruiter calls", "Northwind renewal" |
| **Call type** | One of six (below). Shapes how every answer is written. |
| **Focus** | The stack or topics to lead with when your background covers several: "Go, gRPC, Postgres — lead with the payments work". |
| **Resume** (or **Background** for sales/meetings) | Paste your CV, or the background the model should speak from. Formatting is kept exactly. |
| **Job description** (or **Call context**) | The role, or what this call is about. |
| **Notes** | Talking points, numbers, stories, pricing, objections — anything you want at hand. |

### The six call types

| Call type | How answers change |
| --- | --- |
| **Behavioral interview** (default) | First-person stories grounded in your resume, concrete outcomes when your resume gives them, STAR structure for longer answers. Instructed not to invent experience — always check an answer against what you actually did. |
| **Technical screen** | Answers the technical question directly and correctly first, names the exact APIs and data structures, adds the one tradeoff a senior engineer would mention. Leads with your *Focus* stack. Technical facts do not have to come from your resume, but hands-on experience claims do. Coding questions get the approach and complexity, not a code listing. |
| **System design** | Requirements first, then components and data flow, then the key tradeoff and what changes at 10x scale. Asks one clarifying question when the prompt is ambiguous. |
| **Recruiter screen** | Short, warm, positive. Straight answers on logistics; compensation as a range or deferred, never a single number unless your notes say so. |
| **Sales or customer call** | Works out what they are really asking for and moves the call forward. Instructed never to invent pricing, capabilities or commitments and to offer to confirm instead — the model can still get this wrong, so read before you promise. |
| **General meeting** | The most useful contribution right now: the answer, or the one clarifying question, decision or next step the discussion needs. |

### Switching mid-call

Two dropdowns sit under the Ask box in the full window:

- **Profile** (shown once you have more than one) — switches everything at
  once: resume, job, focus, notes and call type. The header chip shows
  which profile is active.
- **Call type** — changes just the call type of the active profile and
  saves it.

Both apply to the **next** answer. To re-answer the last question under
the new setting, press **Regenerate**. Each history entry is tagged with
the call type it was written for, so you can compare.

### Managing profiles

Open **Settings → Profile**:

- **Edit profile** dropdown chooses which one you are editing.
- **Add profile** creates a blank one; **Duplicate** copies the current
  one (handy for the same resume against a second job). You can keep up to
  **20 profiles**.
- **Delete** removes the current profile (you always keep at least one).
- **Save** stores every profile and makes the one you are looking at the
  active one. **Back** with unsaved edits asks whether to save, discard or
  keep editing.

Your existing resume and job description were moved into a profile called
**Default** automatically the first time this version ran; nothing was
lost.

---

## 4. Answer style

The three chips — **Brief**, **Balanced**, **Detailed** — control length
and shape, and are global (they apply to every profile):

- **Brief**: one or two spoken sentences, no lists.
- **Balanced**: a few sentences, or short structured points for a complex question.
- **Detailed**: a direct sentence, then three to five short points using the
  structure your call type asks for (STAR for behavioral, "requirements →
  components → tradeoff" for system design, and so on).

Changing the style leaves the cacheable profile part of the prompt
unchanged, so it does not invalidate the provider's prompt cache. (Whether
that cache helps at all depends on how long your profile is; a longer style
still means a longer answer.)

---

## 5. Typing instead of recording

If you already know the question, type it in **Type a question instead…**
and press **Ask** (or Enter). It goes through the same pipeline and the
same profile. Regenerate on a recorded question also works this way.

Every answer is written from your active profile and the current question
only. The app does **not** send earlier questions or answers as context,
so a follow-up like "and what about the second one?" is answered without
knowing what "the second one" was — include the context in your question.

---

## 6. The global shortcut

**Ctrl+Shift+Space** toggles Record / Stop from any application — you
never have to click into this window mid-call. Change or disable it in
Settings. If another program already owns that shortcut, the app tells you
so under the Record button instead of silently failing.

---

## 7. While recording

- The green **level meter** moves with the call audio. If it stays flat
  for 5 seconds you will see *"No call audio detected yet"* — the call is
  probably playing through a headset or another output device, not the
  one Windows calls the default. Fix the output device and press Record
  again.
- The **timer** counts up. Recording stops itself at **2:00**, counted from
  when capture actually started; in the last 30 seconds a countdown appears
  so it never surprises you. When the cap
  trips, the answer still comes — the status says *"Reached the 120s
  limit — answering now"*.
- **Stop & Answer** works from the first instant, even while the app is
  still connecting to the transcription service; anything captured so far
  is kept, including the last fraction of a second before you pressed Stop.
- Audio captured before Stop may take a moment to finish uploading to the
  transcription service after you press it.

---

## 8. History

Every answered question is kept (up to six) for the session. Use **←** and
**→** (or the arrows in the prompter strip) to look back; **Clear** wipes
them when idle. History is not saved to disk, is lost if the app's page is
reloaded or the app restarts, and is never sent to the AI as context.

---

## 9. Settings reference

| Setting | Meaning |
| --- | --- |
| **API keys** | Deepgram (transcription), Anthropic (answers), Groq (optional fastest preset). Stored encrypted with Windows DPAPI; if Windows cannot encrypt a key the save fails with an error and your previous key is kept. The field shows "saved — type to replace". **Remove** clears a key on Save. Emptying the box does *not* remove a saved key. "Get a key" opens the provider's console in your browser. |
| **Profile** | See section 3. |
| **Answer provider** | Claude Haiku 4.5 (recommended) or Groq (fastest). Needs that provider's key. |
| **Answer style** | Same as the chips. |
| **Global shortcut** | Any `Ctrl/Alt/Shift/Win + key`. Empty disables it. |
| **Keep window on top** | On by default; turn off if you prefer to alt-tab to it. |

Text size buttons (**A−/A+**) live on the answer panel and the prompter
strip rather than in Settings; both sizes are remembered.

---

## 10. Privacy and what leaves your machine

- **Audio** — everything on your default output device, not just the
  caller — goes to Deepgram while you are recording (Record → Stop; audio
  captured just before Stop may finish uploading right after it).
- **The prompt** — your active profile's resume, job, focus, notes, and the
  transcript — goes to the answer provider you chose, once per answer.
- **A warm-up request** (no key, no content) goes to that provider's public
  models endpoint when you press Record, Ask or Stop, so a connection is
  ready when the answer is needed.
- Nothing else leaves the machine. There is no account, no telemetry, and
  no server of ours. API keys are encrypted with Windows DPAPI for your
  Windows user only; your profiles are stored as ordinary, unencrypted text
  in %APPDATA%\AICallAssistant\settings.json.
- The window asks to be excluded from screen capture on Windows 10 2004 and
  later. If the app shows *"Windows would not hide this window"* or says
  protection is not confirmed yet, assume the window **is** visible in
  shares.

---

## 11. Quick fixes

| You see | Do this |
| --- | --- |
| *First run: open Settings…* | Add the Deepgram key and the key for your chosen provider. |
| *No speech detected in the recording* | Call audio was not reaching the default output device; see section 7. |
| *Deepgram closed the connection (code 1008 …)* | The Deepgram key is wrong or revoked. |
| *Anthropic rejected the API key (401)* | The Anthropic key is wrong; re-paste it in Settings. |
| *The answer didn't start streaming within 10 seconds* | Network or provider hiccup — press Regenerate. |
| *Ctrl+Shift+Space is already taken by another app* | Pick another shortcut in Settings. |
| *The app is still starting up — try again in a moment* | You pressed Record within the first seconds after launch; wait a moment. |
| Window is not where you left it | It moves back on-screen automatically after a monitor is unplugged; press **⊤ Dock under camera** to re-centre it. |
| *Groq no longer offers the model this version of the app uses* | Switch the answer provider to Claude in Settings; update the app when a new version is available. |
| An answer ends with an error such as *The answer stopped before … finished it* | The provider's stream ended early or reported an error; press Regenerate. |

More detail, including exact error messages: `docs/TROUBLESHOOTING.md`.
