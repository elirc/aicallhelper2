import { useEffect, useRef, useState } from "react";

import { bridge } from "../bridge";
import { settingsFileNotice, type Phase } from "../state";
import { ProtectionNotice } from "./ProtectionNotice";
import type {
  AnswerStyle,
  CallType,
  Profile,
  ProfileInput,
  ProtectionVerdict,
  SettingsPatch,
  SettingsView,
} from "../types";
import { ANSWER_STYLES, MAX_PROFILES, MAX_PROFILE_NAME_CHARS, hasKey } from "../types";

const DEFAULT_HOTKEY_PLACEHOLDER = "Ctrl+Shift+Space";

/** Where to get each key; opened in the system browser, never in the webview. */
const KEY_CONSOLES: Record<string, string> = {
  deepgram: "https://console.deepgram.com",
  anthropic: "https://platform.claude.com",
  groq: "https://console.groq.com",
};

/** Sales and meeting profiles ground the answer in "background", not a CV. */
const CONTEXT_CALL_TYPES: ReadonlySet<CallType> = new Set(["sales", "meeting"]);

interface Props {
  settings: SettingsView;
  onSaved: (view: SettingsView) => void;
  onBack: () => void;
  /** The capture-protection verdict: Settings must show it too (R01). */
  protection: ProtectionVerdict;
  /** Capture keeps running behind Settings, so Stop stays reachable here. */
  phase: Phase;
  status: string;
  onStop: () => void;
  /** Bumped each time the shell cancelled a native close for this draft. */
  closeRequests?: number;
  /** Terminal core-failure text, or null. */
  coreFailed?: string | null;
}

/** The core rejected a save built on an older settingsRevision (CONTRACT §10). */
function isStaleSave(message: string): boolean {
  return /newer save/i.test(message);
}

/** Matches the core's PROFILE_ID_RE so a new profile can be activated in
 *  the same patch that creates it. */
function newLocalId(): string {
  return "p" + Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
}

function cloneProfiles(profiles: Profile[]): Profile[] {
  return profiles.map((p) => ({ ...p }));
}

