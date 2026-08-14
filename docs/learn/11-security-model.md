# 11 — Threat model and the security decisions

This is a personal-use desktop app, not a hardened server. "Security"
here means a small set of specific, tested properties — not a compliance
posture. This document says what the app protects, against what, by which
mechanism, and — just as plainly — what it does not protect. Every claim
below is backed by code or a test you can point at.

## What is worth protecting

- **API keys** — Deepgram, Anthropic, Groq. Stored in
  `%APPDATA%\AICallAssistant\settings.json` (`app_data_dir()`,
  `app.py:53`), DPAPI-encrypted (`app_core/store/secrets.py`).
- **The resume and job description** — up to 200,000 characters each,
  stored verbatim in the same file. For the user this is the highest-value
  data in the app: it is slow to rewrite and personal.
- **Live transcripts and answers** — what the interviewer just asked and
  what the model suggested. These exist only in memory: session state on
  the core loop, history in React state. Nothing writes them to disk —
  there is no localStorage use in `frontend/src`, and `crash.log` records
  exception tracebacks only (`install_crash_logging`, `app.py:66`).
- **The meta-fact that the app is running at all.** The user is on a call
  and may share their screen at any moment. The window's invisibility to
  capture is itself an asset — see content protection below.

Three untrusted inputs cross into the app: the model's output, the
settings file, and the wire streams. One boundary runs the other way: key
material must never cross from the core to the page.

## Untrusted input #1 — model output rendered into a webview

The answer is arbitrary text from a remote model, rendered as markdown
inside WebView2 — the classic XSS shape. A script that ran here would sit
in a page that holds the transcript, the answer, and the resume in JS
state, and that can call every `js_api` command (including
`set_settings`, which could overwrite the resume). It could not read API
keys — see the write-only boundary below — but it would be a real
compromise.

The renderer (`frontend/src/markdown/`) eliminates the class instead of
filtering instances:

- **Parse to data, not HTML strings.** `parseBlocks` and `parseInline`
  produce typed trees (`Block`, `InlineNode`). No stage of the pipeline
  ever builds an HTML string from model text.
- **Every string reaches the DOM as a React text node** (JSX text
  rendering, `Markdown.tsx:20`), which React escapes unconditionally.
  There is no `dangerouslySetInnerHTML` anywhere in `frontend/src` — grep
  finds it only in the comment that forbids it.
- **No attribute is derived from model text.** The single content-bearing
  attribute in the entire output is `<ol start>`, a number produced by
  `parseInt` on matched digits (`blocks.ts:139`).
- **Links are deliberately not parsed.** There is no `<a>`, so no `href`
  exists to sanitize and `javascript:` URLs stay literal, visible text.
  The renderer gives up a markdown feature to delete an attack surface.
- **Headings demote** to `h3`–`h6`; model output cannot out-rank the
  page's own hierarchy.

The distinction to internalize is **class-elimination versus filtering**.
A sanitizer enumerates known-bad input (`<script>`, `onerror=`,
`javascript:` …) and fails open on the variant its author did not think
of. Here the dangerous sink does not exist: there is no path from model
text to markup, attributes, or URLs, so a novel payload has nothing to
reach. The adversarial suite in `markdown-render.test.tsx` proves it —
eight payloads (`<script>`, `<img onerror>`, fence-escape
`</pre><script>`, attribute-injection quotes, `<svg onload>`, `<iframe>`)
each assert zero live elements and no attribute beyond `<ol start>`. A
companion test asserts the payload stays *visible*: sanitizing by deletion
would hide what the model actually said, and showing it as text is both
safer and more honest.

Denial of rendering is treated as part of the same threat. React unmounts
the entire root on an uncaught render error, so one pathological answer
could blank the app mid-call. Emphasis nesting is capped at 24 deep and
1,000 resolved pairs, and emphasis resolution is skipped entirely past
20,000 characters (`inline.ts:29-31`) — unbounded nesting overflowed the
render stack and delimiter-dense text was quadratic (18 KB froze the main
thread for ~59 s, per `markdown-hardening.test.tsx`). If rendering throws
anyway, `MarkdownBoundary` falls back to the raw source as a `<pre>` —
still text-only.

One more hop matters: model and transcript text travels core → page
through `evaluate_js` as source code. `events.py` double-encodes the
batch (`json.dumps` of a `json.dumps`, outer dump ASCII-escaped) so
quotes, backslashes, and U+2028 in payload text arrive as a JS string
instead of becoming JS syntax. Interpolating payloads into that string
naively would be an injection channel on the way *in* to the page.

