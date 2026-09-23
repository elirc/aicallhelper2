# TESTING.md — every test, what it verifies, and why it exists

This document is how future changes get judged. One bullet per test: what it
verifies, and the failure mode it guards against. No test anywhere in either
suite touches the network, a live provider, or an audio device — the core is
exercised through Protocol-typed fakes at the wire level (scripted SSE bytes
via `httpx.MockTransport`, a scripted WebSocket server on loopback), and the
frontend through the real components with a mocked command/event bridge.

Run them:

```
.venv\Scripts\python -m pytest tests -q        # core (519 tests)
cd frontend && npm test                        # frontend (236 tests)
```

---

## Core (pytest)

### tests/test_frames.py — Deepgram frame parsing (hostile input)

- **test_interim_and_final** — the two normal Results shapes parse to
  segments; the base case everything else builds on.
- **test_is_final_must_be_literally_true** — `1`, `"true"`, `[1]`, `1.0`
  parse as *interim*. Why: a truthy imposter would commit interim text into
  the transcript prefix, silently corrupting the question the answer is
  grounded in.
- **test_non_string_transcript_ignored** — non-string transcripts drop the
  frame. Why: `str(None)` in the transcript would ground the answer in
  garbage.
- **test_missing_or_null_channel_ignored** — missing/null/wrong-typed
  `channel` drops the frame instead of raising `TypeError` mid-recording.
- **test_empty_alternatives_ignored** — `alternatives: []` (and non-dict
  entries) drop the frame; indexing `[0]` blindly would crash the reader.
- **test_malformed_json_ignored_never_crash** — garbage frames are ignored;
  one weird frame must not kill a recording.
- **test_pathologically_nested_json_ignored** — 50k-deep nesting hits
  RecursionError inside `json.loads`; we swallow it. Why: a hostile/buggy
  stream must not take down the reader task.
- **test_non_dict_payloads_ignored** — valid JSON that isn't an object
  (`[]`, `3`, `null`) is ignored, not attribute-errored.
- **test_other_frame_types_ignored** — Metadata / UtteranceEnd /
  SpeechStarted are ignored per spec.
- **test_v1_listen_shape / test_newer_code_description_shape** — both error
  frame shapes in the wild ({description,message,variant} and
  {code,description}) surface their detail. Why: the surfaced `stt_error`
  quotes whatever Deepgram said; losing the detail sends the user debugging
  blind.
- **test_error_with_no_detail** — an Error frame with no fields still
  produces "unknown error" detail rather than crashing on a missing key.
- **TestAccumulator.test_committed_prefix_plus_interim** — full transcript =
  committed finals + latest interim, appended incrementally (O(1) per
  message, not a re-join of the recording).
- **test_empty_final_clears_interim_but_commits_nothing** — an empty final
  supersedes stale interim text; committing "" would inject double spaces.
- **test_whitespace_final_ignored** — whitespace-only finals don't pollute
  the committed prefix.
- **test_final_supersedes_interim** — a final replaces the interim that
  preceded it (Deepgram re-sends the text, corrected).

### tests/test_sse.py — SSE parser under hostile chunking

- **test_single_event_lf / _crlf / _cr_only** — all three SSE line endings
  produce the same event.
- **test_multiple_events** — events dispatch on blank lines, in order.
- **test_comment_lines_skipped** — `: keep-alive` lines are dropped (Groq
  sends them).
- **test_non_data_fields_ignored** — `event:`/`id:`/`retry:` fields don't
  become data.
- **test_multi_line_data_joined_with_newline** — multi-`data:` events join
  with `\n` per the SSE spec.
- **test_only_one_leading_space_stripped** — exactly one space after the
  colon is stripped; stripping more would corrupt deltas that begin with
  spaces (which streaming answers routinely do).
- **test_final_unterminated_data_line_flushed** — a stream that dies without
  a trailing newline still yields its last `data:` line. Why: a truncated
  stream otherwise silently loses the answer's last words.
- **test_unterminated_event_without_blank_line_flushed** — same, for a
  terminated line missing its blank-line dispatch.
- **test_empty_stream** — zero bytes parse to zero events, no crash.
- **test_every_cut_point_matches_batch** — THE invariant: for a corpus of 8
  hostile documents (CRLF, comments, [DONE], multi-byte UTF-8, Japanese
  text), splitting the byte stream at EVERY offset yields identical events
  to parsing it whole. Why: real chunk boundaries are arbitrary; any
  boundary-dependent behavior is a latent data-corruption bug.
- **test_three_way_cuts_on_crlf_heavy_doc** — three-fragment splits stress
  carry-over state (line buffer + pending CR) across two boundaries.
- **test_byte_at_a_time** — the pathological minimum chunk size, including a
  € sign split mid-encoding; exercises the incremental UTF-8 decoder.
- **test_split_between_cr_and_lf_no_phantom_blank_line** — the CR|LF split
  must not fabricate an empty line, which would dispatch the event twice.

### tests/test_prompt.py — prompt building (byte-stable product behavior)

- **test_bare_prompt_is_role_only** — no resume/JD → no grounding sentence;
  the grounding line must not appear without profile content to ground in.
- **test_resume_section_appended_with_grounding /
  test_jd_section_appended** — exact section headers, grounding appended
  when either section exists.
- **test_edge_trim_only_interior_formatting_survives** — resume trimmed at
  the edges only; interior formatting belongs to the user.
- **test_user_message_wrapper_verbatim** — the exact wrapper string; this is
  product behavior, not prose.
- **test_three_styles / test_unknown_style_falls_back_to_balanced** — the
  three verbatim style policies; corrupt style values degrade to balanced
  instead of crashing or emitting an empty policy.
- **test_byte_stable_across_calls** — identical inputs → byte-identical
  prompts. Why: Anthropic caching is a byte-prefix match; one drifting byte
  silently zeroes the cache hit rate.
- **test_style_flip_never_touches_cached_prefix** — all three styles share
  one cached prefix. Why: the style policy sits AFTER the cache breakpoint
  so flipping styles is latency-free.
- **test_transcript_lives_outside_the_system_prompt** — transcripts vary per
  question; if they leaked into the prefix, no two calls would ever share a
  cache entry.

### tests/test_bounds.py — window-geometry sanitizer

- **TestCorruption (5 tests)** — non-dict, missing field, wrong-typed field,
  bool-as-number, NaN/inf: each drops the bounds AS A UNIT. Why: restoring
  half-corrupt geometry can place the window at (NaN, NaN) — invisible and
  unrecoverable; `True` is an int subclass in Python and must not pass as a
  coordinate.
- **test_size_clamps_up_to_minimum** — persisted sizes below the window
  minimum clamp up; a 10×10 restore is an unusable window.
- **test_floats_rounded_to_integers** — Win32 wants ints.
- **test_fully_on_screen_keeps_position** — the happy path.
- **test_offscreen_drops_position_keeps_size** — position drops (OS
  centers), size survives.
- **test_39px_visible_is_not_enough_40_is** — the exact 40 px boundary on
  the visibility rule.
- **test_both_axes_must_overlap** — 40 px on one axis alone is not visible;
  a window above the work area with full horizontal overlap is still lost.
- **test_negative_coordinates_valid_on_left_monitor** — displays left of
  primary have negative x; rejecting negatives would recenter valid setups.
- **test_unplugged_monitor_recenters** — same coordinates, monitor gone →
  position dropped. The unplug-a-monitor scenario from the QA script.
- **test_visibility_judged_at_clamped_size** — visibility uses the CLAMPED
  size; judging the stored 10×10 would wrongly drop a recoverable position.
- **test_no_displays_drops_position** — no display info → let the OS place
  the window.

### tests/test_secrets.py — DPAPI secret semantics

- **test_roundtrip_encrypted** — enc: roundtrip through the keystore.
- **test_keystore_unavailable_fails_closed /
  test_failing_keystore_fails_closed** — no keystore (or a throwing one) →
  `SecretEncryptionError`; no new key is ever stored recoverably. Replaced the
  old `plain:` fallback tests on 2026-09-22 (R02; see the production-readiness
  pass below).
- **test_key_material_never_stored_raw** — the raw key never appears in the
  stored string.
- **test_unicode_keys_survive** — UTF-8 roundtrip.
- **test_decodes_by_stored_prefix_not_keystore_availability** — a `plain:`
  value decodes even when a keystore exists now. Why: decoding by current
  availability instead of stored prefix breaks keys saved before DPAPI came
  back.
- **test_enc_value_without_keystore_reads_as_unset** — fail closed.
- **test_undecryptable_reads_as_unset** — a blob from another machine reads
  as unset, never as garbage handed to a provider.
- **test_unknown_prefix_reads_as_unset** — future formats (or raw keys
  pasted into the file) never reach a provider as-is.
- **test_invalid_base64_reads_as_unset / test_non_string_reads_as_unset** —
  corrupt values fail closed.

### tests/test_settings.py — settings store

- **test_missing_file_loads_defaults** — first run.
- **test_unparseable_file_loads_defaults_never_crashes /
  test_non_object_file_loads_defaults** — corrupt file = defaults, not a
  crash at boot.
- **test_per_field_fallback_one_corrupt_value_costs_nothing_else** — THE
  fallback rule: four corrupt fields fall back individually while the
  resume, style, and hotkey survive. Why: whole-file fallback silently
  destroys the user's resume and keys over one bad byte.
- **test_oversize_profile_field_falls_back** — the 200k cap on load.
- **test_empty_hotkey_means_disabled_not_default** — `""` must NOT spring
  back to Ctrl+Shift+Space; the user chose "no shortcut".
- **test_corrupt_secrets_dict_falls_back_alone** — secrets corruption
  doesn't take the resume with it.
- **test_patch_returns_fresh_view** — the UI re-renders from the returned
  view; the core is the source of truth.
- **test_invalid_style_rejected / test_invalid_provider_rejected /
  test_oversize_resume_rejected** — patch validation raises (→ error Result
  envelope), never partially applies.
- **test_resume_stored_verbatim_not_trimmed** — profile formatting belongs
  to the user.
- **test_hotkey_trimmed_on_save** — raw spaces would make shortcut
  registration throw; whitespace-only → "" (disabled).
- **test_unknown_patch_fields_ignored** — forward compatibility.
- **test_view_exposes_booleans_never_key_material** — the frontend NEVER
  receives key material; only has\*Key booleans. Serializing the whole view
  proves it.
- **test_has_key_fields_generated_from_registry** — a new provider gets its
  boolean for free.
- **test_omitted_key_field_leaves_key_untouched** — saving the settings form
  without typing a key must not clear the stored key.
- **test_empty_or_whitespace_key_clears** — the documented way to remove a
  key.
- **test_key_trimmed_before_store** — pasted keys carry whitespace.
- **test_keys_encrypted_on_disk** — the raw key never appears in
  settings.json.
- **test_undecryptable_secret_reads_as_unset_in_view** — hasDeepgramKey is
  false for a foreign blob; the UI must nudge for a new key, not pretend one
  exists.
- **test_write_goes_through_tmp_and_replace** — atomic write; no stray tmp
  file left behind.
- **test_failed_write_leaves_memory_matching_disk** — a failed `os.replace`
  leaves the in-memory cache at the last committed state. Why: memory-ahead-
  of-disk means the next successful save silently commits the failed patch.
- **test_window_bounds_save_never_raises** — geometry saves during shutdown
  must never throw.
- **test_window_bounds_roundtrip** — bounds persist across a reload.

### tests/test_retry.py — the retry policy matrix

