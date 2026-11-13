# Updates

```bash
meridian update --check
meridian update
```

`--check` prints the latest version and notes; it changes nothing. `update`
asks, backs up the data, downloads the release, verifies the checksum, swaps
the app tree, and health-checks the API. If the new version fails, the previous
tree is restored.

## What is preserved

The update replaces code, not the operator's data. It copies every non-Python
entry from `app/data` into the new tree:

- JSON stores and their `.hmac` / `.bak` sidecars
- `.auth_credentials`, `.auth_pin`, `.session_secret`
- `imports/`, `snapshots/`, `gpu/`, `reports/`, `deep_history/`
- `market_panel.parquet` and the rest of the caches

`meridian.toml` and `.env` live in `~/.meridian`, outside the app tree, so they
are untouched.

## The manifest

`https://meridian.matthewmerino.com/desk/manifest.json`:

```json
{
  "version": "0.4.4",
  "channel": "beta",
  "released": "2026-11-13",
  "url": "https://meridian.matthewmerino.com/desk/meridian-0.4.4.tar.gz",
  "sha256": "…",
  "notes": ["…"]
}
```

The CLI refuses a tarball whose SHA-256 does not match the manifest.

## Rollback

```bash
meridian update --rollback
```

The previous tree is kept at `~/.meridian/app.old` until the next update. A
rollback stops the services, swaps the tree back, and health-checks the API.

## Manual update

If the CLI is broken, download the tarball and replace the tree by hand:

```bash
meridian stop
mv ~/.meridian/app ~/.meridian/app.old
mkdir -p ~/.meridian/app
tar -xzf meridian-0.4.4.tar.gz -C ~/.meridian/app
# copy data/ across (non-.py entries) — or restore a backup
meridian start
```

The CLI does this correctly; use the manual path only when it cannot run.

## Updating from 0.3.2 or earlier on a uv venv

The 0.3.2 updater runs its own dependency step with `python -m pip`, and a
uv-created venv has no pip. The 0.3.4 fix lives in the new code, which the old
updater cannot run until the update succeeds. Add pip first:

```bash
~/.meridian/venv/bin/python -m ensurepip --upgrade
meridian update
```

Or bypass the old updater with a data-preserving reinstall:

```bash
meridian backup
curl -fsSL https://meridian.matthewmerino.com/install.sh | bash -s -- --force --no-setup
meridian start
```

`--force` stops the running desk, moves the old app aside, carries every
non-code data entry forward, and recreates the venv. 0.3.4 and later update
normally with uv.

## Releasing (maintainer)

```bash
# 1. Build the release (Python only; no web bundle)
scripts/build_release.sh

# 2. Publish
scp dist/meridian-0.4.4.tar.gz dist/latest.tar.gz dist/manifest.json \\
    user@your-host:/var/www/meridian-beta/
```

`dist/manifest.json` points at the versioned tarball. `latest.tar.gz` is a
convenience alias for installs that do not read the manifest.
