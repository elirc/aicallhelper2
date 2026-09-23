export type ErrorCode =
  | "no_stt_key"
  | "no_llm_key"
  | "stt_connect"
  | "stt_error"
  | "stt_timeout"
  | "no_speech"
  | "llm_auth"
  | "llm_http"
  | "llm_rate_limit"
  | "llm_first_token_timeout"
  | "llm_timeout"
  | "aborted"
  | "internal";

export interface AppErrorPayload {
  code: ErrorCode;
  message: string;
}

export type Result<T> =
  | { ok: true; value: T }
  | { ok: false; error: AppErrorPayload };

export interface Metrics {
  /** Stop acceptance -> captured audio drained (newer cores only). */
  audioDrainMs?: number;
  sttFinalizeMs: number;
  firstTokenMs: number;
  totalMs: number;
}

export type AnswerStyle = "brief" | "balanced" | "detailed";

/** The three styles in UI order, with their visible labels — defined once. */
export const ANSWER_STYLES: ReadonlyArray<{ id: AnswerStyle; label: string }> = [
  { id: "brief", label: "Brief" },
  { id: "balanced", label: "Balanced" },
  { id: "detailed", label: "Detailed" },
];

export type LayoutMode = "full" | "prompter";

export const PROMPTER_FONT_MIN = 14;
export const PROMPTER_FONT_MAX = 28;
export const ANSWER_FONT_MIN = 12;
export const ANSWER_FONT_MAX = 22;
export const FONT_STEP = 2;

export type CallType =
  | "behavioral"
  | "technical"
  | "system_design"
  | "recruiter"
  | "sales"
  | "meeting";

export interface CallTypeChoice {
  id: CallType;
  label: string;
}

/** One opportunity or kind of call: what the answer is grounded in. */
export interface Profile {
  id: string;
  name: string;
  callType: CallType;
  focus: string;
  resume: string;
  jobDescription: string;
  notes: string;
}

/** A profile as sent on Save: a null id asks the core to assign one. */
export type ProfileInput = Omit<Profile, "id"> & { id: string | null };

export interface ProviderChoice {
  id: string;
  displayName: string;
}

export interface SettingsView {
  /** The ACTIVE profile's text, mirrored for convenience. */
  resume: string;
  jobDescription: string;
  callType: CallType;
  focus: string;
  notes: string;
  activeProfileId: string;
  profiles: Profile[];
  callTypes: CallTypeChoice[];
  alwaysOnTop: boolean;
  llmProvider: string;
  answerStyle: AnswerStyle;
  hotkey: string;
  layoutMode: LayoutMode;
  prompterFontPx: number;
  answerFontPx: number;
  providers: ProviderChoice[];
  hotkeyRegistered: boolean;
  /** Why the shortcut is off, so the UI never blames another app for a typo. */
  hotkeyStatus: "registered" | "disabled" | "invalid" | "unavailable";
  /** Bumped by every successful save (newer cores; CONTRACT §10). */
  settingsRevision?: number;
  /** Per saved key: how it is stored. "plaintext" = legacy DPAPI fallback. */
  keyStorage?: Record<string, "encrypted" | "plaintext">;
  /** How the settings file loaded; unreadable/invalid means defaults are shown. */
  settingsFile?: { load: "ok" | "missing" | "unreadable" | "invalid"; backup: string | null };
  /** hasDeepgramKey, hasAnthropicKey, ... generated from the provider registry. */
  [key: string]: unknown;
}

export function hasKey(view: SettingsView, id: string): boolean {
  const field = "has" + id.charAt(0).toUpperCase() + id.slice(1) + "Key";
  return view[field] === true;
}

export interface SettingsPatch {
  resume?: string;
  jobDescription?: string;
  callType?: CallType;
  focus?: string;
  notes?: string;
  activeProfileId?: string;
  profiles?: ProfileInput[];
  alwaysOnTop?: boolean;
  llmProvider?: string;
  answerStyle?: AnswerStyle;
  hotkey?: string;
  layoutMode?: LayoutMode;
  prompterFontPx?: number;
  answerFontPx?: number;
  keys?: Record<string, string>;
  /** The settingsRevision a Settings-panel draft was built on; a mismatch
   *  makes the core reject the save and apply nothing. */
  baseRevision?: number;
}

/** The core's profile-count limit (settings.py MAX_PROFILES). */
export const MAX_PROFILES = 20;
/** The core's profile-name length limit (settings.py MAX_PROFILE_NAME_CHARS). */
export const MAX_PROFILE_NAME_CHARS = 60;

/** Capture-protection verdict. "unknown" until Windows has answered — the UI
 *  must never claim the window is hidden before it knows. */
export type ProtectionVerdict = "protected" | "unprotected" | "unknown";

/** Session phases the core reports in a status snapshot. */
export type CoreSessionPhase =
  | "idle"
  | "starting"
  | "recording"
  | "finalizing"
  | "answering";

/** The core's authoritative readiness snapshot (get_status). `revision` is
 *  monotonic and shared with the protection push events, so an older snapshot
 *  can never overwrite a fresher verdict. `pageGeneration` is optional: when
 *  present, events stamped with an older `pageGen` were meant for a previous
 *  page and are dropped. */
export interface StatusSnapshot {
  revision: number;
  coreReady: boolean;
  protection: ProtectionVerdict;
  session: { id: string | null; phase: CoreSessionPhase | string };
  pageGeneration?: number;
  /** Core build state (newer cores). */
  core?: CoreState;
  coreError?: string | null;
}

export type CoreState = "starting" | "ready" | "failed";

/** Every event name the core emits; the reducer switches on these. */
export type AppEventName =
  | "stt:partial"
  | "llm:delta"
  | "llm:done"
  | "session:error"
  | "session:autostopped"
  | "audio:level"
  | "hotkey:toggle"
  | "protection:ok"
  | "protection:failed";

export interface AppEventDetail {
  name: AppEventName | string;
  payload: { sessionId?: string; [key: string]: unknown };
}
