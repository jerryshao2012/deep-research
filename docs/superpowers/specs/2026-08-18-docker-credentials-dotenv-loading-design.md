# Docker credential loading from `.env`

## Problem

`build.sh` currently reads `DOCKER_HUB_USERNAME` from `.env`, but ignores the
matching `DOCKER_HUB_PAT` and never authenticates the selected container runtime.
Image pushes therefore depend on ambient runtime credentials and may request a
password even though the private `.env` already contains the PAT. The companion
UI build reads the same backend `.env` and logs in non-interactively.

## Desired behavior

1. Preserve each non-empty process environment value as its own
   highest-precedence input.
2. Load only missing Docker Hub values from `.env` through
   `scripts/load_docker_credentials.py`; never source the full file.
3. Keep the PAT outside command arguments and repository paths. Pass it to
   `container_runtime_login` through standard input using a temporary file that
   is removed by the existing cleanup trap.
4. Authenticate when a PAT is available. When it is absent, continue with the
   selected runtime's existing registry session.
5. Preserve the repository's pinned Docker Hub username validation.
6. Never print the PAT or persist it beyond the build process. A mode-`0600`
   temporary file under `/tmp` is permitted only until login or exit cleanup.

## Data flow

At startup, `build.sh` captures ambient username and PAT independently and
initializes a temporary credential location outside the repository. If both
ambient values are non-empty, `.env` is not parsed. Otherwise it invokes the
strict credential parser against `.env` only for each missing value; the file is
never sourced. An ambient username with no ambient PAT therefore keeps its
username and loads only the file PAT. The parsed username is checked against the
pinned image namespace. A parsed or ambient PAT is written only to the protected
temporary file, unset from the shell, and piped into the runtime adapter's login
function. Cleanup removes the temporary file on success or failure.

## Error handling

- Missing or malformed `.env` values must not overwrite valid ambient values.
  A malformed file is fatal only when at least one credential is missing from
  the process and the file must be parsed; two complete ambient values bypass
  file parsing.
- Parser errors must stop the build before image construction or push.
- A non-pinned username must fail before registry login.
- Missing PAT is not fatal because a prior registry login may still be valid.
- Login failure must stop the build.

## Tests

Regression tests will execute the real build script against recording command
fixtures and prove:

- `.env` username and PAT are used without PAT output leakage;
- ambient values independently override `.env` values;
- complete ambient credentials bypass `.env`, while ambient username alone
  loads only the missing PAT;
- a mismatched username fails before login;
- PAT reaches registry login only through standard input; and
- focused Azure deployment-script and container-runtime test suites remain
  green.
