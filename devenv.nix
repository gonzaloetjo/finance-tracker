{ config, pkgs, ... }:

{
  name = "finance";

  packages = [
    pkgs.bashInteractive
    pkgs.curl
    pkgs.git
    pkgs.jq
    pkgs.openssl
    pkgs.shellcheck
    pkgs.sqlite
  ];

  env = {
    PYTHONDONTWRITEBYTECODE = "1";
    PYTHONUNBUFFERED = "1";
    UV_CACHE_DIR = "${config.devenv.state}/uv-cache";
  };

  languages.python = {
    enable = true;
    package = pkgs.python311;
    venv.enable = true;
    uv = {
      enable = true;
      sync = {
        enable = true;
        allGroups = true;
        arguments = [ "--frozen" ];
      };
    };
  };

  scripts = {
    finance-check = {
      exec = "devenv test";
      description = "Run the full devenv check suite.";
    };
    finance-test = {
      exec = ''uv run pytest "$@"'';
      description = "Run pytest through the locked uv environment.";
    };
    finance-audit = {
      exec = ''uv run pip-audit --skip-editable "$@"'';
      description = "Run the dependency vulnerability audit.";
    };
    finance-serve = {
      exec = ''uv run finance serve "$@"'';
      description = "Start the local dashboard.";
    };
  };

  tasks = {
    "checks:ruff" = {
      exec = "uv run ruff check src tests";
      before = [ "devenv:enterTest" ];
    };
    "checks:mypy" = {
      exec = "uv run mypy src/finance";
      before = [ "devenv:enterTest" ];
    };
    "checks:vulture" = {
      exec = "uv run vulture src/finance --min-confidence 80";
      before = [ "devenv:enterTest" ];
    };
    "checks:pytest" = {
      exec = "uv run pytest -q";
      before = [ "devenv:enterTest" ];
    };
    "checks:pip-audit" = {
      exec = "uv run pip-audit --skip-editable";
      before = [ "devenv:enterTest" ];
    };
    "checks:shell" = {
      exec = "bash -n scripts/finance-all.sh && shellcheck scripts/finance-all.sh";
      before = [ "devenv:enterTest" ];
    };
  };

  enterTest = ''
    echo "devenv checks passed"
  '';

  processes.dashboard = {
    exec = "uv run finance serve";
    start.enable = false;
  };

  git-hooks.hooks = {
    ruff = {
      enable = true;
      name = "ruff";
      entry = "uv run ruff check src tests";
      language = "system";
      pass_filenames = false;
    };
    finance-shell-syntax = {
      enable = true;
      name = "bash syntax";
      entry = "bash -n scripts/finance-all.sh";
      language = "system";
      pass_filenames = false;
      files = "^(scripts/finance-all\\.sh)$";
    };
  };
}