- **test_connect_failure_before_any_delta_retries_once** — the ONE retryable
  case: connection-level failure, zero deltas delivered.
- **test_two_connect_failures_do_not_retry_twice** — "exactly once" is a
  hard cap; unbounded retry burns the latency budget.
- **test_http_status_never_retried** — the server heard us and said no; an
  instant identical retry cannot succeed and delays the real error.
- **test_never_after_a_delta** — the UI has painted; a second attempt would
  concatenate two answers. The painted partial stays.
- **test_never_after_abort** — CancelledError propagates as control flow;
  retrying cancelled work resurrects a session the user killed.
- **test_retried_request_is_the_same_object** — byte-identical retry by
  construction: `build_request` ran once; the immutable request is reused.

### tests/test_providers.py — provider conformance + error matrices

TestProviderConformance runs against BOTH shipped providers (parameterized);
it is the suite a future provider clones (README "How to add an answer
provider"):

- **test_identity** — id/display_name/origin present; origin has no trailing
  slash (the warmer appends `/v1/models`).
- **test_build_request_is_deterministic** — two builds are byte-identical
  (prompt caching + honest retry both depend on this).
- **test_request_carries_the_key_and_json_content_type** — auth actually
  reaches the wire.
- **test_stream_yields_expected_deltas** — scripted SSE bytes stream out as
  the expected concatenation.
- **test_stream_survives_hostile_chunking** — chunk sizes 1/2/3/7 over
  multi-byte UTF-8 content; providers parse from bytes, so split characters
  must survive.
- **test_non_200_raises_status_failure** — status + body snippet captured
  for classification.
- **test_200_with_empty_body_raises_not_crashes** — Groq has returned 200
  with nothing; that must be an error Result, not a hang or crash.
- **test_connection_failure_is_connect_kind** — the only retryable kind.
- **test_mid_stream_drop_is_stream_drop_kind** — deltas before the drop
  still made it out (the UI keeps the partial); the drop is not "connect"
  (which would wrongly retry after paint). The body carries no terminal
  event, since Anthropic stops reading at `message_stop`.
- **test_abort_classified_first_never_a_scary_http_error** — CancelledError
  → `aborted` BEFORE any HTTP inspection.
- **test_401_maps_to_llm_auth / test_429_maps_to_rate_limit** — the closed
  code set with the status quoted.
- **test_connect_maps_to_llm_http_with_actionable_message** — "check your
  internet", not a stack trace.
- **test_unknown_exception_maps_to_internal** — no raw exception text
  reaches the UI.
- **test_only_connect_is_retryable** — the full retryable/not matrix.

TestAnthropicSpecifics:

- **test_body_shape_two_system_blocks_cache_breakpoint_on_first** — model
  pinned, max_tokens 1024, TWO system blocks, cache_control on the first
  only. Why: cache_control on the style block would invalidate the cache on
  every style flip.
- **test_headers** — x-api-key + anthropic-version pinned.
- **test_multiple_content_blocks_joined_with_nothing** — deltas across
  content blocks concatenate with NOTHING between; a joiner would corrupt
  the answer relative to what streamed.
- **test_403_is_llm_auth / test_529_overloaded /
  test_other_status_includes_snippet** — the Anthropic status matrix.

TestGroqSpecifics:

- **test_body_shape_single_system_string_and_reasoning_knobs** — ONE joined
  system string; `reasoning_effort: low` + `include_reasoning: false`
  (reasoning is the enemy of time-to-first-word); `reasoning_format` is
  NEVER sent (Qwen-family knob — Groq 400s on it for this model family).
- **test_bearer_auth** — OpenAI-style auth header.
- **test_done_sentinel_skipped_bytes_after_still_count** — `[DONE]` is a
  sentinel to skip, not a terminator; treating it as EOF drops any bytes
  that follow in the same chunk.
- **test_truncated_stream_flushes_last_words** — the SSE flush rule
  end-to-end through a provider: the last words still reach the panel, but a
  stream with neither `finish_reason` nor `[DONE]` raises `incomplete`.
- **test_404_gives_an_actionable_message_never_an_edit_the_source_one** —
  Groq retires models on short notice; the message names the model and says
  to switch to Claude in Settings or update the app, never to edit source.
- **test_403_reports_actual_status** — a 403 labelled 401 sends the user
  debugging the wrong thing.
- **test_5xx_unavailable / test_stream_drop_message** — remaining matrix
  rows.
- **test_helpers_are_importable_template_pieces** — `extract_openai_delta` /
  `is_done_sentinel` stay importable; the Groq module is the template for
  future OpenAI-compatible providers.

### tests/test_warm.py — pre-warm

- **test_warms_the_models_endpoint** — GET `<origin>/v1/models`, body read
  to completion (that's what returns the connection to the pool).
- **test_throttled_to_one_per_origin_per_2s** — the throttle boundary at
  exactly 2.0 s, driven by an injected clock.
- **test_throttle_is_per_origin** — switching providers must not starve the
  new origin's warm.
- **test_failed_warm_never_raises** — a failed warm costs nothing; a raising
  warm would kill the session task that fired it.

### tests/test_machine.py — the §5 session-machine invariants

TestHappyPath:

- **test_record_stop_answer** — the whole pipeline through fakes: frames
  routed, partials emitted, stop taken, metrics sane, deltas in order, done
  carries the full answer, audio stopped.
- **test_audio_level_events_flow_while_recording** — rms reaches the sink
  tagged with the session id.
- **test_stop_warms_the_provider_origin** — the stop-press warm exists (it
  overlaps the TLS handshake with the finalize).

TestKeyChecks:

- **test_missing_deepgram_key / test_missing_llm_key_on_start /
  test_missing_llm_key_on_ask** — key checks fail the COMMAND (error
  Result), before any session is created or superseded.

TestSupersession (rule 1):

- **test_new_start_aborts_active_and_drops_its_events** — the superseded
  session's stream is aborted; its late update AND its abort-caused socket
  death produce zero events.
- **test_ask_over_recording_supersedes_silently** — the ask-over-recording
  race: recording dies silently, typed question answers.
- **test_ask_over_streaming_answer_supersedes** — asking over a streaming
  answer; the old session's done is dropped by id (only the new done
  arrives).

TestLatestStartWins (rule 2):

- **test_second_start_during_first_connect_wins** — double-record during
  connect: the loser resolves late, tears down, never installs itself;
  frames route to the winner only.
- **test_connect_failure_after_losing_is_silent** — the loser's connect
  FAILURE is also silent; reporting it would flash a scary error over a
  healthy new recording.

TestStopContract (rule 3):

- **test_unknown_id_not_taken / test_stop_during_connect_not_taken /
  test_second_stop_while_first_runs_not_taken /
  test_stop_after_completion_not_taken /
  test_stop_after_error_teardown_not_taken** — every "not taken" case
  returns an error Result. Why: every other outcome arrives as an event; a
  silently ignored stop leaves the UI in "Finalizing…" forever.
- **test_not_taken_emits_no_events** — not-taken is a return value, never an
  event.

TestAudioRouting (rule 4):

- **test_frames_after_the_capture_cutoff_are_dropped** — a frame queued
  before Stop is accepted (it is drained); once the drain closes the capture
  cutoff, frames are dropped, since sending them would race the CloseStream
  flush. Rule 4 was redefined on 2026-09-22 (R06).
- **test_frames_for_stale_session_dropped** — a stale callback delivers to
  no one.

TestSttErrorPolicy (rules 5+6):

- **test_mid_recording_death_one_error_and_teardown** — ONE `stt_error`
  (second report suppressed), session torn down. Why: a silently truncated
  transcript answers the wrong question.
- **test_late_death_after_finalize_never_kills_streaming_answer** — the
  second half of rule 5: once the transcript is final, the STT stream's job
  is done; its late death must not kill an answer mid-stream.
- **test_connect_failure_surfaces_stt_connect** — connect failure surfaces
  through the event channel with the right code.

TestEmptyTranscript (rule 7):

- **test_whitespace_transcript_is_no_speech_never_an_llm_call** —
  `no_speech` with the actionable message, and the provider was NEVER
  called (`stream_calls == 0`).

TestAskPath (rule 8):

- **test_garbage_input_never_kills_a_live_session** — empty and >8000-char
  asks error WITHOUT superseding; the recording stays stoppable.
- **test_ask_resolves_id_before_any_event** — zero events before the command
  resolved.
- **test_ask_event_shape_and_metrics** — the trimmed question arrives as one
  `stt:partial {isFinal: true}` (both paths share one event shape), then
  done with `sttFinalizeMs` EXACTLY 0 (no STT stage — billing one would be
  a lie).
- **test_8000_chars_exactly_is_accepted** — boundary of the validation.

TestTimeoutInterplay (rule 9):

- **test_first_token_timeout** — a provider that never yields →
  `llm_first_token_timeout`, zero deltas painted, no done.
- **test_total_timeout_after_deltas** — first delta disarms the first-token
  timer; the total timer still fires; the painted delta stays; nothing
  paints after the timeout.
- **test_first_delta_disarms_first_token_timer** — slow-but-flowing deltas
  complete without any timeout firing.

TestProviderFailures:

- **test_status_error_surfaces_classified** — provider classification flows
  through to the session:error event.
- **test_connect_failure_retried_once_then_succeeds** — the retry policy
  wired into the machine (2 stream calls, 1 done, 0 errors).

TestCancel (rule 10):

- **test_cancel_is_silent_and_releases_slot** — no done, no error, event
  stream frozen; the slot is free for the next session (rule 11).
- **test_cancel_invalid_id_does_nothing** — never an error; the live session
  is untouched.
- **test_cancel_during_stop_is_silent** — the cancel-during-stop race: the
  finalize is abandoned with no events.

TestSlotRelease (rule 11):

- **test_after_error_next_session_starts_clean /
  test_after_done_next_session_starts_clean** — whatever the outcome, the
  slot released exactly once; the next session never supersedes a ghost.

TestRecordCap:

- **test_cap_auto_stops_and_answers_normally** — at the cap the machine
  emits `session:autostopped` and answers normally (the UI uses the event
  for its "Reached the 120s limit" status).
- **test_manual_stop_cancels_the_cap** — no phantom autostop after a normal
  stop.

TestAudioFailure:

- **test_audio_start_failure_surfaces_actionably_and_frees_the_slot** — a
  loopback device that fails to open surfaces "Could not open the system
  audio device…" (never the raw exception text), and the slot releases so
  the next attempt works once the device is back. Why: the generic
  "internal error" this replaced sent users hunting through logs for what
  is usually just "no default output device".

### tests/test_bridge.py — the pywebview bridge

JsApi (driven synchronously from a foreign thread against a live loop, the
way pywebview drives it):

- **test_ok_envelope / test_app_error_becomes_error_envelope** — commands
  resolve `{ok, value}` / `{ok:false, error:{code,message}}`; nothing
  throws across the language boundary.
- **test_unexpected_exception_never_leaks_its_text** — a raw RuntimeError
  becomes a generic `internal` message; exception text is not user copy.
- **test_type_validation_rejected_without_reaching_the_loop** — wrong-typed
  arguments (non-string ask/stop ids, non-dict patch) fail fast in the
  worker thread.
- **test_cancel_is_fire_and_forget_and_reaches_the_machine /
  test_cancel_with_bad_id_type_is_still_ok** — cancel returns ok
  immediately, is delivered via `call_soon_threadsafe`, and invalid input
  is never an error (the caller doesn't await it meaningfully).
- **test_get_settings_merges_hotkey_registration_state** — the runtime
  `hotkeyRegistered` flag rides on the settings view.
- **test_set_settings_notifies_and_a_failing_hook_never_fails_the_save** —
  window/hotkey application errors must not turn a successful save into a
  reported failure.
- **test_the_settings_hook_runs_off_the_core_loop** — applying settings
  re-registers the global hotkey, which joins the old message-loop thread
  and waits on the new one; that blocking work runs in a worker, never on
  the loop that pumps audio frames and events.
- **test_heartbeat_updates_liveness** — the crash-recovery watchdog's
  signal actually moves.
- **test_open_external_https_only** — https opens in the default browser;
  http/javascript:/non-strings are refused (the app never navigates its
  own webview).

WebviewEventSink (fake window capturing evaluate_js):

- **test_events_arrive_in_emission_order_with_payloads_intact** — five
  deltas with quotes/backslashes/newlines/unicode arrive in order and
  byte-identical after the double-JSON-encoding round trip. Why: event
  ordering is a correctness property (scrambled deltas scramble the
  answer), and naive string interpolation into evaluate_js corrupts
  payloads.
- **test_events_before_attach_are_held_not_dropped** — events emitted
  before the window exists wait for attach instead of vanishing.
- **test_a_throwing_webview_does_not_kill_the_pump** — a dying renderer
  breaks one dispatch, not the event stream.

### tests/test_providers.py — TestDefaultRegistry

- **test_ships_both_providers_anthropic_first** — the shipped registry
  contains exactly anthropic (first = recommended default) and groq, with
  the display names the Settings select renders.

### tests/test_deepgram_client.py — DeepgramStream over loopback WebSockets

- **test_interim_then_final_updates_and_closestream_flush** — subprotocol
  auth asserted server-side; interim/final callbacks; CloseStream triggers
  the server flush and finalize returns the full transcript including the
  held-back tail.
- **test_preopen_frames_buffered_and_flushed_in_order** — frames sent before
  the socket opened arrive first and in order; losing them clips the start
  of the question.
- **test_malformed_frames_ignored_never_crash** — hostile frames interleaved
  with real ones; the real transcript survives.
- **test_close_before_any_results_is_a_connect_failure** — Deepgram rejects
  bad keys by CLOSING (1008, DATA-xxxx reason, no error frame); surfaced as
  `stt_connect` quoting code+reason with the "check the API key" message.
- **test_mid_recording_death_after_results_is_stt_error** — a 1011 close
  after transcription started is a mid-recording death, not a connect
  failure.
- **test_error_frame_reported_once_with_detail** — Error frames surface once
  with their detail quoted.
- **test_connect_refused_raises_stt_connect** — nothing listening → the
  connect coroutine raises the structured error.
- **test_abort_suppresses_the_death_it_causes** — abort causes a close; that
  close is never reported (rule 1's "abort must not be reported as error").
- **TestAbortDuringHandshake::test_abort_before_the_socket_opens_closes_it_on_arrival**
  — abort() landing while the WebSocket handshake is in flight. The socket
  that opens a moment later belongs to nobody: it is closed at once (code
  1000) instead of spawning reader/sender/keepalive tasks that pinned it
  open until Deepgram's idle timeout, no error is reported, and a later
  finalize() returns immediately. Verified to fail without the fix.
- **test_finalize_is_idempotent_second_call_joins** — two concurrent
  finalize calls send ONE CloseStream and return the same transcript.
- **test_never_opened_stream_finalizes_immediately** — no socket → return
  what we have now, don't burn the 5 s cap.
- **test_dead_stream_finalizes_immediately_with_what_it_has** — an
  already-dead stream finalizes instantly with the partial transcript.
- **test_unresponsive_server_finalize_returns_at_cap** — a server that
  ignores CloseStream: finalize returns at the cap with the transcript so
  far; better a slightly clipped tail than a hung stop.
- **test_keepalive_sent_while_open_and_stopped_after_close_request** —
  KeepAlives flow during silence (Deepgram kills idle sockets ~10 s), and
  NOTHING follows CloseStream: a late KeepAlive errors on the CLOSING
  socket and fabricates a "lost connection" during a stop that is
  succeeding.

### tests/test_downsample.py — audio math

- **test_interleaved_stereo_unpacks / test_stereo_to_mono_averages /
  test_float_to_i16_clips** — the conversion chain, including clipping
  (overflow wraps horribly in int16).
- **test_48k_to_16k_reduces_by_three / test_16k_passthrough /
  test_empty_input** — resampling ratios and edge cases.
- **test_sine_survives_resampling** — a 440 Hz tone keeps its energy through
  the linear-interp resample (a wrong stride silently produces near-silence,
  which Deepgram transcribes as nothing).
- **test_device_chunk_to_16k_mono_i16 /
  test_frame_samples_constant_matches_spec** — one device chunk becomes the
  spec'd 16 kHz mono i16 stream; 2048 samples ≈ 128 ms.

### tests/test_hotkey.py — accelerator parsing (pure)

- **test_default_hotkey / test_case_and_spacing_tolerant /
  test_letter_and_digit_keys / test_function_keys / test_win_modifier** —
  the accelerator grammar maps to the right Win32 modifiers + VK codes.
- **test_invalid_forms** — empty, modifier-only, unknown keys, two keys,
  F25: all rejected (None) so registration reports failure honestly instead
  of registering the wrong key.

---

## Frontend (Vitest + Testing Library)

### src/__tests__/markdown-blocks.test.ts — block parser

- **headings demote (#→h3, cap h6)** — the page owns h1/h2; model output
  must not out-rank the app's own hierarchy.
- **trailing closing hashes stripped** — `# Title ##` renders "Title".
- **paragraph joining/splitting** — soft-wrapped lines join; blank lines
  split.
- **fence info string dropped, content verbatim** — code content renders
  as-is (including HTML-looking text) with no interpretation.
- **tilde fences + longer closers / wrong closer doesn't close** — fence
  matching rules (char + length).
- **unterminated fence at EOF = open block** — while streaming, a fence that
  hasn't closed YET must render as code, not as a paragraph that reflows
  when the closer arrives.
- **hr forms + hr-vs-list precedence** — `- - -` is a rule, not a list.
- **bullet + ordered lists (n. and n))** with start numbers — `3.` starts at
  3.
- **lazy continuation** — wrapped item text belongs to the item.
- **loose lists: blank line ends the list only if no item follows** — the
  spec'd rule verbatim.

### src/__tests__/markdown-render.test.tsx — rendering, XSS, streaming

- **bold/italic/nesting/underscore emphasis** — inline basics through the
  real component.
- **snake_case must not italicize** — the flanking-rule regression the spec
  calls out by name.
- **intraword asterisk allowed** — CommonMark behavior, documents intent.
- **unmatched delimiters stay literal** — `2 * 3` must not eat asterisks.
- **inline code exact-length closers, one-space padding strip, no emphasis
  inside code, backslash escapes** — inline code hazards.
- **links deliberately NOT parsed (+ javascript: stays text)** — there is no
  `<a>`, no href, nothing to sanitize. THE anti-XSS design decision.
- **XSS suite (8 payloads)** — `<script>`, `<img onerror>`, fence-escape
  `</pre><script>`, attribute-injection quotes, `<svg onload>`, `<iframe>`:
  each renders as literal text, zero live elements, and NO element carries
  any attribute beyond `<ol start>`. Model output is untrusted; this suite
  is the proof.
- **raw payload stays visible** — sanitizing by deletion would hide what the
  model actually said; we render it as text instead.
- **every cut point renders identically to a batch render** — the streaming
  invariant over a 6-document corpus: stream-then-finish DOM ===
  render-once DOM at every prefix length.
- **rendering any prefix never throws** — half-typed markdown (open fences,
  dangling emphasis) is the NORMAL streaming case.
- **completed blocks keep their DOM nodes** — node identity across updates
  (no flicker, no lost selection while streaming).
- **unchanged source is a no-op** — re-render with identical text keeps DOM
  identity.

### src/__tests__/format.test.ts — display formatting

- **mm:ss formatting + negative clamp** — the recording timer.
- **latency chip one-decimal seconds** — "0.9s to first word" is THE
  product number; its rounding is product behavior.
- **latency title breakdown** — the hover explains all three metrics.
- **hotkey display formatting** — chip shows "Ctrl+Shift+Space" regardless
  of stored casing.

### src/__tests__/app-flows.test.tsx — main flows through the mocked bridge

- **full record walk (idle→starting→recording→finalizing→answering→done)** —
  status lines per state, live tag, markdown-rendered answer, latency chip,
  transcript panel.
- **level meter + timer only while recording** — recording chrome scoped to
  the recording state.
- **events before the start promise resolved are replayed on adopt** — the
  bridge resolution race: the core may emit for s1 before the JS promise
  resolves; buffering-then-replay prevents losing the first partial.
- **stale events change nothing, ever** — wrong-id partials and errors are
  dropped (supersession safety on the UI side).
- **not-taken stop recovers to idle** — the stop return contract in action;
  without it the UI hangs in "Finalizing…" forever.
- **start failure shows the actionable error** — no_stt_key surfaces in the
  role=alert box.
- **120s cap event flips the status** — "Reached the 120s limit — answering
  now".
- **session:error → alert + idle + partial answer kept** — an error during a
  streaming answer must not erase what already painted (looks like data
  loss).
- **aborted error code never shown** — `aborted` means the user did it;
  showing it as an error would blame them for their own click.
- **ask: trimmed submit, input cleared on success** — and the answer flows
  through the same panels.
- **ask failure keeps the input** — so the user can retry without retyping.
- **empty/whitespace submits never reach the core** — validated at the edge.
- **ask disabled during starting/recording/finalizing, enabled while
  answering** — asking over a streaming answer supersedes it.

### src/__tests__/app-history.test.tsx — history + regenerate

- **offers Clear from one entry; navigation only from two** — a single
  answer must be clearable; prev/next and n/m appear only at 2+.
- **prev/next navigation shows the viewed entry** — n/m label and content
  switch together.
- **only the last 6 entries kept** — oldest trimmed; entry 1 gone, entry 2
  the oldest survivor.
- **new recording jumps the view to the live entry** — you always watch the
  question being answered now.
- **clear wipes, announces "History cleared", moves focus to Record** — the
  button focus lived on disappears; focus must land somewhere sensible.
- **aborted attempt that captured nothing is discarded** — an empty husk
  entry would pollute history.
- **failed attempt that captured a question is retired** — the transcript is
  user work; a half-streamed answer vanishing looks like data loss.
- **regenerate re-asks the viewed question as a NEW entry** — the old answer
  survives for comparison.
- **regenerate hidden while recording / without a question** — visibility
  rule.

### src/__tests__/app-settings.test.tsx — settings, styles, hotkey, first-run

- **gear opens settings and focuses the heading** — focus management on
  view swap (there is no dialog; the view replaces the main view).
- **provider select built from the registry** — options come from the
  settings view's provider list, not hard-coded (a new provider appears for
  free).
- **key fields: password type, "saved — type to replace" placeholder, always
  empty value** — write-only keys across the UI boundary.
- **save sends ONLY the key fields actually typed into** — untouched
  providers' keys survive every unrelated save.
- **save without touching keys omits `keys` entirely** — omission ≠ clear.
- **Escape closes and returns focus to the gear** — keyboard path.
- **save failures surface in the settings-local error box** — the main error
  box is hidden behind this view.
- **style chips: aria-pressed reflects the PERSISTED style** — the save's
  return value wins over the clicked chip; optimistic UI here can lie about
  what the next answer will use.
- **persisted flip updates the pressed chip** — the happy path of the same
  rule.
- **hotkey chip + status mention when registered** — discoverability.
- **taken notice when registration failed** — register honestly: a dead
  shortcut with no notice is the worst outcome.
- **hotkey:toggle does what Record does** — one behavior, two triggers.
- **hotkey IGNORED while settings open** — the user may be typing the
  hotkey itself into the hotkey field.
- **first-run nudge for missing SELECTED-provider key; no nudge when only an
  unselected provider's key is missing** — the nudge tracks what would
  actually block a recording.

### src/__tests__/app-copy.test.tsx — the Copy button

- **copies the markdown SOURCE and confirms** — the clipboard receives the
  raw markdown (bullets survive pasting), "Copied ✓" shows, and the screen-
  reader announcement fires.
- **falls back to execCommand when navigator.clipboard is missing** — the
  packaged app loads from file://, a non-secure context where
  `navigator.clipboard` is undefined; without the textarea+execCommand
  fallback, Copy fails only in the shipped build (the worst kind of bug —
  invisible in dev).
- **surfaces clipboard failure in the error box** — a denied clipboard is
  reported, not swallowed; no false "Copied ✓".
- **hidden until an answer exists** — visibility rule.

---

## Regression tests from the 15-agent audit (2026-08-07)

An adversarial multi-agent audit found bugs the suites above could not see —
each of these tests exists because a specific one of them shipped.

### Core

- **test_machine.py::TestCommandTicket::test_an_earlier_command_stalled_on_its_key_read_never_supersedes_a_later_one**
  — rule 2 applied to the WHOLE command, not just the connect. The key read
  hits DPAPI and can stall; an earlier Record press resuming late used to
  supersede the Ask the user typed afterwards, killing it silently. The
  losing command now reports `aborted` (never shown by the UI).
- **…::test_a_lone_command_is_never_self_superseded** — the ticket must not
  make ordinary single commands abort themselves.
- **TestErrorSuppression::test_nothing_but_the_error_is_emitted_once_a_session_failed**
  — rule 9 made structural: `_emit` now refuses every non-error event from a
  failed session, so a delta racing a timeout cannot paint. Previously only
  a guard inside `on_delta` enforced this, and deleting that guard broke no
  test.
- **…::test_mid_finalize_stt_death_tears_the_session_down** — rule 5's first
  half in the finalize window (previously only mid-recording and
  post-finalize were covered): the provider must never be asked to answer a
  truncated question.
- **TestStopContractExtra::test_stop_during_answering_is_not_taken /
  test_stop_of_an_ask_session_is_not_taken** — two stop-contract rows the
  original matrix missed.
- **TestAudioFailure::test_audio_start_failure_surfaces_actionably_and_frees_the_slot**
  — a loopback device that fails to open said "internal error"; now it names
  the device, leaks no exception text, and releases the slot.
- **TestAnswerTimeMisconfiguration** — settings can change between Record
  and Stop, so the key checks at start are a courtesy, not a guarantee.
  A provider removed mid-session fails with the "pick one in Settings"
  message; an LLM key removed mid-session fails with `no_llm_key`; a
  finalize() that truly hangs (past the stream's own 5 s cap) fails with
  `stt_timeout` and tears the stream down. In every case: no LLM call, and
  the slot is released.
- **TestConnectCrashes** — a non-`AppError` exception out of connect()
  surfaces as `stt_connect` with no raw exception text and frees the slot;
  the same crash from a start that already lost the race is silent.
- **test_deepgram_client.py::test_closestream_never_overtakes_queued_audio**
  — CloseStream used to be written directly on the socket while audio went
  through a queue. Under send backpressure it overtook queued frames, and
  Deepgram discards audio arriving after CloseStream: the tail of the
  question was silently lost. It now travels the same queue.
- **…::test_finalize_leaves_no_pending_tasks /
  test_unresponsive_server_finalize_still_cleans_up** — the sender task used
  to park on the queue forever after finalize, pinning the socket for the
  process lifetime.
- **…::test_bad_key_close_during_finalize_keeps_connect_classification** —
  Record-then-immediately-Stop with a bad key reported "lost the connection
  while finalizing", sending the user to debug their network instead of the
  key.
- **TestProductionWireConstants::test_url_pins_every_required_query_parameter**
  — nothing pinned the production URL; every test injected its own.
- **…::test_the_api_key_is_offered_as_the_second_subprotocol** — asserting
  only `subprotocol == "token"` still passes if the key is dropped.
- **…::test_no_keepalive_can_follow_closestream** — replaces a tautological
  assertion (the old handler returned on CloseStream, so nothing could ever
  be recorded after it); this one keeps reading.
- **test_sse.py::TestByteOrderMark (3 tests)** — a leading UTF-8 BOM fused
  onto the first field name, silently dropping the stream's first event.
- **test_hotkey.py::TestHostileAccelerators (4 tests)** — `"ß".upper()` is
  `"SS"`, so `ord()` raised TypeError; the exception escaped registration at
  launch and bricked startup until settings.json was hand-edited. Non-ASCII
  keys also mapped to unassigned Win32 VK codes.
- **test_settings.py::TestConcurrentWriters::test_a_bounds_save_racing_a_patch_never_loses_either_writer**
  — drag the window, then click Save within half a second: the unsynchronized
  read-modify-write dropped one writer's fields from disk AND cache.
  **test_no_stray_tmp_files_are_left_behind** pins the visible half — no tmp
  file survives a write — but nothing asserts the two writers get distinct
  names, so that half rests on the lock test above.
- **TestTransientRenameFailures (2 tests)** — antivirus real-time scanning
  and the search indexer hold a freshly written file open for a few
  milliseconds on Windows, so the atomic `os.replace` can fail with
  `PermissionError` when nothing is wrong; that surfaced as "Something went
  wrong inside the app core" on Save. The rename now retries briefly (five
  attempts, backing off from 20 ms). A transient failure saves cleanly with
  no tmp file left behind; a persistent one still raises, and memory keeps
  matching disk.
- **TestKeyCharsetValidation (2 tests)** — a key with a smart quote reached
  httpx and raised UnicodeEncodeError mid-answer, surfacing as "internal
  error"; it is now refused at save time with a message naming the cause.
- **test_providers.py::TestPreResponseTimeoutIsNotRetryable (2×2 tests)** — a
  read timeout waiting for response headers was classified `connect` and
  therefore retried, even though the server may already be generating the
  answer. Only genuine connect failures retry.
- **test_warm.py::TestOffLoop** — `warm()` raised RuntimeError off-loop,
  contradicting "never raises".

### Frontend

- **markdown-hardening.test.tsx — hostile-input hardening (5 tests)** —
  deeply nested emphasis (`*`×12000) overflowed the render stack, and React
  unmounts the entire root on an uncaught render error: one answer could
  blank the app mid-call. Delimiter-dense text was quadratic (18 KB froze
  the main thread for ~59 s). Now capped, with the normal-emphasis case
  pinned so the caps can't silently break real answers.
- **…CRLF normalization (3 tests)** — every block regex is `$`-anchored and
  cannot match a trailing `\r`, so a CRLF answer degraded EVERY construct
  (headings, rules, both list kinds, opening fences) into paragraphs.
- **…block signature identity (3 tests)** — the list signature joined item
  text with a separator that can itself appear in model output, so `- ab`
  and `- a\n- b` collided; `BlockView` then memoized away a real change and
  left stale DOM on screen.
- **app-gestures.test.tsx — Record during each phase (5 tests)** — the
  starting→silent-abort path (including cancelling the late-resolving
  session and ignoring its events), finalizing→ignored, answering→supersede,
  and both `aborted` command results rendering nothing.
- **…delta coalescing and ordering (2 tests)** — deltas fired in the same
  tick as a terminal event must flush first, or the answer flickers/jumps.
- **…answer panel scrolling (3 tests)** — the 28 px sticky-bottom rule: it
  follows the stream when you're at the bottom and never yanks you down when
  you scrolled up to re-read.
- **…accessibility wiring (3 tests)** — `aria-live`/`aria-busy` transitions
  on the answer panel, `role="status"`, `role="alert"`.
- **…copy confirmation timing** — "Copied ✓" reverts to "Copy".
- **…history guards** — Clear is disabled while a session is live.

---

## Hardening pass (2026-08-13)

### tests/test_protection.py — content protection is verified, not assumed

Being invisible to screen sharing is the moat feature, and
`SetWindowDisplayAffinity` returns a BOOL that is trivially ignored.

- **test_success_requires_the_os_to_confirm** — success means the affinity
  READ BACK equals `WDA_EXCLUDEFROMCAPTURE`, not that the setter returned
  truthy.
- **test_set_that_reports_success_but_did_not_stick_is_a_failure** — the
  whole reason for the read-back.
- **test_partial_protection_is_not_protection** — `WDA_MONITOR` hides from
  some capture paths but not the ones that matter for screen sharing.
- **test_unreadable_affinity_is_a_failure /
  test_a_throwing_api_fails_closed_instead_of_crashing_the_window_callback**
  — fail closed, and never raise out of a pywebview event callback.
- **test_missing_hwnd_fails_without_calling_the_os** — no HWND yet (the app
  retries) must not be reported as protected.
- **test_constants_match_the_win32_values** — the constants are the contract.

### tests/test_bridge.py::TestEventBatching — batched dispatch

Each `evaluate_js` is a blocking round trip on a worker thread and an answer
streams dozens of deltas per second, so the pump now drains the queue into
one call.

- **test_a_burst_is_delivered_in_one_call_in_order** — 20 queued deltas
  arrive in order and cost far fewer than 20 hops.
- **test_batches_never_exceed_the_cap** — a backed-up queue cannot produce
  one enormous script; order still holds across batch boundaries.
- **test_mixed_event_names_keep_their_relative_order** — batching must never
  reorder `stt:partial` → deltas → `llm:done`.
- **test_javascript_line_separators_cannot_break_out_of_the_script** —
  U+2028/U+2029 are JS source line terminators; the outer encode escapes them
  or the generated script is syntactically broken.
- **test_a_throwing_webview_does_not_kill_the_pump** (rewritten) — a failed
  dispatch loses that batch (the renderer is dying anyway) but the pump must
  still deliver later events.

### tests/test_hotkey.py::TestRegistrationStatus

- **test_empty_accelerator_is_disabled_not_a_failure /
  test_unparseable_accelerator_reports_invalid** — "invalid" and "taken by
  another app" are different user problems; collapsing them into one bare
  `False` sent people hunting for a conflicting app when they had a typo.
- **test_a_real_registration_reports_registered_and_a_conflict_unavailable**
  — real `RegisterHotKey` against an obscure combination, then a second
  manager contending for it, so the "unavailable" branch is exercised for
  real (skips if the environment refuses the probe key).

### Frontend — src/__tests__/app-signals.test.tsx

- **silent-capture hint (7 tests)** — loopback capture that produces nothing
  (audio on a headset, wrong output device, muted call) is the most common
  real-world failure, and today the user only learns at Stop. The hint
  appears after sustained silence while recording, never on a short pause,
  never once any audible frame arrived, treats digital silence as silence,
  clears the instant audio flows, disappears at Stop, and resets between
  recordings.
- **hotkey status messaging (4 tests)** — blames another app only for
  `unavailable`, says "isn't a shortcut Windows understands" for `invalid`,
  stays silent when the user deliberately disabled it.
- **content-protection warning (4 tests)** — silent while protection holds,
  a `role="alert"` when Windows refused, clears on a later success, and
  survives a recording cycle.
- **level meter accessibility** — the meter is `aria-hidden`; a bar that
  changes eight times a second is noise in a screen reader, and the silence
  hint now carries the same meaning in words.

### Frontend — src/__tests__/app-coverage.test.tsx (audit gaps)

- **clipboard fallback failure (3 tests)** — `execCommand` returning false
  must throw so the UI reports it instead of showing a false "Copied ✓", and
  neither path may leak a stray `<textarea>` into the DOM.
- **pre-adoption buffering on the ask path (3 tests)** — the buffer-and-
  replay race was only covered for `start_session`; now also for `ask`, plus
  the buffer being discarded when a start or ask fails so a later session
  cannot inherit orphaned events.
- **stop-not-taken keeps captured work (2 tests)** — a transcript the user
  already spoke is retired into history; an attempt that captured nothing is
  discarded.
- **regenerate targets the VIEWED entry (3 tests)** — re-asks the entry you
  navigated back to (not the newest), stays available while an answer streams
  and supersedes it, hidden when there is no question.

### markdown-render.test.tsx — extended streaming corpus

The invariant corpus was six documents, longest 78 characters. It now also
includes CRLF documents (so a cut between `\r` and `\n` is exercised), mixed
line endings, nested and adjacent emphasis, a control character inside list
item text, and a ~500-character realistic answer — every cut point of each.

---

## Second audit pass (2026-08-13, 14 agents)

### tests/test_app_wiring.py — the shell↔core seam

The seam nothing owned. Core tests use a fake event sink, bridge tests attach
a fake window themselves, frontend tests dispatch events by hand — so the one
line joining them was untested, and it was **missing for four commits**. The
shipped app accepted Record/Stop/Ask (js_api is its own channel) while the
event pump parked forever on `while self._window is None`: no transcript, no
answer, no level meter, no errors.

- **test_wiring_attaches_the_event_sink** — `_wire_window` attaches the sink.
  Verified to fail when the attach is removed.
- **test_wiring_subscribes_every_window_callback** — shown/loaded (content
  protection), moved/resized (debounced geometry), closing (geometry flush).
- **test_an_emitted_event_actually_reaches_the_window** — end to end across
  the seam: `sink.emit` → pump → `window.evaluate_js`, asserting the payload
  and the `app:event` name actually arrive.
- **TestRendererWatchdog** — the heartbeat is a hint, not a verdict. Chromium
  throttles timers in a hidden page to once per minute after five minutes
  minimized, so a stale heartbeat with a healthy page is the *normal* state
  of a minimized app; reloading on it wiped the history every ~25 s the
  window sat in the taskbar. The watchdog now probes the page with a direct
  `evaluate_js` first: a fresh heartbeat never probes; a stale one from a
  live page is forgiven (and the clock restarts, so no probe storm); a
  renderer that raises or hangs on the probe is reloaded, at most once per
  cooldown; no window yet is a no-op.
- **TestGeometryPersistence** — closing flushes the pending debounced save
  and cancels its timer; minimized geometry never overwrites real geometry;
  a save with no window is harmless.
- **TestShellHelpers** — `AICA_DEV_URL` wins over the built frontend; loop
  exceptions land in `crash.log`; the log rotates to `crash.log.1` once it
  grows past the cap.

### tests/test_bridge.py — batch failure isolation

- **test_a_transient_dispatch_failure_loses_nothing** (replaces the old
  "batch is lost" assertion) — a failed batch is retried event by event, so a
  momentary webview hiccup no longer costs up to 64 events. Batching had
  amplified the blast radius 64×, and a lost `llm:done` strands the UI in
  "Generating answer…" forever.
- **test_one_poison_event_cannot_take_its_neighbours_down** — serialization
  now happens per batch inside the guard; an unserializable payload used to
  kill the pump task outright, silencing the app for the rest of the session.
- **test_non_finite_numbers_do_not_produce_invalid_json** — `allow_nan=False`
  because `JSON.parse` rejects `NaN`/`Infinity`, which would drop the batch
  at the page instead of at the pump.

### tests/test_downsample.py::TestStreamingResampler — the audio Deepgram hears

Resampling each device chunk standalone was wrong three ways, all measured:

- **test_output_is_independent_of_how_the_device_chunks_the_audio** — the old
  code pinned both endpoints of every chunk, so output depended on how the
  device happened to slice the stream (up to **2.0** of waveform error on a
  unit-amplitude 1 kHz sine). Now byte-identical across chunk sizes 480 /
  1024 / 6000 / 7777.
- **test_no_samples_are_lost_over_a_long_stream** — a chunk length that did
  not divide evenly silently dropped audio (**15 985** samples where 16 000
  were owed, per second).
- **test_content_above_the_target_nyquist_is_filtered_not_folded** — 48 kHz →
  16 kHz with no low-pass folds everything above 8 kHz into the speech band;
  a 10 kHz tone arrived at near-full strength around 6 kHz. Now **~59 dB**
  suppressed.
- **test_speech_band_content_passes_through_intact** — 300/1000/3000 Hz
  preserved to <1%, so the filter buys alias rejection without dulling
  speech.
- **test_a_44100_device_also_resamples_without_drift** — not every device is
  48 kHz.
- **test_empty_and_tiny_chunks_are_safe / test_cost_stays_negligible_on_the_
  callback_thread** — this runs inside the GIL on PortAudio's thread; 125 ms
  of audio must cost a small fraction of 125 ms (measured ~1.5%).
- **test_downsample_chunk_threads_the_resampler_through** — the same input
  twice through one resampler is deliberately NOT identical output; it
  continues the phase and filter state rather than restarting.

### tests/test_hotkey.py + frontend

- **parse_accelerator** now rejects `f²`, `f①` and friends: `str.isdigit()`
  is true for 128 codepoints `int()` rejects, so the never-raise parser
  raised `ValueError`.
- **markdown-hardening.test.tsx::emphasis resolution soundness (5 tests)** —
  the opener-floor speedup went stale across delimiter characters: a failed
  `_` closer recorded a floor, then a `*` splice shifted every index past it
  and real emphasis silently vanished ("*the foo_ and bar_ conventions* use
  _trailing_ underscores" lost its `<em>`). Floors above a splice are now
  invalidated.
- **app-signals.test.tsx::audio that stops mid-recording (2 tests)** — the
  silence hint tracks *when* audio was last heard rather than *whether* it
  ever was, so a device unplugged mid-question (or Windows switching the
  default output, which leaves the stream bound to a dead endpoint) now
  warns instead of recording silence to the end.

---

## Mutation-testing pass (2026-08-13)

Eighteen mutations were applied one at a time against the green suite. **Seven
survived** — guards no assertion actually protected. Each is now closed, and
the fix was verified by re-applying the mutation.

- **`ask()`'s `_require_ticket` was deletable with all 288 tests green.** The
  only ticket test stalls `start_session`, so `ask`'s copy was never
  load-bearing. `TestMutationSurvivors::test_a_stalled_ask_never_supersedes_a_
  later_command` mirrors it with a stalled *ask*.
- **`retry.py`'s `not got_delta` was never exercised.**
  `test_never_after_a_delta` used a `stream_drop` failure, which
  `is_retryable` rejects anyway — so the retry was skipped for the wrong
  reason and the guard proved nothing. Now uses a `connect` failure, making
  `got_delta` the only thing preventing a doubled answer.
- **CloseStream ordering passed by accident.** On a fast loopback socket the
  sender drains the queue synchronously before finalize runs, so a direct
  `ws.send` could never be observed overtaking. `TestCloseStreamUnderBack
  pressure` stalls the first write so the race is real; verified to fail when
  the sentinel is replaced by a direct send.
- **Content protection had zero coverage** — no test referenced
  `_apply_content_protection`, `protection:ok`, or `protection:failed` at
  all. `TestContentProtectionVerdict` now covers a superseded slow attempt
  (must not overwrite a fresher verdict), a verified failure, and a late-ready
  HWND succeeding through the retries.
- **`f²` was accepted back** when `isdecimal()`+`isascii()` reverted to
  `isdigit()`: the never-raise test's candidate list had no `f<superscript>`
  input. Added, along with `f٢` (Arabic-Indic digits would otherwise map to a
  real F-key).
- **`_on_frame`'s `stop_requested` check and `_emit`'s `errored` guard** are
  defense-in-depth masked by synchronous sibling checks, so no loop-driven
  test could separate them. Both are now driven directly against the machine's
  own state — the window they cover is only reachable from a real audio
  thread, which is exactly why it needs a test rather than a reader's trust.

## Fresh-eyes findings (2026-08-13)

- **A Stop press racing the 120 s cap destroyed the answer.** Once the cap
  auto-stops, `stop_session` correctly returns "not taken" — but the frontend
  treated *any* refused stop as "the session is gone" and called
  `cancel_session`, superseding a session that was mid-finalize. Two minutes
  of recording produced no answer, no error, nothing. The refused-stop path no
  longer cancels; it keeps tracking and falls back to idle only after a
  bounded wait. Covered by `a stop that races the 120s cap` (both the
  answer-survives and the really-gone branches).
- **Minimizing persisted garbage geometry.** Windows reports minimized windows
  at (-32000, -32000) with a titlebar-sized rect; the debounced save wrote it,
  so quitting while minimized lost the window position the user had arranged.
  `plausible_bounds` now rejects it (`TestMinimizedGeometry`).


## Eye-level, profiles and startup pass (2026-09-18)

### tests/test_prompt.py::TestCallTypes — call-type tailoring

- **test_default_call_type_is_behavioral_and_is_the_original_prompt** —
  omitting `call_type` equals `behavioral`, and `ROLE_INSTRUCTIONS` is that
  role. Why: the original single-purpose prompt must stay the default.
- **test_unknown_call_type_falls_back_to_behavioral** — corrupt values fall
  back like unknown styles do.
- **test_every_call_type_has_a_distinct_role_and_grounding** — six distinct
  roles/groundings, none containing the literal `RESUME` (a sibling test
  asserts a JD-only prefix lacks it).
- **test_choices_follow_definition_order_with_labels** — the UI list comes
  from the module, in order.
- **test_sections_land_in_a_fixed_order** — role, resume, JD, focus, notes,
  grounding. Why: caching is a byte-prefix match; order must be stable.
- **test_grounding_appended_when_only_focus_or_only_notes_present** — any
  profile text triggers the grounding rule.
- **test_focus_and_notes_are_edge_trimmed_only** — interior formatting
  survives.
- **test_sales_and_meeting_use_the_call_context_header** — no
  "INTERVIEWING" on a sales call.
- **test_call_type_and_focus_live_in_the_prefix_never_the_suffix** — the
  style suffix is identical across all call types and focus values. Why:
  it sits after the cache breakpoint.
- **test_detailed_suffix_defers_structure_to_the_call_guidance** — STAR is
  behavioral's, not everyone's.

### tests/test_settings.py — profiles, layout, fonts

- **TestProfilesMigration** (4) — a legacy file becomes one lossless
  `Default` profile; load never writes; the migrated shape lands on the
  next save with mirrors; profiles win over stale mirrors.
- **TestProfilesLoadFallback** (6) — non-list profiles, corrupt entries,
  per-field fallback inside an entry, unknown active id, duplicate ids
  (first wins), the 20 cap, and layout/font fields falling back alone.
- **TestProfilesPatch** (11) — top-level resume patches only the active
  profile; callType/focus/notes validation; full-list replace assigns ids
  and rejects bad lists all-or-nothing; active id must exist; a new profile
  can be activated in the same patch; call types listed in order;
  `answer_config` carries the active profile; mirrors track a switch;
  layout mode and font sizes validate; per-mode window bounds are
  independent and survive a reload.

### tests/test_bounds.py::TestDocking

- **top_center** centres against the top margin, shrinks an oversize window
  (460x700 on a 672-tall work area → 656), handles negative origins;
  **primary_area** picks the origin display regardless of order.

### tests/test_bridge.py::TestDockAndCoreReadiness

- **dock_window** calls the shell hook, is a no-op without one, and
  swallows a raising hook. **Session commands wait for a late core then
  run**; **fail actionably if it never arrives** while settings still work.

### tests/test_app_wiring.py

- **TestInitialPlacement** (6) — first run docks top-centre and fits;
  stored on-screen bounds restore verbatim; off-screen keeps size but
  docks; no displays → OS placement; primary chosen over a left monitor;
  full-layout restore clamps to `FULL_MIN_HEIGHT`.
- **TestDockingAndLayoutSwitch** (7) — `_dock_current` keeps size and
  moves to the camera line; switching to prompter saves the full geometry
  and docks the strip (a repeated view does not move it again); switching
  back restores; a saved prompter position is restored, an off-screen one
  docks; bounds saves land under the current mode's key; runtime work
  areas divide by the scale.
- **TestDeferredCore** (3) — the window can exist before the core; `_build_core`
  hands the machine to the bridge; `shown`/`loaded` restart the watchdog clock.

### tests/test_machine.py::TestStopContract

- **test_stop_during_connect_is_taken_and_finalizes_once_the_socket_opens** —
  replaces the old "not taken" assertion: capture stops at once, finalize
  waits for the socket, early frames are kept, late frames dropped, one
  `llm:done`. **test_connect_failure_after_a_deferred_stop_still_surfaces**.

### tests/test_deepgram_client.py — flake fixes

- `wait_until` replaces fixed sleeps in the keepalive/handshake tests. Why:
  the Windows proactor quantises timers to ~16 ms and overshoots under
  load; a 0.16 s budget failed 7/7 in isolation on a slow machine.

### Frontend — src/__tests__/app-prompter.test.tsx (13)

- Strip renders instead of the panel; streams at the persisted size with
  the one-line question titled; top-anchored (scrollTop stays 0); A−/A+
  persist `prompterFontPx` and clamp; Dock calls `dock_window`; Exit/Esc
  and the header button switch layouts; the hotkey toggles recording in
  the strip; errors and the protection verdict alert and dismiss. Full
  layout: provider chip, dock button, answer-before-question DOM order,
  `answerFontPx` buttons, dismissible errors.

### Frontend — src/__tests__/app-profiles.test.tsx (12)

- Profile select hidden with one profile; switching persists
  `activeProfileId` and renders the PERSISTED value; call type persists;
  options come from the view in order. Settings: Add assigns a client id
  and Save activates it; Delete disabled at one; Duplicate copies with
  "(copy)"; labels switch for sales; canonical ids adopted after Save; a
  key typed and emptied does NOT erase (BUG-03); Remove sends an explicit
  empty key; the unsaved-changes guard; "Get a key" opens externally.

### Frontend — src/__tests__/app-settings.test.tsx (changed)

- The save-shape test now asserts `patch.profiles[0].resume` and
  `patch.activeProfileId` instead of top-level `resume`.

## Production-readiness pass (2026-09-22)

### Frontend: status snapshot, command generations, drafts, contract


Frontend total after this pass: 235 tests in 18 files (was 173 in 13).

#### Changed existing entries

##### src/__tests__/app-history.test.tsx
- replace **hidden until 2+ entries** with **offers Clear from one entry; navigation only from
  two** — a single answer must be clearable (review §5); prev/next and n/m appear only at 2+.
- **an aborted attempt that captured nothing is discarded** — now asserts no navigation (the
  bar itself is visible from one entry).

##### src/__tests__/app-gestures.test.tsx
- **during 'starting' aborts silently…** — the status copy is now "Starting system-audio
  capture…" and the test asserts no "microphone" wording anywhere (loopback captures system
  output; §4).
- **during 'finalizing' is ignored** — the button now reads "Finalizing…" with
  `aria-disabled="true"` (not `disabled`, so keyboard focus survives Stop); a click still does
  nothing.

#### New files

##### src/__tests__/app-status.test.tsx — protection verdict + status snapshot (R01, R07, R11) — 16 tests
- **stays an alert after opening Settings, and Settings claims nothing false** — the R01
  reproduction: `protection:failed`, open Settings, the alert is still there and the old static
  "hidden from screen sharing" sentence is gone.
- **shows 'not confirmed' before any verdict, in the full view and Settings** — tri-state: no
  verdict never reads as protected.
- **shows the confirmed verdict once Windows reports it**.
- **shows 'not confirmed' in the prompter strip too**.
- **Settings opened from the prompter keeps the failure alert**.
- **adopts a failed verdict on load with no push event at all** — `get_status()` on mount, so a
  verdict emitted before React subscribed (or before a reload) is not lost.
- **an older snapshot cannot overwrite a fresher pushed verdict** — revision ordering closes the
  snapshot-vs-event race (§8 R01).
- **a stale protection event is ignored once a newer revision is known**.
- **a failed or missing status command leaves the verdict unknown** — older core / error path.
- **re-adopts a session still recording behind a reloaded page, so Stop works** — no second
  session is started (§8 R11).
- **does not resume an idle or unknown-phase session**.
- **drops events stamped for a superseded page generation** — `pageGen` < own generation
  (a timed-out `evaluate_js` landing after reload, R07).
- **parseStatus: accepts the Result envelope and a bare object / rejects malformed snapshots /
  normalizes a missing session to idle** (3 tests) — a malformed snapshot must never flip the
  verdict.
- **settings load failure: says so and retries until the core answers** — the first-load failure
  used to leave settings null forever with no message (R11).

##### src/__tests__/app-races.test.tsx — command generations, ordering, recovery (R04, R07, R08, §4, §5, R03 contract) — 19 tests
- **a stale start-aborted does not null the newer session's id** — start A, abort, start B → s2,
  then A resolves `aborted`: B keeps Stop, its events and a working stop (§8: stale-abort branch).
- **a stale start-failed does not flip the newer session to idle or show its error** — distinct
  expectation for the stale-failure branch (§8).
- **an explicit abort is honoured even after a newer start began** — A resolving ok after B began
  is cancelled, never adopted (the shared `abortStartRef` was reset by B).
- **a stale response does not wipe events buffered for the newer command**.
- **only the latest ask is adopted; the stale one's session is cancelled** — overlapping asks in
  reverse resolution order.
- **a stale ask failure shows no error over the newer answer**.
- **two fast text-size clicks step twice, applied in click order** — quick patches are
  serialized and font steps start from the last requested size.
- **a re-delivered delta (same seq) is not painted twice, even when the answer errors** — `seq`
  dedup; the `session:error` path has no `llm:done` to repair duplicates (§8 R07).
- **events without seq are still accepted (older core)**.
- **an answer streaming past 20 s survives: a delta disarms recovery** (R08).
- **an Ask accepted after a refused stop is not killed by the old timer** — the §7/§8 later-Ask
  case; recovery is session-scoped.
- **recovery cancels the core session so UI and core agree** (§8 R08: recovery used to drop only
  the UI's tracking).
- **asks the core first and keeps waiting while it reports the session alive** — `get_status`
  replaces the silence guess when available; a session no longer reported live is recovered.
- **the global hotkey stops a recording running behind Settings** / **Settings shows the
  recording and a Stop & Answer button** — Stop stays reachable (§4).
- **shows Finalizing in the strip instead of a dead Record button** (§5 prompter status).
- **marks a truncated answer, keeping its text and Copy / marks a refused answer / says nothing
  for a complete or unstamped answer** — `llm:done.finish` from the R03 contract (CONTRACT §4).

##### src/__tests__/app-drafts.test.tsx — Settings draft safety (R09) — 9 tests
- **repeated Escape keeps the draft and the Settings view** — the R09 reproduction.
- **repeated Back keeps the draft too** — only the explicit Discard discards.
- **'Save and go back' saves, then leaves** / **…stays put, draft intact, when the save fails**.
- **serializes saves and makes the form read-only while one is in flight** — visible "Saving…",
  a second click sends nothing, edits cannot be overwritten by the response.
- **a key typed after Remove wins over the queued removal** / **Undo remove cancels a queued
  removal** (§7/§8 R09).
- **Add and Duplicate are disabled at the core's 20-profile limit** / **Duplicate keeps the
  copy's name within the 60-character limit** — the core rejects the whole save otherwise.

##### src/__tests__/bridge.test.ts — readiness wait — 3 tests
- **many early calls share ONE ready listener, and all resolve once ready** — the 3 s heartbeat
  used to add a never-firing `pywebviewready` listener per tick while the bridge was absent.
- **a command answers OFFLINE instead of hanging when the bridge never appears** (R11: bounded
  `whenReady`).
- **getStatus reports 'no status command' for an older core**.

#### Follow-up additions (contract §5–13)

##### src/__tests__/app-races.test.tsx — recording countdown (3 more tests)
- **follows the core's deadline, not the local tick** — session:recording.deadlineMs drives
  "x left" (the local tick starts before the device opens and drifts).
- **ignores a deadline for another session**.
- **falls back to the local tick when the core sends no deadline**.

##### src/__tests__/format.test.ts (1 more test)
- **title includes the audio drain stage when the core reports it** — optional udioDrainMs.

##### src/__tests__/app-contract.test.tsx — shell/release contract (§8–12) — 11 tests
- **version chip comes from the single version source** — 
+__APP_VERSION__, no literal.
- **sends the revision the draft was built on, then advances it** — aseRevision on Settings saves.
- **a stale-revision rejection reloads settings, says so, and keeps the draft** — nothing lost;
  the next save is built on the fresh revision.
- **an older core without settingsRevision gets no baseRevision**.
- **warns next to a key stored without encryption** — keyStorage: plaintext.
- **says when the settings file could not be read, naming the backup** (main view and Settings) /
  **is silent for a normal or first-run file**.
- **guards while the draft is dirty, prompts on a cancelled close, releases on Discard** —
  set_close_guard + window:close-requested.
- **core:failed shows a persistent alert; core:ready clears it** / **an older core event cannot
  undo a newer one** / **a failed core from the load snapshot shows the alert in the prompter too**.

### LLM answer path: completion validation, stale cleanup, secrets


#### Changed existing entries

- tests/test_providers.py **test_mid_stream_drop_is_stream_drop_kind** — the body now carries
  only a text delta before the drop (no terminal event): Anthropic now stops reading at
  `message_stop`, so a completed body would never reach the drop.
- tests/test_providers.py **test_truncated_stream_flushes_last_words** (Groq) — the unterminated
  final line is still flushed and its words still reach the panel, but a stream with neither
  `finish_reason` nor `[DONE]` now raises `incomplete` instead of passing as a finished answer.
- tests/test_providers.py **test_404_gives_an_actionable_message_never_an_edit_the_source_one**
  (renamed from test_404_points_at_the_pinned_model_constant) — the Groq 404 message names the
  model and tells the user to switch to Claude in Settings or update the app; it must never
  mention a source file or constant (R13).
- tests/test_retry.py `run()` reads `AnswerResult.text` (stream_answer now returns AnswerResult).

#### New tests

##### tests/test_providers.py — TestProviderCompletionValidation (R03, 9 tests × 2 providers)

End to end through `stream_answer` over MockTransport, parameterized over Anthropic and Groq:

- **test_error_event_before_any_text_fails_and_is_not_retried** — an error event inside an
  HTTP 200 stream is `provider_error`, classified `llm_http` with the provider's message; the
  server heard us, so exactly one request.
- **test_error_event_after_text_fails_and_keeps_the_partial** — the painted partial stays, the
  outcome is a failure, never a partial "success"; no retry after a delta.
- **test_valid_but_empty_output_is_an_error_not_a_blank_success** — a properly terminated stream
  with no text is `empty_answer` (it used to be a finished blank entry).
- **test_whitespace_only_output_is_an_empty_answer** — whitespace is not usable text.
- **test_clean_but_premature_eof_is_incomplete** — EOF without the terminal event (even with the
  SSE final-line flush delivering the last words) is `incomplete`, "stopped before … finished".
- **test_token_limit_completion_is_reported_as_truncated** — `max_tokens` / `length` completes
  with `finish="truncated"` and the text intact.
- **test_empty_output_at_the_token_limit_says_so** — the empty-answer message says the length
  limit was hit.
- **test_refusal_is_reported_as_refused** — `refusal` / `content_filter` → `finish="refused"`.
- **test_normal_completion_is_complete** — `end_turn` / `stop` → `finish="complete"`, deltas
  byte-identical to the answer.

##### tests/test_providers.py — TestProviderSpecificCompletion

- **test_anthropic_ping_only_stream_is_incomplete_not_success** — `ping` data lines flip the
  transport's "produced" flag; completion still requires `message_stop`.
- **test_anthropic_message_stop_without_message_delta_is_complete** — a missing stop_reason is
  not an error.
- **test_anthropic_context_window_stop_is_truncated** — `model_context_window_exceeded` counts
  as truncation.
- **test_anthropic_overloaded_and_rate_limit_stream_errors_map_like_http** — in-stream
  `overloaded_error` / `rate_limit_error` get the same user copy/codes as HTTP 529/429.
- **test_groq_finish_reason_without_done_is_complete** — either terminal signal suffices.
- **test_groq_rate_limit_stream_error_maps_to_rate_limit** — in-stream rate limit →
  `llm_rate_limit`.

##### tests/test_providers.py — TestGroqSpecifics additions

- **test_unterminated_final_done_line_still_flushes_and_completes** — the SSE flush feature is
  kept: an unterminated final `data: [DONE]` still completes the answer.
- **test_400_model_decommissioned_gets_the_same_actionable_message** — Groq's
  `model_decommissioned`/`model_not_found` body on a 400 is treated as a retired model.
- **test_other_400_is_not_misreported_as_a_retired_model** — a plain 400 keeps its status copy.
- **test_finish_reason_helper** — `extract_openai_finish_reason` is an importable template piece.

##### tests/test_providers.py — TestSecretsStayOutOfReprs

- **test_request_repr_never_contains_the_key_or_profile** (× 2 providers) — `ProviderRequest`
  headers (API key) and body (resume/profile) are `repr=False`; a repr in a traceback, pytest
  output or log line must not leak them.

##### tests/test_retry.py — TestAnswerResult

- **test_a_provider_without_a_stream_end_is_trusted_as_complete** — third-party/fake providers
  that yield bare strings keep working.
- **test_stream_end_metadata_is_carried_into_the_result** — StreamEnd reaches AnswerResult and
  never reaches the panel as a delta.
- **test_no_text_is_an_empty_answer_failure_and_never_retried** — the post-condition that
  replaced "return the concatenation even when empty".
- **test_a_raising_on_delta_closes_the_provider_stream_immediately** — the provider generator
  (and its HTTP response) is closed deterministically when the delta sink raises, not at GC.

##### tests/test_warm.py — TestTaskOwnership

- **test_an_in_flight_warm_is_strongly_held_until_it_finishes** — asyncio holds tasks weakly
  and production callers drop the returned task; the warmer keeps a strong reference until the
  warm finishes, then releases it.

##### tests/test_machine.py — TestAnswerCompletion (R03 at the session boundary)

- **test_done_reports_a_complete_finish** — `llm:done` carries `finish: "complete"`.
- **test_a_token_limit_answer_is_done_but_marked_truncated** — truncation is a done with
  `finish: "truncated"`, not an error.
- **test_an_empty_answer_is_a_session_error_not_a_blank_done** — one `session:error`
  (`llm_http`), no `llm:done`, slot released, no retry.
- **test_an_in_stream_error_after_text_is_an_error_with_the_partial_painted** — the delta
  stays painted, the terminal event is the error.

##### tests/test_machine.py — TestStaleCommandCleanup (R04, backend half)

- **test_a_late_aborted_start_leaves_the_newer_recording_untouched** — a start stalled on its
  key read that resolves `aborted` after a newer start is recording installs nothing, stops no
  audio, aborts no stream; the newer session still stops and answers.
- **test_a_late_cancel_for_a_superseded_id_never_cancels_the_newer_session** — the frontend's
  stale-success cleanup (`cancel(oldId)` sent after a newer start) is a no-op for the newer
  session.

#### Follow-up: public slot accessor

##### tests/test_machine.py — TestActiveSnapshot

- **test_it_tracks_the_live_slot_through_its_lifecycle** — `active_snapshot()` is None when
  idle, reports id/kind/phase while recording and after Stop, and is None again once the
  session is done and released.
- **test_a_cancelled_session_reads_as_idle_and_the_copy_is_immutable** — the snapshot is a
  frozen copy (callers cannot mutate machine state through it) and a cancelled session reads
  as idle.

Fakes updated to the accessor: test_bridge.py `FakeMachine.active_snapshot` (idle) and
`SlotMachine.active_snapshot` (aborted slot reads as idle); test_app_wiring.py
`ShutdownMachine.active_snapshot`.

### Audio stop/drain and recording lifecycle


#### tests/test_capture.py (NEW) — LoopbackCapture stop/drain contract, no device (R06)
| Test | What it pins |
| --- | --- |
| `TestStopAndDrain::test_every_captured_sample_is_delivered[1,100,1600,2047,2048,5000]` | `stop_and_drain()` delivers every captured sample: full 2,048-sample frames, then one short final frame. Previously `stop()` cleared up to 2,047 samples (~128 ms), and a recording shorter than one frame delivered nothing. |
| `TestStopAndDrain::test_the_in_flight_callback_lands_before_the_tail_and_close` | Capture cutoff = `stop_stream()` returning: a callback that PortAudio finishes inside `stop_stream` is included ahead of the remainder, and the stream is then closed. |
| `TestStopAndDrain::test_nothing_is_delivered_after_the_cutoff` | A straggler chunk after the drain posts nothing. |
| `TestStopDiscards::test_stop_discards_the_remainder` | `stop()` (cancel/supersede) keeps discard semantics. |
| `TestStopDiscards::test_a_callback_after_stop_never_reaches_the_old_sink` | A late device callback after stop cannot post into any session's sink. |
| `TestStopDiscards::test_a_failed_chunk_is_dropped_without_escaping` | An exception in chunk processing never escapes into PortAudio's C callback. |

#### tests/test_machine.py — changed/added
| Test | What it pins |
| --- | --- |
| `TestAudioRouting::test_frames_after_the_capture_cutoff_are_dropped` (replaces `test_frames_after_stop_requested_are_dropped`) | Rule 4 redefined: a frame queued before Stop is accepted; once the drain closes the cutoff, frames are rejected (they would race CloseStream). |
| `TestAudioRouting::test_drained_tail_reaches_the_stream_before_finalize` | Order is audio, then the drained tail, then finalize (CloseStream last). |
| `TestAudioRouting::test_drained_tail_during_connect_is_buffered_then_finalized` | Stop while connecting: the drained tail goes to the pre-open buffer and is finalized after the socket opens. |
| `TestAudioRouting::test_cancel_discards_instead_of_draining` | Cancel uses the discard stop, never the drain. |
| `TestMutationSurvivors::test_frames_after_the_cutoff_are_dropped_whatever_the_phase` (replaces `test_frames_arriving_between_stop_request_and_phase_flip_are_dropped`) | The `capture_closed` flag alone rejects a frame. |
| `TestAudioLifecycle::test_a_known_drain_delay_is_counted_in_stop_to_first_token` | Review §8 criterion: a 400 ms fake-clock drain adds exactly 400 ms to `firstTokenMs`/`totalMs`, shows up as `audioDrainMs`, and is not folded into `sttFinalizeMs`. |
| `TestAudioLifecycle::test_a_slow_device_open_does_not_block_the_loop_or_the_connect` | R10: the device open runs on the audio worker, and the STT handshake completes meanwhile. No cap and no `session:recording` until capture is running. |
| `TestAudioLifecycle::test_a_slow_drain_does_not_block_the_loop_and_its_tail_precedes_finalize` | R10: a 300 ms blocking drain on a real worker thread leaves the loop responsive. Finalize waits for it, and the tail posted from the worker lands before finalize. |
| `TestAudioLifecycle::test_a_hung_drain_is_bounded_and_the_answer_still_arrives` | `Timeouts.audio_drain_s` bounds a hung device stop. |
| `TestAudioLifecycle::test_stop_while_the_device_is_still_opening_drains_after_the_open` | The single audio worker runs start, then drain. The device is never left open, and no cap is armed after Stop. |
| `TestAudioLifecycle::test_the_cap_runs_from_capture_start_even_while_connect_hangs` | R10: the cap auto-stops even when Deepgram never finished connecting. It used to arm only after connect. |
| `TestAudioLifecycle::test_recording_event_carries_one_core_deadline` | `session:recording {deadlineMs, capMs}` comes from the injected wall clock. |
| `TestAudioLifecycle::test_a_device_open_failure_after_a_supersede_is_silent` | A device-open failure that lands after the session was superseded emits no error. |

#### tests/conftest.py
Adds `InlineExecutor`, which runs audio-worker calls synchronously so the suite stays deterministic; threaded tests pass a real `ThreadPoolExecutor` instead. Also adds `Harness(audio_executor=...)` and `FakeAudio.stop_and_drain`, with `tail`/`drain_delay_s`/`loop` knobs and a `drains` counter.

### Shell, bridge and store


Every entry below was checked against the pre-fix behavior. The R02, R05, R09, R11,
late-start and R12 regressions were also mutation-checked: re-injecting the old code
makes them fail (scratchpad/shell_mutation_check.py).

#### tests/test_secrets.py — DPAPI fails closed (R02)

- **test_keystore_unavailable_fails_closed** / **test_failing_keystore_fails_closed** —
  replace the two tests that pinned the silent `plain:` fallback. `encode_secret` now
  raises `SecretEncryptionError` when there is no keystore or `protect` raises, so no new
  key is ever stored in a recoverable form while Settings says "encrypted".
- **test_storage_kind_is_read_from_the_prefix** — `secret_storage` reports `enc:` as
  "encrypted", legacy `plain:` as "plaintext", and anything else as None.

#### tests/test_settings.py — TestEncryptionFailsClosed (R02)

- **test_a_failed_encryption_refuses_the_save_and_keeps_the_old_key** — DPAPI fails during a
  key replacement: the save raises "…NOT saved…", the file bytes are unchanged, the old key
  still decodes, and the other field in the same patch (resume) was not applied either.
- **test_saved_keys_report_how_they_are_stored** — the view's `keyStorage` shows
  "encrypted" for a newly saved key (never key material).
- **test_a_legacy_plaintext_key_is_reported_and_migrated_on_save** — a `plain:` key from an
  older build still decodes (not lost on upgrade), is reported as "plaintext", and is
  re-encrypted by the next successful save of any field.
- **test_a_legacy_key_that_cannot_be_reencrypted_keeps_value_and_true_status** — when DPAPI
  still fails, an unrelated save succeeds, the legacy key keeps working, and it is still
  reported as "plaintext". Migration is never implied because some other field saved (§8).

#### tests/test_settings.py — TestUnloadableFilePreservation (R05)

- **test_a_geometry_save_backs_up_a_corrupt_file_before_replacing_it** — the untouched-launch
  data loss: invalid JSON, then an automatic `set_window_bounds`. The original bytes are now in
  exactly one `settings.json.invalid-<stamp>-<id>.bak` before the file is replaced, the view
  reports `settingsFile` = {load: invalid, backup: <name>}, and later writes add no more backups.
- **test_if_the_backup_cannot_be_made_nothing_is_overwritten** — the backup write fails: the
  geometry save stays silent, a user save raises "…NOT overwritten…", and the original bytes
  (invalid UTF-8 here) survive. Once the backup can be written, the next save preserves the file
  and then writes.
- **test_an_unreadable_file_is_distinguished_and_preserved** — a read error at startup (sharing
  violation) loads as "unreadable", not as a first run. The still-valid file is copied to a
  `.unreadable-` backup before the first write.
- **test_a_file_still_unreadable_at_write_time_is_not_overwritten** — if the bytes still cannot be
  read when the first write comes, they cannot be preserved, so the write is refused.
- **test_a_missing_file_is_a_first_run_with_no_backup** / **test_a_valid_file_reports_ok_and_is_never_backed_up**
  — the three load states stay distinct, and only unloadable files are backed up.
- **test_backup_names_never_collide** — two stores backing up in the same second get distinct
  names (timestamp plus a random suffix, opened with `xb`, so nothing is ever overwritten).

#### tests/test_settings.py — TestSaveRevision (R09 backend)

- **test_each_save_advances_the_revision** — `settingsRevision` counts successful patches.
  Geometry saves do not count.
- **test_a_stale_whole_profile_save_cannot_overwrite_a_newer_one** — two saves built on the same
  `baseRevision` are applied in reverse submission order. The late, older one is rejected with
  "…newer save…" and version B stays in memory and on disk. The lock and atomic replace
  prevent torn files but did not prevent this stale write.
- **test_a_malformed_base_revision_is_rejected** — strings, booleans, null and floats are refused
  and nothing is applied.
- **test_saves_without_a_base_revision_keep_last_write_wins** — patches without a precondition
  (quick settings) behave as before.

#### tests/test_bounds.py — title bar reachability and plan_restore (R12)

- **TestTitleBarReachability** — a window whose body overlaps a display while its title bar
  sits above it is no longer "visible" (the old 40 px overlap test kept it). A maximized frame's
  -8 px offset, an oversized window with a reachable title bar, and a display above the primary
  (negative y) are all still restored.
- **TestPlanRestore** — the pure decision used by startup and layout switches: a position on any
  connected display is restored; an unplugged display docks on the preferred display at the saved
  size; with no preferred display the primary is used; with no display information at all the
  mode's size is still applied (position None); a preferred display is honored even when
  enumeration returned nothing.

#### tests/test_app_wiring.py — shell additions

- **TestRendererWatchdog** (updated) — pages are marked booted via `booted()`. After a reload the
  page must load (or outlive the boot grace) before it can be judged again.
- **TestWatchdogBootGate** (R11) — a page that was never shown is never probed, however old the
  construction-time heartbeat is. A booting page 30 s after `shown` is neither probed nor
  reloaded. A boot that never completes is recovered after `BOOT_GRACE_S`. A reloaded page gets
  a fresh grace period (the old code reloaded it again mid-boot 20 s later). The first
  `heartbeat()` counts as booted.
- **TestStatusAndStartupFailure** (R01/R11) — protection starts "unknown". A verdict updates the
  `get_status()` snapshot, and its push event carries the same `revision` and the tri-state value.
  A newer verdict gets a higher revision and a repeat keeps it. A core build that raises (httpx
  import blocked) is reported as `core:failed` with the snapshot revision, and `start_session`
  then fails at once with "failed to start" instead of waiting 25 s. A built core emits
  `core:ready`. `loaded` bumps the event page generation and clears a dead page's close guard.
- **TestShutdown** — window close: the live session is cancelled, which queues its capture stop
  on the app-owned audio worker. The shared HTTP client is closed, the worker drains that stop
  and is shut down, and the core loop stops. A hung device call returns an unclean verdict
  within the budget, so `main()` can exit hard instead of joining a non-daemon thread forever.
  Exiting before the core was built is clean.
- **TestCloseGuard** (R09 native close) — with no guard the window closes. With the guard set
  and a live page, the first close is cancelled (`closing` handler returns False to pywebview)
  and `window:close-requested` is emitted, and the second attempt closes. A stale page or a page
  that never booted cannot hold the close.
- **TestDockingAndLayoutSwitch** (updated + R12) — `_dockable` now pins every work area. Full
  geometry saved on display A is restored exactly while the prompter sits on display B (the old
  code docked it on B). An unplugged display docks on the current one at the saved size. With no
  display information the mode's size is still applied (the old code returned without placing).

#### tests/test_bridge.py — shell additions

- **test_a_failed_core_releases_a_waiting_command_at_once** (R11) — a start already waiting on
  the core returns immediately with the startup failure when `_core_failed` fires.
- **TestStatusSnapshot** (R01) — the snapshot shape before the core exists is JSON-able. The
  revision moves only when a field changes. Machine phases map to page phases, and aborted/done
  slots read as idle. A wedged loop reports session phase "unknown" instead of hanging.
  Core failure appears in the snapshot with its revision.
- **TestLateCommandResults** — a start or ask that completes after the 30 s bridge deadline used to
  leave a session running while the page was told the command failed; `future.cancel()` cannot
  stop a coroutine that already finished. Now exactly that late session is cancelled. An
  on-time start is never reaped.
- **TestShellCommands** — `open_external` rejects host-less, whitespace, control-character and
  oversized https URLs before they reach the OS shell. `set_close_guard` accepts only a real
  `true`. `heartbeat()` marks the page booted.
- **TestSaveSerialization** (R09) — a save and its window/hotkey hook complete before the next
  save's patch starts. Before, a slow layout switch from save 1 could land after save 2.
- **TestBoundedDelivery** (R07) — every payload carries `seq` and `pageGen`. `audio:level`
  coalesces latest-wins per session, keeping its session id and its original position and seq.
  The queue bound sheds levels first, then interim partials, then deltas, never reserved
  events, and never reorders. After a timed-out batch the terminal event is re-sent with the
  same seq, while its deltas are not re-sent, so a late execution of the abandoned call cannot
  show them twice. A permanently hung renderer never gets more than `MAX_HUNG_DISPATCHES`
  threads, and its terminal event stays queued.

#### tests/test_hotkey.py

- **test_a_registration_that_finishes_after_the_wait_is_undone** — `RegisterHotKey` outlasting
  `register()`'s wait (a loaded machine) used to leave an untracked thread holding the key in a
  message loop nothing could stop. Now the late success is unregistered, and the late thread can
  no longer overwrite `_thread_id` for a newer registration.

### Release metadata

#### tests/test_release_metadata.py — one version, every dependency pinned (R13)

Stdlib-only checks over `tools/release_meta.py`; no build, no network.

- **test_repository_release_metadata_is_consistent** — the real repository
  passes `release_meta.check()`: `frontend/package.json`, both root
  versions in `frontend/package-lock.json`, `installer.iss`'s
  `#define AppVersion` and any literal `vX.Y` UI chip agree with
  `pyproject.toml`, and every dependency declared in pyproject is pinned in
  `constraints.txt`. Why: before R13 the lockfile still said 3.0.0 and
  pyproject declared no dependencies at all, so CI and a developer machine
  could test different sets.
- **test_version_is_semver_and_four_part_for_the_exe_resource** — the
  version parses as MAJOR.MINOR.PATCH and maps to the four-part tuple the
  Windows `VS_FIXEDFILEINFO` resource needs (`aica.spec` uses it).
- **test_check_reports_version_drift[package.json|lockfile|installer]** —
  on a temporary copy, changing ONE place's version produces exactly one
  problem naming that file. Why: the check must fail loudly on drift, not
  just pass on the happy path.
- **test_ui_chip_may_abbreviate_but_not_disagree** — a `vX.Y` chip that is
  a prefix of the version passes; a chip with a different minor fails.
  Why: Settings hard-coded `v3.1`; a bump that forgets it must fail CI.
- **test_check_reports_an_unpinned_dependency** — removing the
  `websockets` pin from a copy of constraints.txt is reported. Why:
  declared-but-unpinned dependencies silently float.
- **test_build_info_records_revision_and_pins_without_secrets** — the
  `build_info.json` payload the spec bundles has the version, a revision,
  the pins, is JSON-serializable, and contains nothing key-like.
- **test_spec_and_installer_take_the_version_from_one_source** — static:
  `aica.spec` builds its version resource from `release_meta.read_version()`
  and bundles `build_info.json`; `installer.iss` uses `{#AppVersion}` for
  `AppVersion` and the output name, and stays `PrivilegesRequired=lowest`.

### Release-review fixes (independent review, 2026-09-22)

- tests/test_bridge.py **TestBoundedDelivery::test_a_reloaded_page_gets_deliveries_again_after_the_old_one_hung** —
  the abandoned-dispatch count is kept per page generation. A call against a crashed WebView2
  page may never return, so a global count ratcheted the pump shut for good after two crashes;
  the reloaded, healthy page then never got another event.
- tests/test_bridge.py **TestBoundedDelivery::test_a_permanently_hung_renderer_does_not_pile_up_threads**
  (changed) — polls for the held terminal event instead of sampling once at 0.5 s, which raced
  a dispatch attempt under a loaded suite.
- tests/test_app_wiring.py **TestWatchdogBootGate::test_a_watchdog_reload_advances_the_generation_exactly_once** —
  the watchdog advances the page generation before `load_url`, and the following `loaded` must
  not advance it again. A dispatch stamped in the load window runs on the new page, and a second
  bump made that page drop it, including terminal events.
- tests/test_app_wiring.py **TestGeometryPersistence::test_close_flushes_the_pending_debounced_save**
  (changed) — asserts the timer's `finished` flag (set by `cancel()` at once) instead of
  `is_alive()`, which raced the timer thread's exit.
- tests/test_bridge.py **TestStatusSnapshot::test_the_page_cannot_set_the_protection_verdict** —
  pywebview exposes every public `js_api` method to page JS; the verdict setter is private so
  only the Windows read-back can mark the window protected.
- tests/test_machine.py **TestAudioLifecycle::test_a_device_open_failure_after_an_early_stop_is_reported_not_no_speech** —
  no output device plus a Stop inside the open window reports "Could not open the system audio
  device", not "No speech detected".
- src/__tests__/app-races.test.tsx **an 'unknown' status (stalled core loop) waits again instead of cancelling** —
  refused-stop recovery treats `phase: "unknown"` as "cannot tell" and rechecks (bounded)
  instead of cancelling a session that may be mid-answer.