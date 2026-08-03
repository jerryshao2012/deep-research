# Authenticate Deep Research services

Use this guide to secure LangGraph requests, protected custom webapp routes, browser OAuth sessions, and passkey flows. It describes current credential precedence, identity behavior, durable session stores, production controls, and recovery procedures.

## Check prerequisites

Install project dependencies with `uv sync`. Before exposing a service beyond loopback, provide stable secrets through local environment variables or a deployment secret store, configure HTTPS, and choose a durable auth store for user sessions.

## Choose an authentication mode

| Mode | Intended use | Credential accepted |
| --- | --- | --- |
| Static API key | Automation and service-to-service access | `X-API-Key`; LangGraph also accepts `Authorization: Bearer` |
| OAuth session | Browser and user-facing clients | Session token in `X-API-Key` or `Authorization: Bearer` |
| Passkey session | Identifier-free browser sign-in after enrollment | Passkey ceremony through a trusted UI BFF, then a bearer session |

API-key and OAuth-session authentication can operate together. Passkeys are disabled by default and retain OAuth as enrollment and recovery path.

## Configure API keys by service surface

### Protect custom webapp routes

Protected document upload, document management, storage, skill, and related webapp routes always authenticate. At process startup, their static key resolves in this order:

1. `UPLOAD_API_KEY`;
2. `LANGCHAIN_API_KEY`;
3. a randomly generated process-local key.

The generated fallback is logged as a warning, changes on restart, and is unsuitable for clients or multiple replicas. Set `UPLOAD_API_KEY` explicitly in production.

Use the selected static key only in `X-API-Key`:

```bash
curl http://localhost:8000/documents/list \
  -H "X-API-Key: $UPLOAD_API_KEY"
```

Protected custom routes also accept an OAuth session token in `X-API-Key` or `Authorization: Bearer`. The static API key is not accepted through `Authorization: Bearer` on this surface.

### Protect LangGraph

LangGraph resolves its static key separately:

1. `LANGCHAIN_API_KEY`;
2. `UPLOAD_API_KEY`.

It does not generate a fallback. A request may send a valid API key or OAuth session token in `x-api-key`, or in `Authorization: Bearer`; if neither static key is configured and the credential is not a valid session, authentication fails with a server-configuration error.

`ALLOW_ALL_THREADS=true` bypasses normal identity checks and returns a test identity. It is a testing switch, not an authentication mode; never enable it in a shared or production environment.

## Configure Google or GitHub OAuth

OAuth routes are served by the custom FastAPI app attached to `langgraph dev` and by the standalone Uvicorn app. When Authlib dependencies import successfully, OAuth route capability is enabled even if provider credentials are blank; configure credentials before login, because blank or invalid values fail during the OAuth flow.

### Register Google

Create a web OAuth client with:

- the frontend origin as an authorized JavaScript origin;
- the exact backend callback `https://<backend-origin>/auth/callback/google` as an authorized redirect URI;
- `openid email profile` consent scopes.

Set `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`. Local LangGraph development normally uses callback `http://localhost:2024/auth/callback/google`; `localhost` and `127.0.0.1` are different registered URIs.

### Register GitHub

Create a GitHub OAuth app for each environment with the frontend as Homepage URL and `https://<backend-origin>/auth/callback/github` as Authorization callback URL. Set `GITHUB_CLIENT_ID` and `GITHUB_CLIENT_SECRET`; a GitHub OAuth client secret is not a personal access token.

For multiple frontend domains, the runtime also accepts paired comma-separated mappings:

```dotenv
GITHUB_CLIENT_IDS=domain-one.example:<client-id>,domain-two.example:<client-id>
GITHUB_CLIENT_SECRETS=domain-one.example:<client-secret>,domain-two.example:<client-secret>
```

The domain keys present in both variables select the matching OAuth client. Keep a default `GITHUB_CLIENT_ID` and `GITHUB_CLIENT_SECRET` when a fallback client is required.

### Allow frontend redirects

Set `FRONTEND_URLS` to a comma-separated list of exact frontend origins. `/auth/login/{provider}` accepts `redirect_url` only when it matches the configured origin allowlist, stores the selected frontend and safe return path in the signed session cookie, and constructs its callback from the request or forwarded host and protocol.