## Untrusted input #2 — the settings file

`settings.json` is user-writable, survives upgrades, gets hand-edited,
and gets copied between machines. `SettingsStore._load`
(`app_core/store/settings.py:63`) treats it accordingly:

- **Per-field fallback.** Every field is individually type-checked,
  whitelist-checked (provider against the registry, style against the
  three known values), and size-capped; a field that fails falls back to
  its own default while every other field loads. The rule exists because
  whole-file fallback silently destroys the resume and keys over one bad
  byte — `test_per_field_fallback_one_corrupt_value_costs_nothing_else`
  corrupts three fields and asserts the resume, style, and hotkey survive.
  A fully unparseable file loads as first-run defaults, never a crash.
- **Atomic writes.** Saves go through a per-writer tmp file and
  `os.replace` (`_save`, `settings.py:106`), so a crash or full disk
  mid-write cannot truncate the file into "defaults". The in-memory cache
  updates only after the write lands; a failed write leaves memory
  matching disk, so the next successful save cannot silently commit the
  failed patch.
- The `secrets` dict is filtered to string keys and string values on
  load; anything stranger is dropped there and fails closed at decode
  time. `windowBounds` is sanitized at restore time (`bounds.py`).

## Untrusted input #3 — the wire

Deepgram and the LLM providers are trusted with the *content* (next
section) but their streams are still parsed as hostile bytes, because a
parser crash mid-recording destroys the asset in flight — the question
that was just asked.

- **Deepgram frames** (`app_core/stt/frames.py`): every level of the
  payload is isinstance-checked; `is_final` must be literally `True` —
  `is True` rejects the truthy imposters `1` and `"true"`, which would
  otherwise commit interim text into the transcript. Malformed JSON,
  pathologically nested JSON (`RecursionError` inside `json.loads`),
  non-dict payloads, and wrong-shaped Results frames all return `None` —
  ignored, never a crash. Incoming WebSocket frames are capped at 4 MiB
  (`max_size=2**22`, `client.py:110`).
- **Provider SSE** (`app_core/llm/sse.py`): chunks may split at any byte
  boundary — mid-line, between `\r` and `\n`, mid multi-byte UTF-8
  character — so the parser is incremental from raw bytes, handles all
  three line endings, strips a leading BOM, and flushes an unterminated
  final `data:` line. `test_stream_survives_hostile_chunking` replays
  documents at chunk sizes 1/2/3/7.
- **Delta extractors** (`extract_anthropic_delta`,
  `extract_openai_delta`): isinstance checks at every level; a hostile or
  novel event yields `None` and the stream continues.

## Secrets: DPAPI, prefixes, and a write-only boundary

`app_core/store/secrets.py` is the whole story, and it is short:

- Stored values are `enc:<base64 DPAPI blob>` — current-user scope, so
  *another* user on the same machine cannot decrypt them — or, when the
  keystore is unavailable, `plain:<base64>`: a **marked** fallback,
  honestly labeled, still functional. Base64 is encoding, not
  protection; the prefix says so.
- **Decoding goes by the stored prefix**, not by current keystore
  availability. A `plain:` value saved before DPAPI came back still
  decodes; deciding by availability instead would break it.
- **Fail closed.** A blob from another machine, invalid base64, an
  unknown prefix, a non-string — all read as `None`, "unset". The raw
  stored string is never handed to a provider, and the UI nudges for a
  new key instead of pretending one exists.
- **Write-only across the UI boundary.** `SettingsStore.view()` exposes
  `hasDeepgramKey` / `hasAnthropicKey` / `hasGroqKey` booleans and never
  key material; `test_view_exposes_booleans_never_key_material`
  serializes the whole view to prove it. The frontend can set a key,
  clear it (empty string), or leave it untouched (omit the field) — it
  can never read one back. This is what limits the blast radius of a
  renderer compromise to "annoying" instead of "credential theft".
- **Keys must be plain ASCII**, enforced at save time
  (`settings.py:193`). A smart quote from copy-paste used to reach httpx
  and raise `UnicodeEncodeError` mid-answer — surfacing as a useless
  "internal error" — because HTTP headers cannot carry it. Refusing at
  the door with a message naming the real problem is both a UX and an
  integrity fix.
- Keys travel exactly twice: as the WebSocket subprotocol to Deepgram
  over `wss`, and in HTTPS headers to the chosen provider. The origin
  pre-warm is an unauthenticated `GET /v1/models` — no key on that path
  (`warm.py:59`).

