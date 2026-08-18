# Docker credential loading from `.env.docker`

## Problem

`build.sh` currently validates only ambient `DOCKER_HUB_USERNAME`. Its former
dotenv fallback and registry-login block are commented out, so valid
`DOCKER_HUB_USERNAME` and `DOCKER_HUB_PAT` entries in `.env.docker` do not affect
the build. The resulting error incorrectly directs operators to `.env`.

## Desired behavior

1. Preserve non-empty process environment values as highest-precedence inputs.
2. Load only missing Docker Hub values from `.env.docker` through
   `scripts/load_docker_credentials.py`; never source the full file.
3. Keep the PAT outside command arguments and repository paths. Pass it to
   `container_runtime_login` through standard input using a temporary file that
   is removed by the existing cleanup trap.
4. Authenticate when a PAT is available. When it is absent, continue with the
   selected runtime's existing registry session.
5. Fail with an accurate `.env.docker` message when no username is available.
6. Never print or persist the PAT.

## Data flow

At startup, `build.sh` captures any ambient Docker Hub variables, validates the
private dotenv file, and initializes a temporary credential location outside
the repository. For each missing value, it invokes the strict credential parser
against `.env.docker`. The parsed username becomes the image namespace. A parsed
or ambient PAT is written only to the protected temporary file, unset from the
shell, and piped into the runtime adapter's login function. Cleanup removes the
temporary file on success or failure.

## Error handling

- Missing or malformed `.env.docker` values must not overwrite valid ambient
  values.
- Parser errors must stop the build before image construction or push.
- Missing username must report that it can be exported or set in `.env.docker`.
- Missing PAT is not fatal because a prior registry login may still be valid.
- Login failure must stop the build.

## Tests

Regression tests will execute the real build script against recording command
fixtures and prove:

- `.env.docker` username and PAT are used without PAT output leakage;
- ambient values override `.env.docker` values;
- ambient username works without reading dotenv credentials;
- missing username reports `.env.docker` accurately;
- PAT reaches registry login only through standard input; and
- focused Azure deployment-script and container-runtime test suites remain
  green.