Register callback URLs that exactly match the externally visible scheme, host, port, and path. Configure the reverse proxy to overwrite untrusted forwarded headers rather than passing arbitrary client values.

### Use OAuth sessions

Start login at `/auth/login/google` or `/auth/login/github`. After callback, the backend creates a random session token and redirects to `<frontend>/login/success?token=...`; the frontend should capture the token, remove it from browser history immediately, and prevent query strings from entering analytics, logs, or referrer data.

Sessions last 24 hours. Validation applies a sliding refresh when less than one hour remains; clients can also call `POST /auth/session/refresh`, validate through `GET /auth/session/validate`, and revoke through `POST /auth/logout`.

## Choose a durable session store

OAuth and passkey sessions use the current SQLite, PostgreSQL, or Cosmos DB auth-store adapters. The former README's in-memory and Redis example is not a supported runtime adapter.

| Backend | Selection and required configuration | Operational use |
| --- | --- | --- |
| SQLite | `AUTH_STORE_TYPE=sqlite` or `DB_TYPE=sqlite`; `SQLITE_DB_PATH` defaults to `:memory:` | Local development. Set a persistent path for durable sessions. |
| PostgreSQL | `AUTH_STORE_TYPE=postgres`; `DATABASE_URL` or `POSTGRES_URL`, otherwise host/user/database variables | Multi-replica production. Port defaults to `5432`. |
| Cosmos DB | `AUTH_STORE_TYPE=cosmosdb`; connection string or endpoint and key | Multi-replica production. Database defaults to `deep_research`. |

SQLite defaults to WAL journal mode. Azure Files/SMB deployments must use `AUTH_SQLITE_JOURNAL_MODE=DELETE`, mount the database on durable storage, and remain at one replica; the startup check can reject an in-memory path but cannot prove that a path is actually persistent.

## Configure passkeys

Set `PASSKEY_ENABLED=true` only after all prerequisites are present:

- at least one complete Google or GitHub OAuth configuration for enrollment and recovery;
- a durable auth store;
- `OAUTH_SECRET_KEY` containing 32-4096 unpredictable bytes;
- `PASSKEY_PROXY_ID` and a separately generated `PASSKEY_PROXY_SECRET` containing 32-4096 bytes;
- `PASSKEY_ORIGINS` and either `PASSKEY_RP_IDS` or `PASSKEY_RP_ID`; `PASSKEY_RP_NAME` is optional and defaults to `BMO Deep Agent`.

Startup fails closed when enabled passkey configuration is incomplete. Never define both RP-ID variables: `PASSKEY_RP_ID` is an absent-only single-domain fallback.

### Configure origins and relying parties

`PASSKEY_ORIGINS` contains exact browser origins. Outside localhost, each origin must use HTTPS and its hostname must equal or be a subdomain of the most-specific configured RP ID.

RP IDs are normalized to lowercase ASCII DNS names. Schemes, ports, paths, wildcards, malformed labels, IP addresses, and public suffixes are rejected; every configured RP ID must map to an origin. Unrelated RP domains require separate passkey enrollment.

For local development, omit `PASSKEY_RP_IDS`, then use:

```dotenv
PASSKEY_RP_ID=localhost
PASSKEY_ORIGINS=http://localhost:3000
```

The BFF must send the same proxy identity and secret configured by the backend. Never expose the proxy secret through browser-visible variables.

### Tune passkey windows and limits

| Variable | Default | Purpose |
| --- | ---: | --- |
| `PASSKEY_CHALLENGE_TTL_SECONDS` | `300` | One-time ceremony lifetime. |
| `PASSKEY_RECENT_AUTH_SECONDS` | `600` | Recent-auth window for sensitive management actions. |
| `PASSKEY_AUTHENTICATED_RATE_LIMIT` | `20` | Authenticated-account rate limit. |
| `PASSKEY_ANONYMOUS_RATE_LIMIT` | `300` | Anonymous ceremony rate limit. |

Registration requires an OAuth-backed session and user verification. Authentication is identifier-free; management lists credentials for the signed-in immutable provider identity across configured RPs. Google and GitHub accounts are never linked merely because their email addresses match.

## Understand request identity

