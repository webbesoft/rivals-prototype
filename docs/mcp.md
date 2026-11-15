# Rivals MCP operation

Rivals exposes ten authenticated, read-only tools at `/mcp` over Streamable HTTP. The **MCP** tab at `/accounts/ai/mcp/` contains connection instructions for ChatGPT, Claude and Gemini, a copyable server address and per-account disconnect controls. Existing AI briefings remain on the adjacent tab.

## Run and configure

Install the locked dependencies and apply migrations, including Django OAuth Toolkit and `rivals_mcp`:

```sh
uv sync --locked
uv run python manage.py migrate --noinput
uv run uvicorn rivalspy.asgi:application --host 127.0.0.1 --port 8000
```

The production `bin/start-web` already starts this ASGI application and runs migrations. Django's default WSGI `runserver` serves the settings pages but does not mount the MCP transport; use Uvicorn for MCP development. Keep `fpl_worker` running separately as before.

Set `SITE_URL` to the canonical public HTTPS origin, for example `https://rivals.webbe.dev`, and include its hostname in `ALLOWED_HOSTS`. This origin defines the OAuth issuer, discovery URLs and exact MCP token audience. Changing it invalidates existing token audiences; reconnect assistants after an intentional hostname change. Hosted providers must reach this public address; localhost alone will not work for their cloud clients.

`MCP_ENABLED=True` enables calls and new connections. Set it to `False` and restart the web process to stop calls/new consent/registration while keeping the disconnect controls available. `MCP_ALLOWED_ORIGINS` is a comma-separated list of exact allowed Origin headers, defaulting to `SITE_URL`. Hosted client requests without an Origin header are accepted. Add origins only for clients that actually send them. Kamal forwards both settings; `.env.production.example` documents them.

The transport is stateless and accepts JSON results, with a 64 KiB request/result limit, a ten-second request timeout, 60 authenticated requests per account per minute and five concurrent requests per account per web process. The existing deployment runs one web worker. If scaling to several workers, coordinate concurrency limits across processes. OAuth token requests have an IP limit of 120/minute and dynamic registrations 10/hour. Error and performance telemetry excludes MCP and credential endpoint requests.

## Account linking

Public discovery documents:

- `/.well-known/oauth-authorization-server`
- `/.well-known/oauth-protected-resource/mcp`

OAuth endpoints live under `/mcp/oauth/`: `authorize/`, `token/`, `revoke/`, and `register/`. Django OAuth Toolkit implements the protocol; Rivals adds verified-account policy, consent, exact resource validation and connection revocation. Only authorization-code grants with S256 PKCE and the `rivals:read` scope are allowed. Access tokens last one hour, refresh tokens rotate and expire after 30 days, and replay protection is enabled. Tokens are hashed at rest. Client names are self-declared and the consent screen shows the return address.

Anonymous dynamic registration supports hosted clients without asking managers to handle credentials. Register automatically when configuring Claude; the server does not fetch client identity documents. Registered redirect addresses use HTTPS with exact matching. HTTP loopback callbacks are allowed only when Django is in development mode. Gemini CLI local callbacks therefore require deliberate configuration rather than the default production setup. Administrators can preregister an authorization-code OAuth application for a client that needs explicit credentials using Django OAuth Toolkit's application model.

Use the library's maintenance command periodically to remove expired OAuth artifacts:

```sh
uv run python manage.py cleartokens
```

Review unused dynamically registered clients periodically; registration metadata persists independently of token expiry. No cleanup schedule or public connector-directory submission is installed by this change.

## Data behavior

`rivals/services/published_reads.py` is the shared query interface. It reads domain tables and `FplSnapshot` publications, including usable expired snapshots with stale status. It never calls FPL, creates a missing team/page or queues refresh work. Browser handlers retain their existing demand-driven refresh behavior. A user opening a squad or league in Rivals may therefore make data available that MCP initially reports as unavailable.

Tools cover context, leagues/standings, tracked rivals, team summary/squad/history/transfers, squad comparison and upcoming fixtures. Discovery returns `entry:<id>`, `league:<id>`, `player:<season>:<id>` and fixture club references. Explicit team lookup is limited to the account's own team, active tracked rivals and accessible current-season league entries already present in the database. Summary-page access resolution scans at most 100 stored pages; an entry beyond that scan must be tracked in Rivals for reliable detail lookup. It never imports an entry just because an assistant asks about it.

Lists default to 20 rows and cap at 50. Signed cursors expire after 15 minutes and bind the account, filters, access state and publication. Summary standings read at most three existing pages from the relevant location and paginate those rows. Missing pages are reported as partial coverage. The cursor traverses those publications; it cannot fetch the rest of a large league. Head-to-head match standings and transfer forecasts are outside this factual release.

Squad comparisons require two complete squads at the same gameweek. Current bank values are public estimates, not private pre-deadline balances. Missing prices, scores and publications remain unknown. Fixture results distinguish blank weeks, doubles and unscheduled matches; availability/news come from FPL. User planning corrections, journals, email addresses and saved Gemini credentials are excluded.

## Verification

```sh
uv run pytest tests/unit/test_mcp.py --no-cov
uv run python manage.py check
uv run python manage.py makemigrations --check --dry-run
```

The tests exercise actual SDK HTTP initialization/list/call behavior, discovery, registration, consent, token exchange/rotation, revocation, account isolation, schema rejection, expired/missing publications and read queries with network access blocked and SQL writes prohibited.

Before advertising provider compatibility, use a deployed HTTPS endpoint and eligible accounts to complete a first question, refresh and reconnect in each provider. Setup copy follows their current official instructions, but those real-account flows and a nontechnical user walkthrough require live validation. No production deployment or provider account connection is performed by the automated suite.

Provider setup references: [ChatGPT](https://help.openai.com/en/articles/12584461-connecting-custom-mcp-servers-in-chatgpt), [Claude](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp), [Gemini](https://support.google.com/gemini/answer/17209137). The fuller product scope and follow-on analysis plan are in [MCP scope](mcp-scope.md).
