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
  sttFinalizeMs: number;
  firstTokenMs: number;
  totalMs: number;
}

export type AnswerStyle = "brief" | "balanced" | "detailed";

export interface ProviderChoice {
  id: string;
  displayName: string;
}

export interface SettingsView {
  resume: string;
  jobDescription: string;
  alwaysOnTop: boolean;
  llmProvider: string;
  answerStyle: AnswerStyle;
  hotkey: string;
  providers: ProviderChoice[];
  hotkeyRegistered: boolean;
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
  alwaysOnTop?: boolean;
  llmProvider?: string;
  answerStyle?: AnswerStyle;
  hotkey?: string;
  keys?: Record<string, string>;
}

export interface AppEventDetail {
  name: string;
  payload: { sessionId?: string; [key: string]: unknown };
}
