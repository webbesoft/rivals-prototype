# AI briefings: deadline delivery

AI briefings are the deadline emails. `/accounts/gemini/` is the single settings page for the manager's Gemini connection, email opt-in, timing, pause controls, previews, test delivery and recent briefings. The transfer planner can preview the same evidence priorities. Connecting a key alone does not subscribe an account to email.

## What arrives

The email starts with the gameweek and actual FPL deadline, then the planner's **Fixtures and playing-time risks** visual. It shows the unchanged-squad baseline in formation, captain/vice, all four bench players, opponents with H/A, blanks/doubles, individual estimates and visible risk flags. A compact table covers the remaining planning window. The HTML uses inline styles and text/table layouts; no images, scripts, hover explanations or gameweek controls are needed. The plain-text alternative contains the same names, fixtures, estimates and advice.

After the visual, Gemini selects priorities from the planner's evidence, using only the recipient's saved key. It uses the same credential revisions, six-per-day/ten-minute reservations, provider limits and evidence-ID validation as planner previews. No extra model, shared provider key or free-form AI claims are introduced. A digest links one reserved AI run before execution; restarting generation reuses it rather than repeating a failed or ambiguous paid request. A recent matching planner briefing can be reused.

If the key is missing, revoked, unavailable or rate-limited, or Google fails, the email still includes the fresh visual and structured transfer-versus-hold and captain advice. It labels the AI priorities unavailable. Removing/replacing a key prevents the old priorities being included at submission. Already submitted provider requests/emails cannot be recalled. Keys, ciphertext and raw provider errors never appear in email or task arguments.

## Consent and timing

- Email delivery starts disabled. Only verified, active accounts with an active linked FPL team can enable it or request a preview/test.
- Default lead time is four hours. Choices are 1, 2, 4, 6, 12 or 24 hours before the timezone-aware FPL deadline, rather than a weekday or host-local time.
- Pausing retains the preference and stops pending scheduled deliveries. Turning delivery off unsubscribes; re-enabling records new consent and rotates the unsubscribe token.
- Email unsubscribe links display a confirmation on GET and change state only on CSRF-protected POST. No login is required for a valid signed link. An old link cannot undo a later opt-in.
- Preview and test requests are explicit and do not enable recurring delivery. Each is limited to three per rolling day and one per two minutes; Gemini's shared limits also apply. Tests go only to the current verified account email and do not consume the scheduled delivery for that event.

The existing minute dispatcher maintains one scheduled record per user, season and event. Changed FPL deadlines or lead times reschedule unsent work and invalidate its old generation. Sent and ambiguous deliveries are never resent for that event. A missed send window has a thirty-minute grace period, always bounded by the actual deadline. After that window, on expired deadlines, or when an account/team changes, the worker skips the obsolete email. Opting in after that event's window takes effect at the next event. Pausing/unsubscribing cancels that event's pending digest; resuming does not resurrect it unless its schedule changes.

## Refresh, retries and duplicate prevention

Generation refreshes player/fixture snapshots (bypassing their shared caches), public profile, results/history, transfers and the published source-week picks before running the existing decision engine. It retains the manager's planner corrections and preferences. Public data can still lag unpublished pre-deadline moves; bank and selling prices remain estimates. Rival scenarios use the existing subscribed-league snapshots, carry their timestamp, and do not claim fresh unpublished rival actions.

If fresh inputs cannot be obtained, no stale email is sent. The account's recent briefing record explains the failure and points to the planner. Generation tries up to three times with two/four-minute backoff. Generation claims include a version and ten-minute lease so a superseded/recovered job cannot publish over newer work.

Delivery separately claims the generated record, checks the FPL event's season/deadline again, and requires advice prepared within ten minutes. After opening the email connection, it rechecks account/team/consent/current recipient and key revision before recording `sending`. Once submission starts, success becomes `sent`; exceptions/zero sends become `ambiguous`, with no automatic retry. An abandoned `sending` claim also becomes ambiguous. Only failures before message submission are safe to retry, up to three times, with the same bounded backoff and send window. SMTP cannot provide exactly-once delivery; the deliberate tradeoff is to skip uncertain resends. Stable Message-ID values supplement the database claims but are not assumed to deduplicate mail at the receiver.

The two durable state fields distinguish generation (`pending`, `queued`, `generating`, `ready`, `failed`, `skipped`) from delivery (`pending`, `connecting`, `sending`, `sent`, `ambiguous`, `skipped`). Preview records never submit email. Polling reads stored status only; it does not call FPL, Google or SMTP.

## Deployment and operations

1. Apply migrations `0017_digestsubscription_deadlinedigest` and `0018` (usage audit) and deploy the templates/application code.
2. Configure the existing `EMAIL_*`, `DEFAULT_FROM_EMAIL` and externally reachable `SITE_URL` settings. SMTP connections use a fifteen-second timeout. The implementation uses the repository's existing Django email API; its Django 2028 deprecation warnings also apply here.
3. Supply the same external encryption keyring on web/job roles as described in [AI briefing security and operations](gemini-briefings.md). Each user supplies their own Gemini key in settings. No operator/provider key is added.
4. Restart the existing `manage.py fpl_worker`. It dispatches digests once per minute, refreshes FPL inputs and processes generation/delivery jobs. No separate scheduler or worker is needed. `manage.py check_fpl_worker` checks the worker heartbeat.
5. With a verified test account, inspect an email preview on mobile/desktop and with images blocked, then explicitly request a test using a controlled SMTP destination and Gemini key. Confirm unsubscribe, pause and deadline rescheduling. Use a staging environment for the final send/provider checks; deployment must not subscribe existing users automatically.

Automated coverage includes scheduling/rollover, changed deadlines, consent/CSRF, access isolation, fresh inputs, bounded retries, duplicate jobs, expired leases, provider failures/reservations/revocation, ambiguous SMTP outcomes, multipart content, and execution through the existing task worker. Real Google charges, SMTP inbox delivery, production deployment and cross-client visual verification are not included in local automated validation.

Usage investigations and the generation emergency stop are documented under [AI usage audit operations](gemini-briefings.md#usage-audit-and-billing-investigations). Scheduled, preview and test triggers all share that audit and its spending controls.