export function SettingsPanel({
  settings,
  onSaved,
  onBack,
  protection,
  phase,
  status,
  onStop,
  closeRequests = 0,
  coreFailed = null,
}: Props) {
  const [profiles, setProfiles] = useState<Profile[]>(() => cloneProfiles(settings.profiles));
  const [editingId, setEditingId] = useState(settings.activeProfileId);
  const [alwaysOnTop, setAlwaysOnTop] = useState(settings.alwaysOnTop);
  const [provider, setProvider] = useState(settings.llmProvider);
  const [style, setStyle] = useState<AnswerStyle>(settings.answerStyle);
  const [hotkey, setHotkey] = useState(settings.hotkey);
  // Only the key fields the user actually typed into are sent on save — and
  // only with real text: a field typed into and then emptied again must NOT
  // erase the stored key (the core treats "" as "clear").
  const [typedKeys, setTypedKeys] = useState<Record<string, string>>({});
  const [removeKeys, setRemoveKeys] = useState<Set<string>>(() => new Set());
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirmDiscard, setConfirmDiscard] = useState(false);
  // Saves are serialized: while one is in flight the form is read-only, so
  // an edit can neither be overwritten by the response nor race a second
  // save to the core in the wrong order (R09).
  const [saving, setSaving] = useState(false);
  const savingRef = useRef(false);
  const savedTimerRef = useRef<number | undefined>(undefined);
  const saveButtonRef = useRef<HTMLButtonElement>(null);
  const headingRef = useRef<HTMLHeadingElement>(null);
  // The last-saved snapshot, for the unsaved-changes guard.
  const savedRef = useRef(snapshot(settings));
  // The settingsRevision this draft is built on; sent with every save so the
  // core can refuse to apply a draft over a newer save (absent on old cores).
  const baseRevisionRef = useRef<number | undefined>(settings.settingsRevision);
  const confirmRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    headingRef.current?.focus();
    return () => window.clearTimeout(savedTimerRef.current);
  }, []);

  const editing = profiles.find((p) => p.id === editingId) ?? profiles[0];
  const editingIndex = profiles.findIndex((p) => p.id === (editing?.id ?? ""));

  const currentSnapshot = JSON.stringify({
    profiles,
    editingId,
    alwaysOnTop,
    provider,
    style,
    hotkey,
    typedKeys: Object.entries(typedKeys).filter(([, v]) => v.trim() !== ""),
    removeKeys: [...removeKeys].sort(),
  });
  const dirty = currentSnapshot !== savedRef.current;

  // Tell the shell whether a native close would lose work (R09); always
  // released when the panel goes away.
  useEffect(() => {
    void bridge.setCloseGuard(dirty);
  }, [dirty]);
  useEffect(() => () => void bridge.setCloseGuard(false), []);

  // The shell cancelled a close because of this draft: ask now.
  useEffect(() => {
    if (closeRequests === 0) return;
    if (dirty) {
      setConfirmDiscard(true);
      window.setTimeout(() => confirmRef.current?.querySelector("button")?.focus(), 0);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [closeRequests]);

  // Only the explicit Discard button throws a dirty draft away. Back and
  // Escape, however often pressed, just (re)show the choice (R09).
  const back = () => {
    if (savingRef.current) return;
    if (dirty) {
      setConfirmDiscard(true);
      return;
    }
    onBack();
  };

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") back();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dirty, confirmDiscard, onBack]);

  const keyIds = ["deepgram", ...settings.providers.map((p) => p.id)];

  const keyLabel = (id: string): string => {
    if (id === "deepgram") return "Deepgram API key";
    const choice = settings.providers.find((p) => p.id === id);
    const name = choice ? choice.displayName : id;
    return `${name} API key${id === "groq" ? " (only for the Groq preset)" : ""}`;
  };

  const updateEditing = (patch: Partial<Profile>) => {
    if (!editing) return;
    setProfiles((prev) => prev.map((p) => (p.id === editing.id ? { ...p, ...patch } : p)));
  };

  const atProfileLimit = profiles.length >= MAX_PROFILES;

  const addProfile = () => {
    if (atProfileLimit) return;
    const id = newLocalId();
    setProfiles((prev) => [
      ...prev,
      {
        id,
        name: `Profile ${prev.length + 1}`,
        callType: "behavioral",
        focus: "",
        resume: "",
        jobDescription: "",
        notes: "",
      },
    ]);
    setEditingId(id);
  };

  const duplicateProfile = () => {
    if (!editing || atProfileLimit) return;
    const id = newLocalId();
    // The suffix must not push the name past the core's limit (the core
    // rejects the whole save, blocking every other edit in it).
    const suffix = " (copy)";
    const base = editing.name.slice(0, MAX_PROFILE_NAME_CHARS - suffix.length);
    setProfiles((prev) => [...prev, { ...editing, id, name: `${base}${suffix}` }]);
    setEditingId(id);
  };

  const deleteProfile = () => {
    if (!editing || profiles.length <= 1) return;
    const remaining = profiles.filter((p) => p.id !== editing.id);
    setProfiles(remaining);
    setEditingId(remaining[Math.max(0, editingIndex - 1)]?.id ?? remaining[0]!.id);
  };

  /** Returns true when the save landed. */
  const save = async (): Promise<boolean> => {
    if (savingRef.current) return false;
    savingRef.current = true;
    setSaving(true);
    setError(null);
    setConfirmDiscard(false);
    const patch: SettingsPatch = {
      profiles: profiles.map((p): ProfileInput => ({ ...p })),
      activeProfileId: editing?.id ?? settings.activeProfileId,
      alwaysOnTop,
      llmProvider: provider,
      answerStyle: style,
      hotkey,
    };
    if (typeof baseRevisionRef.current === "number") patch.baseRevision = baseRevisionRef.current;
    const keys: Record<string, string> = {};
    for (const [id, value] of Object.entries(typedKeys)) {
      if (value.trim() !== "") keys[id] = value;
    }
    for (const id of removeKeys) keys[id] = "";
    if (Object.keys(keys).length > 0) patch.keys = keys;
    const result = await bridge.setSettings(patch);
    savingRef.current = false;
    setSaving(false);
    // The disabled form dropped focus to <body>; put it back on Save.
    window.setTimeout(() => {
      if (document.activeElement === document.body) saveButtonRef.current?.focus();
    }, 0);
    if (!result.ok) {
      if (isStaleSave(result.error.message)) {
        // Nothing was applied. Refresh what is saved (and the base) but keep
        // every edit in this draft; the user decides whether to save it over.
        const fresh = await bridge.getSettings();
        if (fresh.ok) {
          onSaved(fresh.value);
          baseRevisionRef.current = fresh.value.settingsRevision;
        }
        setError(
          "Settings were changed by a newer save, so nothing was saved. Your edits are " +
            "still here — review them and press Save again to keep them.",
        );
        return false;
      }
      setError(result.error.message); // the draft is untouched: nothing lost
      return false;
    }
    baseRevisionRef.current = result.value.settingsRevision;
    onSaved(result.value);
    // Adopt the core's canonical ids (a new profile's id is assigned there).
    setProfiles(cloneProfiles(result.value.profiles));
    setEditingId(result.value.activeProfileId);
    setTypedKeys({});
    setRemoveKeys(new Set());
    savedRef.current = snapshot(result.value);
    setSaved(true);
    window.clearTimeout(savedTimerRef.current);
    savedTimerRef.current = window.setTimeout(() => setSaved(false), 1500);
    return true;
  };

  const saveAndLeave = async () => {
    if (await save()) onBack();
  };

  const fileNotice = settingsFileNotice(settings);

  const isContextCall = editing ? CONTEXT_CALL_TYPES.has(editing.callType) : false;

  return (
    <div className="app settings">
      <header className="header">
        <h1 className="title" tabIndex={-1} ref={headingRef}>
          Settings
        </h1>
        <span className="header-chip">v{__APP_VERSION__}</span>
      </header>

      <ProtectionNotice verdict={protection} />

      {coreFailed && (
        <p className="protection-warning" role="alert">
          {coreFailed}
        </p>
      )}

      {fileNotice && (
        <p className="protection-warning" role="status">
          {fileNotice}
        </p>
      )}

      {phase !== "idle" && (
        // Capture keeps running while Settings is open: never hide Stop.
        <div className="session-bar">
          <span role="status">{status}</span>
          {phase === "recording" && (
            <button type="button" className="mini-button" onClick={onStop}>
              Stop &amp; Answer
            </button>
          )}
        </div>
      )}

      <fieldset className="settings-form" disabled={saving}>
      <section className="settings-section" aria-label="API keys">
        <h2>API keys</h2>
        {keyIds.map((id) => (
          <label key={id} className="field">
            <span>{keyLabel(id)}</span>
            <div className="field-row">
              <input
                type="password"
                autoComplete="off"
                value={typedKeys[id] ?? ""}
                placeholder={
                  removeKeys.has(id)
                    ? "will be removed on Save"
                    : hasKey(settings, id)
                      ? "saved — type to replace"
                      : ""
                }
                onChange={(event) => {
                  const value = event.target.value;
                  setTypedKeys((prev) => ({ ...prev, [id]: value }));
                  // A key typed after Remove is the user's newer intent: it
                  // cancels the queued removal instead of being erased by it.
                  if (value.trim() !== "") {
                    setRemoveKeys((prev) => {
                      if (!prev.has(id)) return prev;
                      const next = new Set(prev);
                      next.delete(id);
                      return next;
                    });
                  }
                }}
              />
              {hasKey(settings, id) && !removeKeys.has(id) && (
                <button
                  type="button"
                  className="mini-button"
                  aria-label={`Remove ${keyLabel(id)}`}
                  onClick={() => {
                    setRemoveKeys((prev) => new Set(prev).add(id));
                    // Removal wins over a half-typed replacement it replaces.
                    setTypedKeys((prev) => ({ ...prev, [id]: "" }));
                  }}
                >
                  Remove
                </button>
              )}
              {removeKeys.has(id) && (
                <button
                  type="button"
                  className="mini-button"
                  aria-label={`Keep ${keyLabel(id)}`}
                  onClick={() =>
                    setRemoveKeys((prev) => {
                      const next = new Set(prev);
                      next.delete(id);
                      return next;
                    })
                  }
                >
                  Undo remove
                </button>
              )}
            </div>
            {settings.keyStorage?.[id] === "plaintext" && !removeKeys.has(id) && (
              <span className="field-help field-warn">
                This saved key is stored WITHOUT encryption (Windows could not encrypt it
                earlier). Saving settings tries to encrypt it again.
              </span>
            )}
            {KEY_CONSOLES[id] && (
              <span className="field-help">
                <button
                  type="button"
                  className="link-button"
                  onClick={() => void bridge.openExternal(KEY_CONSOLES[id]!)}
                >
                  Get a key at {KEY_CONSOLES[id]!.replace("https://", "")}
                </button>
              </span>
            )}
          </label>
        ))}
      </section>

      <section className="settings-section" aria-label="Profile">
        <h2>Profile</h2>
        <span className="field-help">
          One profile per opportunity or kind of call. The profile you are editing
          becomes the active one when you Save; switch between them from the main
          window.
        </span>
        <div className="profile-toolbar">
          <select
            aria-label="Edit profile"
            className="compact-select"
            value={editing?.id ?? ""}
            onChange={(event) => setEditingId(event.target.value)}
          >
            {profiles.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </select>
          <button
            type="button"
            className="mini-button"
            onClick={addProfile}
            disabled={atProfileLimit}
            title={atProfileLimit ? `At most ${MAX_PROFILES} profiles` : undefined}
          >
            Add profile
          </button>
          <button
            type="button"
            className="mini-button"
            onClick={duplicateProfile}
            disabled={atProfileLimit}
            title={atProfileLimit ? `At most ${MAX_PROFILES} profiles` : undefined}
          >
            Duplicate
          </button>
          <button
            type="button"
            className="mini-button"
            onClick={deleteProfile}
            disabled={profiles.length <= 1}
          >
            Delete
          </button>
        </div>

        {editing && (
          <>
            <label className="field">
              <span>Profile name</span>
              <input
                type="text"
                maxLength={60}
                value={editing.name}
                onChange={(event) => updateEditing({ name: event.target.value })}
              />
            </label>

            <div className="field">
              <label className="field-label">
                <span>Call type</span>
                <select
                  value={editing.callType}
                  onChange={(event) =>
                    updateEditing({ callType: event.target.value as CallType })
                  }
                >
                  {settings.callTypes.map((choice) => (
                    <option key={choice.id} value={choice.id}>
                      {choice.label}
                    </option>
                  ))}
                </select>
              </label>
              <span className="field-help">
                Changes how answers are shaped: STAR stories for a behavioral
                interview, facts first for a technical screen, no invented pricing
                on a sales call.
              </span>
            </div>

            <div className="field">
              <label className="field-label">
                <span>Focus</span>
                <input
                  type="text"
                  value={editing.focus}
                  placeholder="e.g. React 19, TypeScript, Node — lead with the frontend work"
                  onChange={(event) => updateEditing({ focus: event.target.value })}
                />
              </label>
              <span className="field-help">
                The stack or topics to lead with when your background covers several.
              </span>
            </div>

            <label className="field">
              <span>{isContextCall ? "Background / resume" : "Resume"}</span>
              <textarea
                rows={6}
                value={editing.resume}
                onChange={(event) => updateEditing({ resume: event.target.value })}
              />
            </label>

            <label className="field">
              <span>{isContextCall ? "Call context" : "Job description"}</span>
              <textarea
                rows={4}
                value={editing.jobDescription}
                onChange={(event) => updateEditing({ jobDescription: event.target.value })}
              />
            </label>

            <label className="field">
              <span>Notes</span>
              <textarea
                rows={3}
                value={editing.notes}
                placeholder="Talking points, numbers, stories, pricing — anything to keep at hand"
                onChange={(event) => updateEditing({ notes: event.target.value })}
              />
            </label>
          </>
        )}
      </section>

      <section className="settings-section" aria-label="App">
        <h2>App</h2>
        <label className="field">
          <span>Answer provider</span>
          <select value={provider} onChange={(event) => setProvider(event.target.value)}>
            {settings.providers.map((choice) => (
              <option key={choice.id} value={choice.id}>
                {choice.displayName}
              </option>
            ))}
          </select>
        </label>

        <label className="field">
          <span>Answer style</span>
          <select
            value={style}
            onChange={(event) => setStyle(event.target.value as AnswerStyle)}
          >
            {ANSWER_STYLES.map((choice) => (
              <option key={choice.id} value={choice.id}>
                {choice.label}
              </option>
            ))}
          </select>
        </label>

        <div className="field">
          <label className="field-label">
            <span>Global shortcut</span>
            <input
              type="text"
              value={hotkey}
              placeholder={DEFAULT_HOTKEY_PLACEHOLDER}
              onChange={(event) => setHotkey(event.target.value)}
            />
          </label>
          <span className="field-help">
            Toggles record/stop from any app. Leave empty to disable the shortcut.
          </span>
        </div>

        <label className="field field-check">
          <input
            type="checkbox"
            checked={alwaysOnTop}
            onChange={(event) => setAlwaysOnTop(event.target.checked)}
          />
          <span>Keep window on top</span>
        </label>
      </section>
      </fieldset>

      <p className="settings-note">
        Keys are encrypted with Windows (DPAPI) and never shown again; if Windows
        cannot encrypt a key, it is not saved. Profile text is stored as ordinary
        JSON. Settings and the crash log live in %APPDATA%\AICallAssistant.
      </p>

      {error && (
        <div className="error-box" role="alert">
          <span>{error}</span>
        </div>
      )}

      {confirmDiscard && (
        <div className="unsaved-bar" role="status" ref={confirmRef}>
          <span>You have unsaved changes.</span>
          <button
            type="button"
            className="mini-button"
            onClick={() => void saveAndLeave()}
            disabled={saving}
          >
            Save and go back
          </button>
          <button type="button" className="mini-button" onClick={onBack} disabled={saving}>
            Discard
          </button>
          <button
            type="button"
            className="mini-button"
            onClick={() => setConfirmDiscard(false)}
          >
            Keep editing
          </button>
        </div>
      )}

      <div className="settings-actions">
        <button
          ref={saveButtonRef}
          type="button"
          className="primary-button"
          onClick={() => void save()}
          disabled={saving}
        >
          {saving ? "Saving…" : "Save"}
        </button>
        <button type="button" className="mini-button" onClick={back} disabled={saving}>
          Back
        </button>
        {saving && (
          <span className="field-help" role="status">
            Saving…
          </span>
        )}
        {saved && <span className="saved-note">Saved ✓</span>}
        {dirty && !saved && !saving && <span className="field-help">Unsaved changes</span>}
      </div>
    </div>
  );
}

function snapshot(view: SettingsView): string {
  return JSON.stringify({
    profiles: cloneProfiles(view.profiles),
    editingId: view.activeProfileId,
    alwaysOnTop: view.alwaysOnTop,
    provider: view.llmProvider,
    style: view.answerStyle,
    hotkey: view.hotkey,
    typedKeys: [],
    removeKeys: [],
  });
}
