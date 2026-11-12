# Deployment safeguards

The default web container validates its production configuration and applies database migrations **before** starting Uvicorn. A failed check or migration exits the container before it can listen or pass Kamal's health check. The job role continues using the existing migration-wait behavior. No production deployment is performed by the local checks described here.

## Production environment

Start with `.env.production.example` and configure your deployment environment. Kamal loads `.env.production` locally and injects the values listed in `config/deploy.yml` into both roles. `SITE_URL` and `DEFAULT_FROM_EMAIL` are included alongside the existing SMTP, Django and AI settings. The example deliberately leaves secrets empty.

Production startup rejects these conditions:

- A missing, short, repetitive or Django development `SECRET_KEY`. Use an independently generated key with at least 32 characters. For a new installation, `uv run --locked python -c 'from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())'` generates one. Changing an existing key invalidates signed links and sessions unless rotation is planned separately; this epoch does not automatically rotate private credentials.
- Empty `ALLOWED_HOSTS` or wildcard hosts. List the actual permitted domain names.
- A `SITE_URL` that is not a public HTTPS origin on an allowed host, or contains credentials, a path, query, fragment or whitespace. Localhost and loopback addresses are rejected. This canonical URL is used in verification and unsubscribe links.
- The default placeholder sender, an invalid sender address or email header newlines. Configure a sender authorized by your email provider.
- Both TLS and SSL enabled, or an unencrypted remote SMTP connection. A local SMTP relay can be used without TLS. Custom non-SMTP email backends manage their own transport.

Keep `DEBUG=False` in production. The local `.env.example` uses console email and local defaults. Tests and the `collectstatic` command can run without production credentials; asset collection never serves requests. The web startup calls `check_production_config` after environment injection; direct operational commands must invoke it explicitly when production validation is needed.

Kamal terminates TLS and controls access to the application container. Keep that network boundary intact: Django trusts the proxy's HTTPS header, and the request limiter uses the client address supplied by the application server. The web startup resolves `TRUSTED_PROXY_HOST=kamal-proxy` on the private Docker network and passes only those addresses to Uvicorn’s forwarded-header trust list. App ports must remain unpublished. Local startup trusts loopback only.

## Release gate and boot order

`.kamal/hooks/pre-deploy` runs locally after the image is available and before new app containers boot. It requires `uv` and Node.js with `node --test` support. The hook uses the installed Kamal 2.12.0 configuration loader to resolve each web/job host environment, including clear values, overrides, and secret aliases, then passes it directly to Django without printing secrets. `.env.production` is loaded by the ERB in `config/deploy.yml`; `.kamal/secrets` maps those values to container secrets.

1. Prepare the pinned Python interpreter and run `check_production_config` using the resolved web/job container environments, with telemetry and local dotenv loading disabled. Unsafe production configuration aborts deployment.
2. Replace runtime credentials with isolated test settings, disable remote email and telemetry, and run the complete pytest suite. Clear inherited `PYTEST_ADDOPTS` so a local filter or collect-only option cannot silently skip the release checks.
3. Run all JavaScript tests, Django system checks and migration consistency checks for application-owned models. Third-party packages own their migration histories.
4. Boot the web image using `bin/start-web`: run `check_production_config` and migrations, then replace the shell with Uvicorn. The configured deployment timeout is 120 seconds. Migration failures prevent traffic switching to the new web container.
5. After boot, check the deployed job container by its exact release version, waiting up to 60 seconds for a new dispatcher tick from that same container after the check starts. A failed check returns an error; it is not reported as a successful deployment.

The current deployment has one web container and a worker sharing local SQLite storage. Schema changes still need to be compatible with the old web/worker code during a rolling deployment. Use expand/contract changes for destructive schema updates. Startup checks do not provide an automatic database rollback, and the post-deploy worker check does not reverse traffic that has already switched.

The post-deploy hook no longer runs migrations. Operational `kamal app exec` commands also do not implicitly run the web startup script. If you override the default web command, preserve its checks-before-listening behavior.

## Reproducible assets and image contents

The Dockerfile pins the standalone Tailwind CLI to v4.1.7, matching the checked-in CSS toolchain. Asset compilation runs on the native Docker build platform, while Python dependencies and runtime use the configured target platform. This avoids the standalone compiler falling back to Bun's CLI under x64-on-ARM QEMU emulation. Only the resulting platform-independent CSS crosses between stages, and the build requires a newly generated, nonempty stylesheet.

Both supported compiler artifacts are checked against the [official release checksum manifest](https://github.com/tailwindlabs/tailwindcss/releases/download/v4.1.7/sha256sums.txt) before execution:

| Build architecture | SHA-256 |
| --- | --- |
| amd64 | `0706292935a9773c6c879e17d23625322e4490e7e8f7a0ad0e688faae4def981` |
| arm64 | `8c419a662196f05726551ad006bd8341f47b8bedf8e0cb651687d9ae3c0ae066` |

The amd64 digest was also checked against the complete downloaded artifact. Updating the CLI requires reviewing a specific release and updating its checksums together; a moving `latest` URL is not used. Native asset builds require BuildKit's automatic BUILDPLATFORM/BUILDARCH arguments, as provided by the current Kamal/buildx workflow.

Asset collection runs in the build stage without runtime secrets. `.dockerignore` excludes environment secrets, databases and SQLite WAL/shared-memory/journal sidecars, preventing a live database's sidecars from entering the image through `COPY .`.

Kamal's [deployment flow](https://kamal-deploy.org/docs/commands/deploy/) switches traffic after the new web container passes its health check. See [application commands](https://kamal-deploy.org/docs/commands/app/) for the distinction between executing commands in a new container and reusing the deployed container.

## Epoch 8 validation

The epoch checkpoint in [epochs.md](epochs.md) records the final test and local image validation results. Local test credentials are disposable; no real SMTP delivery, production secret rotation or remote deployment is included.
