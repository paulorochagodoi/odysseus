# Fork Notes

Fixes and changes carried by the `paulorochagodoi/odysseus` fork on top of
upstream `odysseus-dev/odysseus` (`dev`). Each section names the symptom, the
root cause, and the regression test, so it can be dropped once upstream has
an equivalent fix.

## Codex email API returned 401 for valid tokens

**Symptom.** `GET /api/codex/emails?limit=3` with a valid `email:read` bearer
token (`ody_...`) returned `401 {"error": "Invalid API token"}` and logged
`API token auth error`. With the same token, `/api/codex/capabilities` and
`/api/codex/todos` returned 200.

**Root cause.** There were two bugs:

1. `routes/codex_routes.py` calls the email route handlers directly as Python
   functions. `list_emails` did not pass `date_from`, `date_to`,
   `cached_only` or `refresh`, so they kept their FastAPI defaults:
   `Query(None)`/`Query(0)` objects, which are truthy. The handler then called
   `datetime.strptime(<Query>, ...)`, which raised `TypeError`. `read_email`
   also leaked `full` as a `Query(False)` object; that was harmless only by
   accident.
2. In `app.py`, `AuthMiddleware` wrapped `await call_next(request)` in the
   same `try/except Exception` as bearer-token verification. Any downstream
   route exception was therefore reported as `401 Invalid API token`.

**Fix.**

- Codex routes call every borrowed handler through `_call_handler()`. It
  replaces each unpassed FastAPI marker default with the real default value,
  and raises `TypeError` for an unresolved `Depends()` or a required
  parameter. Email list/read now also pass every parameter explicitly.
- `AuthMiddleware` returns 401 only when token verification fails. Route
  errors after a successful auth are logged with the path and token id, then
  re-raised so normal error handling returns a 500.

**Tests.** `tests/test_codex_email_api_token.py`.

## Scheduled built-in tasks always failed with "no matching server request authority"

**Symptom.** Built-in housekeeping tasks (for example `tidy_sessions`) failed
on every scheduled run with `Scheduled action has no matching server request
authority.` The scheduler still logged each failed run as
`Task '...' completed`, so the logs did not show the failure.

**Root cause.** Built-in rows seeded before scheduled tasks stored a sealed
request authority have `request_authority_json = NULL`. `ensure_defaults`
only sealed newly seeded rows, so these legacy rows never got an authority
and were denied on every run. Separately, `_execute_task_locked` logged
"completed" for every run that reached the end, including `status="error"`
runs.

**Fix.**

- `ensure_defaults` (`src/task_scheduler.py`) reseals any kept built-in row
  that has no prompt and no permitting authority, the same way a fresh seed
  is sealed. Such a row cannot be told apart from a fresh seed. A built-in row
  that has a prompt keeps unknown provenance and stays denied.
- Error runs are logged as a warning, `Task '...' failed (run ...): <error>`.

**Tests.** `tests/test_request_authority.py::test_server_seeded_defaults_have_authority_and_only_promptless_legacy_rows_gain_it`,
`tests/test_task_scheduler_failure_logging.py`.

## ChatGPT Subscription rejected requests with "Unsupported parameter: temperature"

**Symptom.** Requests to ChatGPT Subscription (Codex backend) for models
outside the reasoning-model prefix list, such as `gpt-6.1-sol`, failed with
HTTP 400 `Unsupported parameter: temperature`.

**Root cause.** `_build_chatgpt_responses_payload` (`src/llm_core.py`) sent
`temperature` unless `_restricts_temperature(model)` matched a static list of
model-name prefixes. The Codex backend rejects `temperature` for every model,
so any model name missing from the list failed.

**Fix.** The payload never includes `temperature`, and `temperature` was
removed from `CHATGPT_ALLOWED_PAYLOAD_KEYS`. The backend uses its own default.

**Tests.** `tests/test_llm_core_temperature_reasoning.py::test_chatgpt_subscription_payload_never_sends_temperature`,
`tests/test_chatgpt_zero_native_tools.py`.

## AI reply refused a draft that only held the signature

**Symptom.** In a new reply draft, AI Reply showed "Reply already has text"
even though the user had not typed anything. The only text above the quote
was the account signature that the composer adds. When AI Reply did run, the
generated text replaced the signature.

**Root cause.** The AI-reply gate in `static/js/document.js` counted all text
above the quoted original as user-typed, including the `-- ` signature block.
The generated reply also replaced that whole section of the body, so the
signature was lost.

**Fix.**

- New `splitSignature` / `composeReplyWithSignature` helpers in
  `static/js/emailLibrary/signature.js`. The split accepts a delimiter line
  without its trailing space, because the rich editor drops it.
- `_emailAiReplyDraftState` counts only text above the signature delimiter as
  authored text.
- Generated replies are assembled as reply, then the draft's signature once,
  then the quote. If the model echoes the signature, its copy is removed.
- `replaceEmailReplyBody` uses the same path.
- A draft that holds only a signature no longer sends that signature to the
  backend as the "original" email.

**Tests.** `tests/test_email_ai_reply_signature_js.py` runs the real
functions in node; the tests are skipped when `node` is not on `PATH`.

## AI reply showed "Model returned analysis" instead of the provider error

**Symptom.** When ChatGPT Subscription rejected the request (for example a
401 for expired credentials), AI Reply reported `Model returned analysis or
an incomplete reply, not a finished email` instead of the actual error.

**Root cause.** ChatGPT Subscription reports HTTP failures as an
`event: error` SSE frame whose data is `{"status": 401, "text": "..."}`, with
no `error` key. `stream_reply` (`src/email_reply_stream.py`) only checked
`event["error"]`, so it ignored the failure and finished with an empty reply.
The empty reply then failed the reply-marker check.

**Fix.** `_provider_error` treats any of these as a failure: an
`event: error` frame, an `error` value (string or dict), or `status >= 400`.
It raises the provider's message. An empty stream now reports
`Model returned an empty reply`. Analysis text without
`<<<REPLY>>>`/`<<<END>>>` markers is still rejected.

**Tests.** `tests/test_email_reply_stream.py`.

## AI reply got 401 from ChatGPT Subscription while chat worked

**Symptom.** In a chat session that uses ChatGPT Subscription, chat worked
but AI Reply's request to `chatgpt.com/backend-api/codex` got HTTP 401.

**Root cause.** The AI-reply handler in `routes/email/email_routes.py` read
the bearer from the session's raw `sessions.headers` column. Chat never
stores the ChatGPT Subscription bearer there, so the column is either empty
or holds a stale token.

**Fix.** The handler now resolves auth through
`routes.chat_helpers.resolve_session_auth`, the resolver chat uses, on a
detached copy of the session so the ORM row is not changed. This takes the
bearer from `resolve_endpoint_runtime`, which refreshes an expiring OAuth
token ahead of time, and builds the headers with `build_headers` /
`chatgpt_headers`.

**Tests.** `tests/test_email_ai_reply_chatgpt_auth.py`.
