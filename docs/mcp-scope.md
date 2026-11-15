# Read only MCP scope for Rivals

Proposed scope, 15 November 2026. Build one hosted MCP server that lets a manager connect their Rivals account to an assistant and ask about their FPL team, mini leagues, tracked rivals and upcoming fixtures. Reuse this Django application's published data and calculations. The assistant supplies the conversation and explanation; Rivals supplies bounded, attributable facts.

The factual release is implemented with local automated verification: ten tools, shared published reads, OAuth linking/revocation and the MCP tab with provider guides. See [implementation and operations](mcp.md) for exact limits and commands. Production deployment, live provider flows and a nontechnical walkthrough remain release validation; the follow-on forecast tools remain separate work.

## Product boundary

The first release should answer questions such as:

- How did my team do this gameweek, and how has my rank changed?
- Who is above me in my mini league, and how many points separate us?
- Which players and captains do my tracked rivals have in common with me?
- What transfers did a rival make in the last three gameweeks?
- Which of my players have difficult fixtures, blanks or doubles coming up?
- When is the next deadline?

Connect to Rivals, rather than asking users for FPL credentials or an AI provider key. Return the last published squad with its gameweek: public FPL data cannot reveal a manager's private, pre-deadline lineup or transfers. Unknown values stay unknown.

Exclude tracking changes, league imports, sync commands, saved plans, preference edits, account changes, emails and transfer execution. For MCP calls, read-only applies to domain data and refresh scheduling. OAuth grants, token rotation, rate counters and minimal operational logs necessarily have their own writes.

## Agreed read and refresh design

Use a dedicated MCP interface backed by shared queries that only read published data. Preserve the browser's existing bounded refresh-on-read behavior. Extract reusable queries as needed for MCP; a general refactor of browser GET requests is outside this release.

Browser handlers can combine the shared queries with explicit refresh scheduling. MCP handlers use the shared queries alone. Refresh decisions belong in browser orchestration and the refresh module, outside the shared read queries. Both interfaces use the same publications and worker; query and calculation logic should be shared as it is extracted.

This preserves automatic recovery and freshness on browser visits while giving MCP predictable reads. The cost is maintaining two refresh policies and accepting that an MCP answer can contain stale or unavailable data that a browser visit would cause to refresh. Less-used squads and league pages may remain unavailable until browser demand requests them. Measure this in the beta and improve worker coverage if needed; adding MCP-triggered refreshes would be a separate design decision.

## Client compatibility

Target a public HTTPS endpoint such as `https://<rivals-host>/mcp`. Client availability and workspace policy can restrict who can connect; protocol support alone does not establish a successful connection.

