# FPL account timezone research

Checked 2026-11-17 against first-party FPL responses and published frontend source. Requests were unauthenticated; only field names/types were recorded. No credentials, environment files, or account-specific field values were retained.

## Finding

The public FPL entry data available to RivalSpy does **not** provide a usable IANA timezone. Use the browser's configured timezone as the initial preference, validate it on the server, and let the user change it. Do not derive a timezone from an FPL country/region.

## Evidence

| First-party source | Observed schema | Consequence |
| --- | --- | --- |
| [`GET /api/entry/1/`](https://fantasy.premierleague.com/api/entry/1/) | `player_region_id`: integer; `player_region_name`, `player_region_iso_code_short`, `player_region_iso_code_long`: strings. No `timezone`, `time_zone`, or `timeZone` field anywhere in the response. | These identify an FPL region, not a user's time zone. |
| [`GET /api/regions/`](https://fantasy.premierleague.com/api/regions/) | Each object has `id`: integer, `name`: string, `code`: integer, `iso_code_short`: string, `iso_code_long`: string. No timezone field. | Looking up the entry's region adds no timezone information. |
| [`GET /api/bootstrap-static/`](https://fantasy.premierleague.com/api/bootstrap-static/) | `game_settings.timezone`: string, currently `UTC`. | This belongs to the global game settings, not an individual account; it cannot establish the user's timezone. |
| [`GET /api/me/`](https://fantasy.premierleague.com/api/me/) | An unauthenticated request returned HTTP 200 with `player`: null and `watched`: array. | This does not expose the authenticated account schema. It is not evidence that authenticated accounts can never contain a timezone. |

The official [frontend source map](https://fantasy.premierleague.com/assets/index-CMoSnM_-.js.map), referenced by the [published JavaScript](https://fantasy.premierleague.com/assets/index-CMoSnM_-.js), shows `EntryTitle.tsx` using `player_region_id` to render a country flag. This supports treating the region fields as country/region data. No public first-party account API documentation establishing a per-user IANA timezone was found. The frontend assets are versioned and their URLs may change.

RivalSpy's existing [`FplApiService.fetch_team_basic_info`](../rivals/services/fpl_api_service.py) reads the public `/api/entry/{id}/` endpoint using plain requests; it does not authenticate to FPL accounts. Therefore even an unverified field in an authenticated FPL response would not supply a default through the current integration.

## Defaulting recommendation

Use `Intl.DateTimeFormat().resolvedOptions().timeZone` in the browser when the account has no timezone preference. The [ECMA-402 specification](https://tc39.es/ecma402/#sec-intl.datetimeformat.prototype.resolvedoptions) defines this as the host environment's timezone when none was supplied. Validate the returned value against the server's supported IANA identifiers; fall back to `UTC` if detection or validation fails. Keep an explicit user selection authoritative.

This detects a device/browser setting, not a verified residence or permanent preference. It should remain visible and editable. Country data cannot choose a unique zone for countries with multiple zones; even a single-zone FPL region would not prove the zone the user wants. IANA's [time zone database](https://www.iana.org/time-zones) models representative locations and their changing offset/daylight-saving rules, which is why a named zone is preferable to a country or fixed UTC offset for future dates.

The conclusion is limited to the observed public API, the unauthenticated `/me/` result, and the current integration. It does not claim exhaustive knowledge of private Premier League account APIs. There is no verified first-party per-user timezone source here on which to base automatic account defaults.

## RivalSpy implementation

- `accounts.User.timezone` stores a validated named timezone. An empty value uses the application's UTC fallback until the browser supplies a default or the user saves a choice in **Profile & settings → Timezone**. Migration `accounts.0008_user_timezone` adds the field without guessing existing users' locations.
- Authenticated full-page GETs with no saved preference load `timezone-default.js`. It sends the browser's zone to a CSRF-protected POST endpoint. The database update only matches an empty preference, so a late request or another device cannot replace an existing selection, including an explicit `UTC` choice. Detection failure leaves the profile form available.
- Request middleware activates the saved zone for template dates, including HTMX fragments, and restores the previous context after normal responses or errors. Concurrent async requests have separate contexts. Shared analysis snapshots store timezone-aware timestamps; display filters localize them when rendered.
- Briefing workers read the recipient's latest preference and render both HTML and plain text in that zone. The same preference applies to the browser email preview. Named zones account for daylight saving at the timestamp being displayed, including future deadlines.
- Timezone selection changes presentation. Stored deadlines, comparisons against the current time, and the absolute scheduled delivery time remain unchanged. Existing explicit UTC fields in machine-readable FPL data retain their meaning.
- `tzdata` is a direct dependency so container deployments have timezone rules even without an operating-system timezone database. Deployment must run the normal migrations and static-file collection.