## Content protection is a security feature, not a UI nicety

The window is excluded from screen capture with
`WDA_EXCLUDEFROMCAPTURE`. The security-relevant decisions
(`app_core/bridge/protection.py`, `app.py:280`):

- **Verified by read-back, never trusted.** `SetWindowDisplayAffinity`
  returns a BOOL that is trivially ignored, and it can report success
  without the setting sticking. Success means `GetWindowDisplayAffinity`
  reads back exactly `WDA_EXCLUDEFROMCAPTURE`; `WDA_MONITOR` — which
  hides from some capture paths but not screen sharing — counts as
  failure (`test_partial_protection_is_not_protection`).
- **Failure is shown to the user, not logged.** After five attempts with
  growing back-off, an unconfirmed exclusion emits `protection:failed`
  and the page shows a standing `role="alert"` warning
  (`App.tsx:641`). The reasoning: a user who believes they are hidden
  while being broadcast is this product's worst outcome, and no user
  reads a log file mid-call.
- Mutation testing found this path had **zero coverage** — no test
  referenced `_apply_content_protection` or either protection event.
  `tests/test_app_wiring.py` now drives that method and pins the events it
  emits; `tests/test_protection.py` pins the read-back, the fail-closed
  behavior, and the Win32 constants themselves.

## The CSP

The page ships this meta tag (`frontend/index.html`, carried into the
built `frontend/dist/index.html`):

    default-src 'self'; style-src 'self' 'unsafe-inline';
    script-src 'self'; img-src 'self' data:; connect-src 'self'

What it buys: a second, independent fence behind the renderer. If a
markup injection ever appeared despite the text-node design, `script-src
'self'` blocks inline script and `connect-src 'self'` blocks an
exfiltration fetch from page context. Defense in depth is the honest
framing — the first fence is the renderer.

What it does not buy: `style-src` includes `'unsafe-inline'` because
pywebview/WebView2's bootstrap requires it, so style injection is not
CSP-blocked (model text still cannot produce a style attribute — no
attribute comes from model text at all). The CSP constrains the page,
not the Python side: `js_api` is exposed to page script regardless.
Relatedly, the only navigation escape hatch, `open_external`
(`api.py:89`), accepts `https://` URLs only and opens them in the system
browser — the webview itself never navigates away from its page.

## What is NOT defended

Say these out loud rather than discovering them:

- **Anything running as your Windows user.** DPAPI's current-user scope
  means same-user code decrypts the keys exactly as the app does, can
  read `settings.json` (resume included), can edit `frontend/dist` on
  disk, and can unset the display affinity. This app does not and cannot
  defend against local malware; that is the OS account boundary's job.
- **The `plain:` fallback is not encryption.** No keystore means marked
  base64. The app stays functional and the prefix is honest — that is
  the whole guarantee.
- **The providers themselves.** Call audio goes to Deepgram; the resume,
  job description, and transcript go to Anthropic or Groq over TLS.
  Their retention and handling are governed by their terms, not this
  code. TLS uses the OS trust store; there is no certificate pinning.
- **Prompt injection is out of scope.** The resume is the user's own
  text pasted into their own prompt verbatim (`prompt.py:58`), and the
  transcript is whatever the other person said — either can try to
  steer the model. The defended property is narrower: whatever the
  model answers renders as inert text. Nothing it says can execute.
- **Capture bypasses below or outside the OS.** `WDA_EXCLUDEFROMCAPTURE`
  does nothing against a phone camera pointed at the screen or an HDMI
  capture device, and the app cannot know a remote-control tool is
  mirroring the desktop by other means.

## If you add code here, do not

- Do not add `dangerouslySetInnerHTML`, an attribute derived from model
  text, or link parsing to the renderer. Add a node kind to the typed
  tree instead, and extend the XSS suite for it.
- Do not return key material from any `js_api` command or event. New
  providers get a `has<Name>Key` boolean from the registry for free.
- Do not read a stored secret except through `decode_secret` — anything
  unreadable must stay "unset", never a raw string sent to a provider.
- Do not let one settings field's validity decide another's fallback,
  and do not write `settings.json` except through `_save`.
- Do not trust `SetWindowDisplayAffinity`'s return value, and do not
  demote a protection failure to a log line.
- Do not widen the CSP (`script-src` especially) for a convenience
  library; the frontend loads no code from an external origin today.
- Do not log transcripts, answers, resume text, or keys — `crash.log`
  carries tracebacks only.
- Do not relax the ASCII key check or the `https://`-only rule in
  `open_external`.