OAuth accounts use immutable provider subjects: `google:<sub>` or `github:<numeric-id>`. The auth store rejects provider/subject conflicts and refreshes sanitized profile data separately from identity.

LangGraph receives a compact identity containing `identity`, `display_name`, and `is_authenticated`; a static key maps to `admin`. The custom `/auth/session/validate` response exposes normalized user fields and sanitized provider metadata, while excluding raw provider tokens and the session token.

Protected custom routes currently authorize through a boolean helper; they do not populate the README's former `request.state.user_identity` example. Code requiring user metadata should validate the session through the current auth store or session endpoint rather than assuming that request-state field exists.

## Harden production deployments

- Terminate TLS before every OAuth, session, and passkey route; retain HTTPS through the trusted proxy boundary.
- Store API keys, OAuth client secrets, `OAUTH_SECRET_KEY`, and `PASSKEY_PROXY_SECRET` in a secret manager. Generate each independently and rotate with an explicit session-invalidation plan.
- Keep `OAUTH_SECRET_KEY` stable across replicas and restarts so signed OAuth state cookies remain valid.
- Session-cookie `Secure` currently follows passkey origin configuration and is false when passkeys are disabled; do not expose an HTTP route for a production auth domain.
- Use PostgreSQL or Cosmos DB for multi-replica auth state. Use persistent SQLite only for a single replica.
- Restrict `FRONTEND_URLS`, CORS origins, OAuth callbacks, and passkey origins to intended domains.
- Strip or overwrite inbound `X-Forwarded-Host` and `X-Forwarded-Proto` at the edge.
- Keep auth and session endpoints out of verbose request-body and query-string logs.
- Monitor failed logins and expired-session cleanup. General OAuth endpoints do not gain rate limiting from passkey ceremony limits.

## Recover Cosmos passkey reservations

A crashed Cosmos writer can leave a capacity reservation after the credential or challenge document was never created. Stop every application replica and auth writer before repair; Cosmos cannot fence a paused writer across containers.

Dry-run an age-bounded inspection first:

```bash
uv run python -m scripts.reclaim_cosmos_auth_reservations \
  --identity 'google:<provider-subject>' \
  --cutoff <unix-timestamp> \
  --confirm-quiesced
```

Review the count-only JSON, then repeat with `--apply`. `--limit` bounds one invocation to at most 100; the tool point-reads referenced documents and uses the current account ETag before removal. Restart writers only after it exits.

Committed markers remain untouched by default. If the underlying document is known to be absent, perform a separate dry-run with `--include-committed-missing`, then apply while all writers remain stopped; this opt-in path has no age signal.

## Troubleshoot authentication

### Login reports missing or invalid client configuration

Confirm the selected provider has both ID and secret and restart the process after changing `.env`. OAuth capability may be loaded even with blank credentials, so route availability alone does not prove provider readiness.

### Provider rejects the redirect URI

Compare scheme, hostname, port, and callback path character for character. `langgraph dev` normally serves callbacks on port `2024`; standalone Uvicorn uses the port passed to the command, commonly `8000`.

For Google, configure both frontend JavaScript origins and backend redirect URIs. For GitHub, configure Homepage URL and Authorization callback URL; private emails are read through the requested `user:email` scope.

### Protected route returns 401 after restart

If no stable `UPLOAD_API_KEY` or fallback `LANGCHAIN_API_KEY` was configured, the custom webapp generated a new process-local key. Set an explicit key and update the client.

### Sessions disappear or replicas disagree

Check `AUTH_STORE_TYPE`/`DB_TYPE` and database connectivity. An unset SQLite path is in-memory, a local SQLite file is not shared across replicas, and SQLite on SMB requires `DELETE` journal mode plus a single replica.

### Passkey startup fails

Check OAuth recovery, durable storage, secret lengths, exact origins, RP-ID exclusivity, and proxy settings. Use `uv run pytest tests/test_passkeys.py -q` and `uv run pytest tests/test_auth_store.py -q` for focused local validation.

## Related documentation

- [Configuration](configuration.md)
- [Usage](../getting-started/usage.md)
- [Document Upload API](../api/upload.md)
- [Thread Wiki API](../api/wiki.md)
- [Azure security](../deployment/azure/security.md)
- [Handbook index](../README.md)
