# AI briefings

Epoch 5 adds AI briefings: an optional, on-demand feature in the transfer planner, currently powered by Gemini using each user's own key. Regular transfer suggestions, estimates, comparisons and the decision journal work without a key. Scheduled email digests remain Epoch 6.

## Who supplies each key

| Key | Supplied by | Where it goes | Purpose |
| --- | --- | --- | --- |
| Gemini API key | Each individual user | Gemini settings, then encrypted in the database | Authorises that user's Google requests against their own quota/billing |
| Encryption keyring (`AI_CREDENTIAL_KEYS`) | The app operator | Deployment secrets, shared by web and worker | Encrypts/decrypts the user keys stored in the database |

The operator does not supply a shared Gemini key. There is no deployment-level Gemini API key or fallback provider account. The encryption keyring is generated locally using Fernet; it is not issued by Google and cannot authorise Gemini requests. Each briefing uses only its owner's saved Gemini key.

## What the model does

A verified-email manager connects a Gemini API key at `/accounts/gemini/`. The server checks access to the approved model using Google's model metadata endpoint before saving the key. This verifies that the key can see the model; it does not verify the manager's Google identity, billing account, remaining quota, or ability to complete a future generation.

The planner's “Prepare AI briefing” action queues one request. The worker recomputes the plan and stops if its inputs have changed. Gemini ranks up to three evidence items, always including the engine's transfer-or-hold recommendation. Evidence includes expected gain after hits, captain options, availability/minutes risks, allowance retained, forecast sensitivity, fixture blanks/doubles, and an anonymous rival comparison when available.

The model returns only evidence IDs from a supplied enum. The server rejects unknown IDs, duplicates, missing priorities, extra fields, truncated output and unsupported responses. Titles, explanations, names and figures displayed in the briefing come from the engine's evidence, never from free-form model output. Gemini cannot introduce a new transfer, price, injury claim or external link. It cannot execute transfers. This first integration prioritises evidence rather than offering a free-form chatbot or independent forecasts.

Google receives public player names and a small planning summary. Account email, manager/team/FPL identifiers, league names, rival identities, the full private financial-input object and raw player news are excluded. Availability flags are summarised rather than copying imported news; all supplied evidence is treated as untrusted data. The schema and server-side enum validation also constrain output if a player/source string contains instructions. The settings page explains disclosure, API costs and Google's data terms before the user connects. Connecting records consent; each generation requires an explicit request.

The approved model is `gemini-3.5-flash-lite`, through fixed HTTPS `generativelanguage.googleapis.com/v1beta/models/...` endpoints. Provider URLs, tools, alternate models, redirect destinations and arbitrary prompts are not user-configurable. Requests use the key in a header, never a query string. Ambient proxies and netrc credentials are disabled for this client. Response bodies are bounded at 64 KiB. Connect/read timeouts are 3/15 seconds; a 20-second elapsed-time check between streamed bytes additionally bounds a slowly progressing response (a stalled read still uses its read timeout). There are no automatic provider retries.

Primary references checked for this implementation:

