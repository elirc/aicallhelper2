import { useEffect, useRef, useState } from "react";

import { bridge } from "../bridge";
import type { AnswerStyle, SettingsPatch, SettingsView } from "../types";
import { hasKey } from "../types";

const DEFAULT_HOTKEY_PLACEHOLDER = "Ctrl+Shift+Space";

interface Props {
  settings: SettingsView;
  onSaved: (view: SettingsView) => void;
  onBack: () => void;
}

export function SettingsPanel({ settings, onSaved, onBack }: Props) {
  const [resume, setResume] = useState(settings.resume);
  const [jobDescription, setJobDescription] = useState(settings.jobDescription);
  const [alwaysOnTop, setAlwaysOnTop] = useState(settings.alwaysOnTop);
  const [provider, setProvider] = useState(settings.llmProvider);
  const [style, setStyle] = useState<AnswerStyle>(settings.answerStyle);
  const [hotkey, setHotkey] = useState(settings.hotkey);
  // Only the key fields the user actually typed into are sent on save.
  const [typedKeys, setTypedKeys] = useState<Record<string, string>>({});
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const headingRef = useRef<HTMLHeadingElement>(null);

  useEffect(() => {
    headingRef.current?.focus();
  }, []);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onBack();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onBack]);

  const keyIds = ["deepgram", ...settings.providers.map((p) => p.id)];

  const keyLabel = (id: string): string => {
    if (id === "deepgram") return "Deepgram API key";
    const choice = settings.providers.find((p) => p.id === id);
    const name = choice ? choice.displayName : id;
    return `${name} API key${id === "groq" ? " (only for the Groq preset)" : ""}`;
  };

  const save = async () => {
    setError(null);
    const patch: SettingsPatch = {
      resume,
      jobDescription,
      alwaysOnTop,
      llmProvider: provider,
      answerStyle: style,
      hotkey,
    };
    if (Object.keys(typedKeys).length > 0) patch.keys = typedKeys;
    const result = await bridge.setSettings(patch);
    if (!result.ok) {
      setError(result.error.message);
      return;
    }
    onSaved(result.value);
    setTypedKeys({});
    setSaved(true);
    window.setTimeout(() => setSaved(false), 1500);
  };

  return (
    <div className="app settings">
      <header className="header">
        <h1 className="title" tabIndex={-1} ref={headingRef}>
          Settings
        </h1>
      </header>

      {keyIds.map((id) => (
        <label key={id} className="field">
          <span>{keyLabel(id)}</span>
          <input
            type="password"
            autoComplete="off"
            value={typedKeys[id] ?? ""}
            placeholder={hasKey(settings, id) ? "saved — type to replace" : ""}
            onChange={(event) =>
              setTypedKeys((prev) => ({ ...prev, [id]: event.target.value }))
            }
          />
        </label>
      ))}

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
          <option value="brief">Brief</option>
          <option value="balanced">Balanced</option>
          <option value="detailed">Detailed</option>
        </select>
      </label>

      <label className="field">
        <span>Global shortcut</span>
        <input
          type="text"
          value={hotkey}
          placeholder={DEFAULT_HOTKEY_PLACEHOLDER}
          onChange={(event) => setHotkey(event.target.value)}
        />
        <span className="field-help">
          Toggles record/stop from any app. Leave empty to disable the shortcut.
        </span>
      </label>

      <label className="field">
        <span>Resume</span>
        <textarea
          rows={6}
          value={resume}
          onChange={(event) => setResume(event.target.value)}
        />
      </label>

      <label className="field">
        <span>Job description</span>
        <textarea
          rows={4}
          value={jobDescription}
          onChange={(event) => setJobDescription(event.target.value)}
        />
      </label>

      <label className="field field-check">
        <input
          type="checkbox"
          checked={alwaysOnTop}
          onChange={(event) => setAlwaysOnTop(event.target.checked)}
        />
        <span>Keep window on top</span>
      </label>

      <p className="settings-note">
        Keys are stored encrypted and never shown again. This window is hidden
        from screen sharing.
      </p>

      {error && (
        <div className="error-box" role="alert">
          {error}
        </div>
      )}

      <div className="settings-actions">
        <button type="button" className="primary-button" onClick={() => void save()}>
          Save
        </button>
        <button type="button" className="mini-button" onClick={onBack}>
          Back
        </button>
        {saved && <span className="saved-note">Saved ✓</span>}
      </div>
    </div>
  );
}
