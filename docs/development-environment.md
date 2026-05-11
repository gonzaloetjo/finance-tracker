# Development Environment

This repo uses `uv.lock` as the Python dependency authority and `devenv` as
the reproducible system-tool shell around it. The committed devenv files pin
Nix inputs and system tools; they do not duplicate Python dependencies in
Nix.

## Why devenv

- The app is local and stateful, so Docker is not the right default runtime.
- Python dependencies are already pinned with `uv.lock`; duplicating them in
  Nix would create a second dependency source of truth.
- Devenv pins Python 3.11, uv, shell tools, tasks, processes, and git hooks
  without replacing the existing `uv run finance ...` workflow.

## Setup

Install Nix and devenv `2.1` or newer, then from the repo root:

```bash
devenv shell
```

Devenv 2.1 has native shell activation. No `.envrc` is committed or needed.
To enable automatic activation in zsh, add this once to your shell config and
trust the repo:

```bash
eval "$(devenv hook zsh)"
devenv allow
```

## Commands

```bash
devenv test                 # full local check suite
devenv tasks run checks     # run the check task namespace
devenv tasks list           # inspect the task graph
statix check devenv.nix     # Nix config lint, also part of devenv test
finance-test -q             # pytest wrapper
finance-audit               # pip-audit wrapper
finance-serve --port 8001   # dashboard wrapper
devenv up                   # process TUI; dashboard is available but not autostarted
```

`devenv shell` pins Python with `pkgs.python311` from the locked nixpkgs
input and runs `uv sync --frozen --all-groups`, so Python packages continue
to come from `pyproject.toml` and `uv.lock`. Devenv writes the regular shell
virtualenv to `.devenv/state/venv`; `devenv test` uses an isolated
`.devenv/test-state/venv`.

The first shell entry downloads the pinned Nix store paths and the locked
Python wheels. Later entries reuse those caches. The check graph includes
`statix check devenv.nix` so the committed Nix config gets the same local
quality gate as Python and shell code.

## Local State And Secrets

The committed environment keeps the shell mostly clean but preserves the env
vars this app legitimately needs:

- `FINANCE_CONFIG_DIR`, `FINANCE_DATA_DIR`, `FINANCE_KEY_PASSPHRASE`
- `ANTHROPIC_API_KEY`
- D-Bus/display/runtime vars for OS keyring and browser flows
- `SSH_AUTH_SOCK` and `GITHUB_TOKEN` for normal developer workflows

Developer-specific overrides belong in `devenv.local.nix` or
`devenv.local.yaml`; both are ignored.

Devenv also generates `.pre-commit-config.yaml` from the committed
`git-hooks` configuration. That file is local generated state and is ignored.