- [Stable model and capabilities](https://ai.google.dev/gemini-api/docs/models/gemini-3.5-flash-lite)
- [GenerateContent REST API](https://ai.google.dev/api/generate-content)
- [Model metadata API](https://ai.google.dev/api/models)
- [Structured output](https://ai.google.dev/gemini-api/docs/structured-output)
- [Gemini thinking configuration](https://ai.google.dev/gemini-api/docs/thinking)
- [Google's data-handling terms](https://ai.google.dev/gemini-api/terms)
- [Gemini pricing](https://ai.google.dev/gemini-api/docs/pricing)
- [Fernet authenticated encryption](https://cryptography.io/en/latest/fernet/)

Google's terms distinguish unpaid/paid services and regional treatment. The app does not infer billing or promise that Google will not use a user's prompts. The connection screen links to those terms; users should review their account and provider billing limits.

## Limits and lifecycle

Limits are durable database reservations per manager, independent of key replacement/deletion:

- Six new briefings per UTC day, ten minutes between new requests.
- A 100,000-token daily reservation ceiling. Each run reserves 16,512 tokens: at most 16,000 serialized input bytes (a conservative text-token allowance) plus 512 maximum generated tokens, including reported thinking usage. Minimal thinking is requested.
- Ten connection attempts per UTC day, at least thirty seconds apart. Metadata validation does not generate text.

The app bounds its own calls and tokens, not spending elsewhere in the user's Google account. Prices and provider policies can change; configure billing/quota controls with Google too. Failed, cancelled and ambiguous requests keep their reservations to avoid accidental retries and untracked charges. Repeated requests for the same recent plan reuse existing work/results instead of spending again. Reported token usage is stored when available; a response exceeding the expected token limits is rejected.

Queued tasks contain a briefing UUID only. The worker decrypts the credential at execution, rechecks the plan, and claims the run once. A task retried after a crash cannot make a second request; the user may request a new briefing after the cooldown. Jobs older than ten minutes are skipped. Ready results expire after thirty minutes or at the deadline, whichever comes first. GET polling does not contact FPL or Gemini. An expired/changed plan's briefing is hidden rather than presented as current advice.

Replacement/deletion cancels pending and running results and clears their evidence/priorities. Deletion removes the credential row; neither models nor user interfaces expose the key or a decrypt API. Work already dispatched to Google cannot be recalled, but a result arriving after deletion/replacement is not published. Quota counters and cancelled task metadata remain to enforce limits. Expired evidence is not served; it remains in the database until the manager removes the connection or their account is deleted.

Provider errors and raw model output are not stored in task errors or application logs. User-facing failures are fixed strings. Credential views, provider operations, workers and rotation use a sensitive Sentry scope; matching events/transactions and provider HTTP breadcrumbs are dropped. Global Sentry request-body capture and local-variable capture are disabled. Password fields never repopulate a key; no credential model is registered with Django admin. Continue to run production with `DEBUG=False` and avoid custom middleware/proxies that record request bodies or authentication headers.

## Deployment

1. Install locked dependencies (including `cryptography`) and apply migration `0015_aibriefing_aicredential_aiusage`.
2. Generate a dedicated Fernet key in a secure operator session:

   ```sh
   uv run python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
   ```

3. Store this encryption key in the deployment secret manager, separately from the database and Django `SECRET_KEY`. Do not put a Gemini API key here. Supply the same versioned encryption keyring to the web and job roles:

   ```dotenv
   AI_CREDENTIAL_KEYS='{"v1":"<dedicated Fernet key>"}'
   AI_CREDENTIAL_ACTIVE_KEY=v1
   ```

   `.kamal/secrets` forwards these environment values; `config/deploy.yml` injects them into both roles. Empty/malformed configuration disables new connections/generations safely. There is no fallback to Django's secret and no automatically generated ephemeral production key.

4. Deploy the new static/templates and restart the existing `fpl_worker`. There is no second AI worker or scheduler to operate. Check `manage.py check` and `manage.py check_fpl_worker`.
5. With a verified test account, connect a restricted Gemini key, request a briefing, then replace/remove it and check that a new request no longer uses the old connection. The automated tests mock Google; no real user's key was used or charged during implementation.

No production secrets, Gemini key, deployment or live-provider billing verification are included in this epoch. Operators must configure the external keyring before users can connect.

## Rotation, backup and recovery

Keep the database backup and encryption keyring in separate access-controlled stores. Back up every key version needed by encrypted rows or retained database snapshots. A database backup alone cannot restore credentials; losing all applicable encryption keys requires users to reconnect. Restoring a snapshot also restores its historical credential rows: apply deletion/retention policies and do not treat restoration as consent to dispatch old queued work. The ten-minute queue age check prevents old briefing jobs from running.

To rotate encryption keys:

1. Add `v2` to the keyring while keeping `v1`. Roll out the shared keyring to every web/job instance before changing the active version.
2. Set the active version to `v2` on all instances, retaining both keys. Restart them. New connections now use `v2`.
3. Run `uv run python manage.py rotate_ai_credentials`. This re-encrypts rows transactionally, preserving credential revisions and pending valid work. A failure rolls back the rotation; command output contains counts only.
4. Verify all rows use `v2` by counting versions, without displaying ciphertext or decrypted keys. Retire `v1` from live instances only after old instances are stopped. Keep it in the restricted backup key archive until every snapshot that requires it has expired.

Do not publish keys in shell history, CI output, support tickets, dumps or application logs. If a provider key is compromised, revoke it at Google and reconnect; rotating the app's encryption key does not revoke a provider API key. Deleting a live connection cannot erase historical encrypted backups or data already sent to Google; enforce backup retention and provider policies separately.

## Verification

Tests cover verified-email/key eligibility, encryption tampering and owner binding, masked/password forms, CSRF, cross-user isolation, replacement/deletion, queue contents, reservations/cooldowns, outdated plans, duplicated jobs, in-flight revocation, failure redaction, bounded provider transport, grounded output validation, telemetry filtering, and transactional key rotation. They use mock provider responses; browser interaction and real Google generation still require deployment smoke checks.
