# Install Meridian Beta

Meridian Beta is headless: a FastAPI app, a CLI, and agent skills. No dashboard,
no Node.js.

## One command

```bash
curl -fsSL https://meridian.matthewmerino.com/install.sh | bash
```

That:

1. checks macOS or Linux,
2. provisions Python 3.12 through a private `uv` when the system Python is not
   3.11/3.12,
3. installs into `~/.meridian` (override with `MERIDIAN_HOME` or `--home`),
4. creates a private virtualenv,
5. writes `~/.local/bin/meridian`.

Then:

```bash
meridian setup
```

Three questions — starting book, model, agent runtime — and the desk is
running.

## Flags

| Flag | Meaning |
|---|---|
| `--home PATH` | install somewhere other than `~/.meridian` |
| `--version X.Y.Z` | install a specific release |
| `--url URL` | use a different update manifest |
| `--local FILE` | install from a local tarball |
| `--force` | reinstall, carrying `data/` forward |
| `--no-setup` | install only; run `meridian setup` later |

`--no-ui` is accepted for compatibility and does nothing; this edition has no
web bundle.

## Manual install

```bash
mkdir -p ~/.meridian/downloads
curl -fL https://meridian.matthewmerino.com/desk/meridian-0.4.4.tar.gz \
  -o ~/.meridian/downloads/meridian-0.4.4.tar.gz
mkdir -p ~/.meridian/app
tar -xzf ~/.meridian/downloads/meridian-0.4.4.tar.gz -C ~/.meridian/app
python3 -m venv ~/.meridian/venv
~/.meridian/venv/bin/python -m pip install -r ~/.meridian/app/requirements.txt
```

## Requirements

- macOS or Linux (Windows via WSL2)
- Python 3.11+; 3.13/3.14 are handled by the installer's private `uv`
- `curl` and `tar`

No Node, no dashboard, no browser.

## After install

```bash
meridian status
meridian doctor
meridian qa
meridian backup
```

`qa` is the end-to-end pass. Any defect goes to
`meridian report --kind …` — the report becomes a GitHub issue.