| Client | Proposed support and current constraints |
| --- | --- |
| ChatGPT | Custom read/fetch servers are available on Pro; full MCP support is documented for Business and Enterprise/Edu. Workspace admins may control availability. Support OAuth and stable tool schemas; these domain tools do not universally require a `search`/`fetch` wrapper. [Connection guide](https://developers.openai.com/api/docs/guides/custom-mcp-server), [availability](https://help.openai.com/en/articles/12584461-connecting-custom-mcp-servers-in-chatgpt) |
| Claude | Hosted custom connectors support remote MCP and OAuth. Prefer Streamable HTTP; the endpoint must be reachable from Claude's cloud. Verify setup on the intended plan/workspace. [Build guide](https://claude.com/docs/connectors/building/index.md), [connection setup](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp) |
| Gemini app | Custom MCP connections exist, currently restricted to US users aged 18+, personal accounts, English and Keep Activity enabled. Add through the web app, then use on web/mobile. [Availability and setup](https://support.google.com/gemini/answer/17209137) |
| Gemini CLI | A separate supported client target, with remote HTTP/OAuth configuration. CLI support does not remove consumer app eligibility restrictions. [CLI documentation](https://geminicli.com/docs/tools/mcp-server/) |

Gemini's Connected Apps Platform currently requires Streamable HTTP, supports protocol versions through `2025-11-25`, and does not support `structuredContent`. JSON text results are therefore required. Keep tool names/schemas stable across registered snapshots; version breaking changes and document client refresh/reconnection. [Server requirements](https://developers.google.com/connected-apps/mcp/server-optimization)

Use dynamic client registration or configured client credentials for Gemini and prove the flow in the real app; do not depend solely on client ID metadata documents. [Gemini OAuth guidance](https://developers.google.com/connected-apps/mcp/oauth)

## What the project already provides

| Capability | Existing implementation | MCP work |
| --- | --- | --- |
| Connected manager and own team | `accounts/models.py`, `api/views.py` | Minimal identity response without email or account internals |
| Accessible mini leagues | `rivals/services/access_service.py` | Reuse active membership rules |
| Standings and rank history | `MiniLeagueTeam`, `LeagueRankSnapshot`, `LeagueStandingsPage` in `rivals/models.py` | Normalize full imports and partial published pages |
| Tracked rivals | `TrackedTeam` | Account-scoped discovery and name search |
| Team history and transfers | `GameweekData`, `SeasonHistory`, `Transfer` | Season filters, bounded pagination and explicit missing fields |
| Published squads | `SquadHistory`, `FplSnapshot` | Resolve latest available gameweek and expose final/provisional points |
| Fixtures, players and deadlines | `FplSnapshot`, `GameweekContextService` | New structured fixture query; distinguish club, player and FPL entry IDs |
| Comparisons and forecasts | `TeamAnalysisService`, `DecisionEngine` | Reuse verified calculations through a side-effect-free read adapter |
| Refresh infrastructure | `SyncState`, `fpl_worker` | Keep worker ownership of ingestion |

The REST API is useful reference material, but should not be wrapped route for route. Its team detail/history/transfer/squad handlers currently check verified authentication without restricting queries to the connected user's team graph. `large_league_context()` calls `requested_page()`, which can create pages and queue refreshes. Even `FplApiService(snapshot_only=True)` requests refresh work on a cache miss. These behaviors need a separate read path for the MCP contract. See [API handlers](../api/views.py), [league summaries](../rivals/services/league_summary_service.py) and [snapshot reads](../rivals/services/fpl_api_service.py).

## Proposed first release tools

Use ten explicit tools. Avoid a generic SQL, URL-fetching or natural-language query endpoint. Tool descriptions should explain identifier types, defaults, access limits and data freshness.

| Tool | Inputs | Returns |
| --- | --- | --- |
| `get_my_context` | None | Own team reference, season, scoring gameweek, next decision gameweek/deadline and publication status |
| `list_my_leagues` | Optional name query and cursor | Active accessible leagues, scoring type, own rank where published, summary/full coverage |
| `get_league_standings` | League reference; section `leaders` or `near_me`; optional cursor | Published rows, own rank, points gaps, page coverage and freshness |
| `list_my_rivals` | Optional name query and cursor | Active tracked teams and stable references |
| `get_team_summary` | Team reference, default `me` | Team/manager display names, points, rank, value and bank where known, each with source context |
| `get_team_squad` | Team reference, default `me`; optional season/gameweek | Published XI, bench, captain, vice captain, multipliers and player references; actual source gameweek |
| `get_team_history` | Team reference; optional season and gameweek range; cursor | Points, rank, hits and chips from recorded gameweeks |
| `get_team_transfers` | Team reference; optional season/gameweek range; cursor | Recorded player moves, dates and known costs; unknown costs remain null |
| `compare_teams` | Two accessible team references; optional league reference and gameweek | Comparable published squads, overlap, unique players, captain differences, points/rank and league gap where available |
| `get_upcoming_fixtures` | Exactly one of team reference, club references or player references; horizon 1–6 gameweeks | Opponents, home/away, kickoff UTC, FPL difficulty, blanks/doubles, availability notes and unscheduled fixtures separately |

Team fixture queries mean fixtures for the players in that team's published squad. Resolve player and club names from the same season's bootstrap data. Player availability is FPL's report, not a medical conclusion or predicted return date.

Discovery tools return stable references that later tools accept. Use a documented namespace, for example `entry:<fpl_id>`, `league:<fpl_id>` and `player:<season>:<fpl_id>`; resolve them server-side and enforce access on every call. Echo season/gameweek in results. Never interchange internal Django IDs, FPL entries, clubs and players.

Name search operates only inside the caller's accessible set. Ambiguous names return candidates. Start history queries in the current published season; accept older seasons only where stored data are explicitly classified. A current team summary must not masquerade as an archived season's profile.

Comparisons use a common published gameweek. If either squad is missing, return a partial comparison instead of silently comparing different weeks. League gap means the difference between comparable published totals, not a prediction of finishing position. Treat head-to-head league match results as unsupported until their ingestion is verified; individual squad comparison remains available.

## Access and account linking

Recommended policy: verified active Rivals accounts can read their own team, active tracked rivals and entries present in current published standings of an actively accessible league. A published standings row can be visible even when its full team has never been imported; return detail unavailable without importing it. Recheck membership/tracking on every request and isolate all user-specific caches. Do not let a supplied entry ID grant global access to the database.

Private planning corrections and journal records belong only to their owner, even when two accounts select the same FPL team. Exclude them from the initial tools. Exclude email, credentials, encrypted keys, AI usage receipts, session identifiers and administrative metadata from all responses. Return the same unavailable/not-found result for inaccessible and nonexistent object references.

Use OAuth authorization code flow with PKCE and a single initial `rivals:read` scope. A user adds the MCP URL in their client, signs in on Rivals, sees the named client and requested data, and consents. Provide a Rivals connections screen listing grants, creation/last-use dates and revocation. Revocation stops future reads; it cannot recall data already sent into a chat.

Implement protected resource metadata, authorization server discovery, resource/audience-bound access tokens, short access-token lifetimes, refresh-token rotation and revocation. Validate issuer, audience, expiry and scope before creating the request principal. Support preregistered clients and dynamic registration for the targeted manual connection flows, with exact redirect matching, PKCE and registration limits. Use a maintained OAuth implementation; the existing broad DRF login token is not the MCP credential. These choices follow the [MCP authorization specification](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization).

## MCP tab in AI settings

Add an **MCP** tab within AI settings, alongside **AI briefings**, which retains the existing Gemini key and briefing delivery controls. The current settings implementation is `accounts/ai_views.py` and `templates/accounts/gemini.html`; extend that settings area so both features are easy to find. Put connection management in this tab rather than a separate settings destination.

Open with plain-language copy: “Ask your AI assistant about your Rivals team, leagues and rivals. Connect once, then ask questions in ChatGPT, Claude or Gemini.” Explain MCP once as “the connection that lets your assistant read your Rivals data.” State that the connection can read data but cannot change teams, make transfers or edit settings. A Gemini key for Rivals briefings is not required for any of these connections.

Show the hosted connection address with a **Copy connection address** button and accessible copied/failed feedback. Provide separate, clearly labelled setup guides for **ChatGPT**, **Claude** and **Gemini**, with short numbered steps and direct links to the provider's connection settings or official help. Keep developer-oriented CLI setup in optional advanced help.

Each provider guide should walk the user through:

1. Open the relevant connection settings in the chosen assistant, using the supplied link.
2. Choose the option to add a custom app or connection and paste the copied Rivals address.
3. Sign in to Rivals and approve read access.
4. Return to the assistant, select or enable Rivals if required, and try “How is my team doing in my mini leagues?”

At implementation, replace generic menu descriptions with the actual provider-specific labels and validate each guide against a real connection flow. Explain any developer-mode or administrator step in everyday language. Put plan, account and regional requirements beside the relevant guide before its setup steps, drawing on the client compatibility section above. If the option is unavailable, explain the likely eligibility or workspace restriction and link to official help. Do not imply Gemini app support is available worldwide or that Gemini CLI is the same experience.

Include a few copyable example questions covering team performance, league gaps, rival squads and upcoming fixtures. Briefly explain that answers use the latest data available in Rivals, so recent changes may not appear immediately. Explain that retrieved data is shared with the chosen assistant.

List authorized connections with the client display name, date connected, last used when known, and a **Disconnect** action. Keep the empty state useful by showing the setup guides and “No assistants connected yet.” Show “Access granted” after authorization; only claim that a connection has been used successfully after an authenticated MCP call. Include help for cancelled sign-in, expired/revoked access, missing provider setup options and unavailable Rivals data. Users should never need to understand OAuth, tokens, JSON or transports to connect.

## Server architecture

```text
Chat client -> HTTPS /mcp -> OAuth validation -> tool schema validation
                                             -> account-scoped shared read queries
                                             -> Django ORM / published snapshots

Browser handlers -> shared read queries -> published data
                 -> bounded refresh scheduling -> existing FPL worker

FPL worker -> FPL upstream -> published snapshots and domain tables
```

Add a small `rivals_mcp/` package for transport, schemas, principal resolution and tool handlers, plus reusable read services under `rivals/services/`. Share the existing database and worker. No second FPL ingestion pipeline, vector database or LLM service is required.

Use the [official Python MCP SDK](https://github.com/modelcontextprotocol/python-sdk) and mount its ASGI app alongside Django through `rivalspy/asgi.py`. Pin a tested SDK release and verify lifespan handling, synchronous ORM execution, middleware boundaries and existing Uvicorn deployment during the first spike. Keep the initial server stateless, using JSON responses over [Streamable HTTP](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports). Handle protocol initialization and version negotiation through the SDK. Validate Host/Origin and configure proxy limits for the MCP route.

Implement published-data readers that neither fetch upstream nor enqueue work. Return usable stale data with explicit freshness where appropriate; missing or unusable publications return unavailable coverage and a link back to Rivals. Cached reports may be written to infrastructure cache, but MCP tool calls must not mutate domain tables or jobs. Existing worker scheduling and browser refresh requests continue to drive ingestion. Only reuse existing read paths whose complete call chains meet the MCP contract; extract queries from refresh orchestration where needed.

## Response contract and limits

Define typed result schemas and always return a JSON text content block. Include equivalent `structuredContent` only where client compatibility is verified; Gemini must work using text alone. Mark all tools `readOnlyHint: true`, `destructiveHint: false`, `idempotentHint: true`, `openWorldHint: false`. Annotations describe behavior; authorization and implementation enforce it. See the [MCP tools specification](https://modelcontextprotocol.io/specification/2025-11-25/server/tools).

Every response should carry:

- `schema_version`, season and relevant gameweek; deadline/kickoff values in ISO 8601 UTC.
- `data` with explicit units: monetary amounts in GBP millions with a unit field, points and percentages distinguished.
- Per-source `updated_at`, `expires_at` where available, and `status` (`fresh`, `stale`, `missing`); response assembly time separately. A database row's update time is not necessarily FPL's event timestamp.
- `coverage` (`complete`, `partial`, `unavailable`), available gameweeks/pages and assumptions. Never turn absent scores, missing pages or missing fixtures into zero, an empty complete league, or a confirmed blank.
- `next_cursor` for lists and verified canonical Rivals links where a route exists. Do not imply a URL reproduces historical filters unless that route supports them.

Default lists to 20 rows and cap at 50. Bind cursors to caller, filters, season and publication; reject stale cursors with a restart instruction. Cap comparisons at two teams and history at one season per call. Proposed operational starting limits: 60 calls per account per minute, 5 concurrent calls, 10-second server deadline and 64 KiB response maximum; truncate through pagination with explicit coverage rather than cutting JSON. Tune after usage measurements.

Missing data is a successful domain response with unavailable coverage. Invalid references/arguments, unsupported seasons and stale cursors are bounded tool errors; authentication and insufficient scope use transport-level authorization responses. Include a retry hint only when useful, with no automatic polling loop or refresh promise. Sanitize untrusted team names and player news as data; never incorporate them into server/tool instructions. Allowlist fields rather than returning raw FPL payloads.

Log tool name, pseudonymous account/grant correlation, duration, result status and response size. Keep tokens, chat text, full tool arguments and result payloads out of logs. The connection consent should explain that retrieved data reaches the chosen assistant and is subject to that provider's handling.

## Follow on analysis

After the factual tools, add `get_team_outlook` and `evaluate_transfer_scenario` using the existing decision engine. These can return hold/transfer comparisons, suggested captains, fixture outlook and assumptions without saving anything. Accept at most two hypothetical transfers and a 3–6 gameweek horizon, matching the documented planner bounds.

Inject the strict published-data adapter rather than relying on `snapshot_only=True`. Separate optional owner-private corrections from public rival data. Return the model version, estimated bank/free transfers/selling prices, source squad and the existing heuristic limitations. No asserted win probability or invented rival future transfers. The assistant explains the engine output; Rivals never generates a paid AI briefing for an MCP call. See [transfer decision behavior](transfer-decisions.md).

Also defer historical league reconstruction, head-to-head match analysis, arbitrary untracked entry lookup, injury/news aggregation beyond stored FPL data, bulk exports, proactive notifications and client-specific interactive widgets. A search/fetch document interface can follow if a target client experience needs retrieval beyond these domain tools.

## Delivery and acceptance

1. **Compatibility and auth spike:** Pin SDK/OAuth dependencies; prove ASGI mounting, discovery, consent, token refresh and one authenticated context tool in real target clients. Decide client-registration configuration from those tests.
2. **Shared read queries:** Extract the queries needed by MCP, with account access policy, season-aware identity resolution, freshness/coverage envelopes and strict snapshot reads. Normalize large-league pages without scheduling them. Preserve browser refresh behavior; migrate browser query use only where needed to share extracted logic.
3. **Factual tool release:** Implement the ten tools, bounded schemas/pagination, the MCP tab in AI settings with provider-specific setup guides and connection management, throttling and operational metrics.
4. **Analysis release:** Adapt the decision engine, test hypothetical inputs and add the two analysis tools separately.

Indicative effort for one engineer familiar with this repo: 2–3 days for the spike, 3–5 for reads/tools, 3–5 for OAuth consent/revocation and 2–4 for integration tests, client checks and documentation: roughly 2–4 working weeks for a production beta, with some auth work overlapping the spike. Forecast tools add roughly 3–5 days. These are planning estimates, dependent on OAuth library fit and client account access; public directory submission is separate.

Release checks:

- Complete connect, use, refresh, revoke and reconnect flows in each supported client; cover disabled/unverified accounts, expired tokens, bad audiences/scopes and invalid redirects.
- Follow each provider guide from the MCP tab through a successful first question using a nontechnical test user. Check connection-address copying, eligibility guidance, empty/authorized/used/disconnected states, keyboard access and preservation of existing AI briefing settings.
- Verify isolation with two accounts, shared FPL teams, removed tracking/membership, guessed references and user-bound cursors/caches.
- Run tool calls with outbound network blocked and assert no domain/job writes, including cold/expired snapshots and missing league pages.
- Verify that browser visits still schedule bounded refreshes for missing/stale data, while MCP reads of the same data return freshness/coverage without scheduling. Scope these checks to paths touched by the extraction.
- Check season rollover, absent squads, Free Hit source context, blank/double/unscheduled fixtures, provisional/final points and partial large leagues.
- Verify input/output schemas, text fallback, pagination, limits and predictable error behavior through an MCP client/Inspector and automated integration tests.
- Evaluate the example questions against known seeded facts; require correct source gameweek, gaps and freshness. Require the assistant to disclose missing data rather than infer a private lineup.

The largest risks are account-linking interoperability, hidden refresh side effects and incomplete publications. Finish those checks before expanding the tool surface.
