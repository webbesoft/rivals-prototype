# Passwordless authentication rollout

RivalSpy supports Google sign-in and single-use email links behind `PASSWORDLESS_AUTH_ENABLED`. The flag defaults to `False` so the existing password routes remain available during staging. Enable it to switch browser login/signup to passwordless and reject password-based API login/signup requests.

## Configuration

Set `PASSWORDLESS_AUTH_ENABLED=True` in the target environment. Email links use the existing SMTP settings and `DEFAULT_FROM_EMAIL`. The link origin always comes from `SITE_URL`; set it to the canonical HTTPS address in production. Local development can use the console email backend.

Google is optional. Set both `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` to display the Google button. Configure a Google OAuth web application with this exact callback:

```text
https://<your-host>/accounts/google/login/callback/
```

Register separate localhost and staging callbacks as needed. Google uses the authorization code flow with S256 PKCE and requests profile/email access. Provider tokens are not retained. The separate MCP OAuth configuration continues to control assistant access.

Kamal forwards all three new environment variables through `config/deploy.yml` and `.kamal/secrets`. Keep credentials in the environment, rather than committed files. See the [allauth Google setup](https://docs.allauth.org/en/latest/socialaccount/providers/google.html) for provider registration.

## Account and FPL behavior

Email requests do not create accounts. A successful email challenge creates or reuses the case-insensitive email identity, verifies it and removes its usable password. Google creates new accounts only with a verified email. If Google matches an existing local email, first sign in by email link and connect Google from **Profile → Manage sign-in methods**. Google connections must use the same verified email. Disconnecting Google leaves email-link access available.

Accounts without a matching FPL ID and linked team must finish `/accounts/onboarding/fpl/` before reaching dashboard pages, fragments, actions or MCP consent. The API enforces the same rule after session/token authentication. The onboarding page validates the FPL entry, publishes the team link atomically, reconciles memberships and queues refresh work. Full league/stat imports do not block entry to the dashboard. Validation or initialization failure leaves setup incomplete; failed team changes preserve the previous team.

Users can close the browser and return to complete setup. Existing users with a matching FPL ID/team link skip setup. Their existing team, memberships and tracked rivals remain attached to the same account. Staff returning to `/admin/` may sign in without an FPL team; dashboard access still requires setup.

## Email link security and operations

Links expire after ten minutes. Only their SHA-256 digest is stored. Opening a link stores the challenge in the receiving browser session and redirects to a clean confirmation URL. A CSRF-protected confirmation POST atomically consumes it. A scanner opening the link does not consume it; recipients can open it on another device. The confirmation page names the account being signed into.

Email requests share IP/email limits across browser and API entry points. Responses do not reveal whether the email exists or belongs to a disabled account. Delivery failures remove the challenge and record a generic error without SMTP payloads or addresses. Expired challenges receive bounded cleanup on subsequent requests.

Authentication responses are not cacheable. The PWA worker already passes authentication pages through to the network. Sentry drops events and URL breadcrumbs for magic-link, Google callback and API auth routes. Configure reverse-proxy/access-log rules to omit or redact `/accounts/magic/<secret>/` paths and Google callback queries; these URLs contain temporary credentials. Console email output is suitable only for local development.

Authentication forms use `Referrer-Policy: same-origin`. Setting `no-referrer` on these pages makes browser form submissions send `Origin: null`, which Django rejects even with a valid CSRF token. The secret-bearing email-link redirect retains `no-referrer`; its clean confirmation page uses `same-origin`. Keep CSRF checks enabled and do not add `null` to `CSRF_TRUSTED_ORIGINS`.

## Migration and cutover

1. Back up the database and run `uv sync --locked` and `uv run python manage.py migrate` with the rollout flag still off. The migration normalizes emails and enforces case-insensitive uniqueness. It stops with affected user IDs if there are empty addresses or collisions; resolve ownership before retrying. Existing verification flags are copied to allauth email records. No migration erases passwords.
2. Enable the flag in staging. Test new and returning accounts, SMTP inbox delivery, real Google consent/callbacks, staff access, FPL validation failures, interrupted onboarding and an MCP connection initiated before login. Automated Google tests mock the provider response; real provider registration and email delivery require this staging check.
3. Migrate any API consumers that depend on password-issued DRF tokens. Enable the flag in production after those checks. The changed session backend requires existing browser sessions to sign in again.
4. After successful cutover, preview remaining passwords with `uv run python manage.py retire_passwords`. Run `uv run python manage.py retire_passwords --apply` to invalidate them permanently, including staff passwords. This also invalidates sessions whose authentication hash changes. Confirm staff email access before this step.

Turning the flag off restores legacy routes while diagnosing a rollout problem. Active accounts with unusable passwords, including email-link and Google accounts, can request a recovery email through **Forgot your password?** and set a password using the single-use reset link. The rollback form explicitly includes these users; Django's default reset form excludes them. Disabled accounts remain ineligible. Re-enabling passwordless restores email-link access. Password retirement is a separate operational action and is not run by migrations or deployment.

## Account deletion and incomplete onboarding

Users can choose **Delete my account** from their profile or the FPL onboarding
page. The confirmation page requires their account email and a CSRF-protected
POST. Deletion signs them out, removes private plans, tracking subscriptions,
briefing history and AI credentials, and revokes API tokens, social sign-ins and
MCP OAuth grants. Outstanding email sign-in links and legacy account jobs are
also removed. Other browser sessions lose access on their next request.
Shared public FPL teams, leagues and history remain available to other users;
usage audit metadata is retained with its user reference removed. A later
sign-up creates a fresh account.

New accounts have seven days from creation to connect an FPL team. The onboarding
page shows the deletion deadline. Signing in again does not extend it; successful
FPL initialization permanently clears it, including through the legacy signup
flow. Staff and superuser accounts are excluded from automatic cleanup.

Apply migration `0009_user_onboarding_expires_at` before restarting the web and
worker processes. Existing incomplete accounts receive seven days from migration
time, while existing accounts with FPL setup are preserved. Cleanup runs in
batches of up to 100 accounts on each `fpl_worker` dispatcher tick, including when
upstream FPL data is unavailable. No additional scheduler is required. Expired
accounts are deleted when the worker next processes them; keep the worker running.

For an operational preview or a manual batch:

```sh
uv run python manage.py cleanup_incomplete_accounts --dry-run
uv run python manage.py cleanup_incomplete_accounts --limit 100
```

## API contract

With passwordless enabled, `POST /api/auth/login/` and `/api/auth/signup/` accept `{ "email": "manager@example.com", "next": "/dashboard/" }` and return `202` with generic delivery guidance. Any password field returns `410` with `code: password_login_retired`. Completing the emailed link establishes a browser session; these endpoints do not issue bearer tokens.

Existing DRF bearer tokens remain supported, subject to verification and FPL gating. `/api/me/` returns `onboarding_required` for a verified account, including one without an FPL team. Gated endpoints return `403` with `code: onboarding_required` and `onboarding_url`. `POST /api/onboarding/fpl/` accepts `fpl_id` and publishes setup for a verified user; session-authenticated POST requests require CSRF protection. Native-client token issuance is outside this rollout; MCP continues to use its existing scoped OAuth grants.

## Verification

```sh
uv run python manage.py check
uv run python manage.py makemigrations --check --dry-run
uv run python -m pytest tests/unit/test_passwordless.py
```

The new tests enable the rollout flag explicitly. Existing auth tests continue to cover the flag-off path. Run the full release checks in `docs/deployment.md` before deployment.
