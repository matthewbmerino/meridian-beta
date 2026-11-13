# Meridian Beta 0.4.4 (early beta)

- CLI polish: one blank line per section, silent installer downloads, and `meridian qa` in the setup next-steps.
- The 0.4.3 monochrome surface is unchanged: dithered mark, thin rules, spaced labels, deny/approve confirmations.
- No command behavior changed.

# Meridian Beta 0.4.3 (early beta)

- New CLI surface: monochrome, thin rules, generous spacing, a dithered brand mark, and deny/approve confirmations.
- The installer, `setup`, `status`, `qa`, `doctor`, and `report` now share one hierarchy. No command behavior changed.
- The beta channel, headless API/CLI, update/rollback, and report-to-GitHub-issue loop are unchanged.

# Meridian Beta 0.4.2 (early beta)

- Renamed from Meridian Desk to Meridian Beta. The repository is public at github.com/matthewbmerino/meridian-beta.
- Removed the MIT license. No license is granted yet; all rights reserved.
- The free edition remains headless: API, CLI, and agent skills; no dashboard, no Node.js.
- `qa`, report-to-GitHub-issue, beta channel, installer, and update/rollback are unchanged.

# Meridian Beta 0.4.1 (early beta)

- This edition is headless: the API, CLI, and agent skills only. No Next.js dashboard, no Node.js, no web bundle.
- Moved to its own repository, matthewbmerino/meridian-beta; the personal Meridian dashboard is a separate product.
- `meridian qa` covers the API, book, VaR, stress, and record. There is no visual pass because there is no dashboard.
- Reports are filed as GitHub issues in the desk repository; `meridian report` returns the issue URL.
- The installer/updater, beta channel, and report kinds are otherwise unchanged.

# Meridian Beta 0.4.0 (early beta)

- The installer, `meridian version`, `meridian status`, `meridian doctor`, and the update manifest now label this channel `beta`.
- `meridian qa` runs the end-to-end checks an agent needs: API, auth, positions, summary, VaR, stress, liabilities, theses, decisions, data health, and the dashboard.
- `meridian report --kind bug|missing-flow|visual|architecture|docs` classifies a report; `GET /api/desk/reports` filters by kind.
- Every agent skill now requires QA plus a visual pass, and reports are the only path for fixes to an installed desk.

# Meridian Beta 0.3.4

- Updates no longer fail with `No module named pip` on a uv-created virtualenv: `meridian update` uses `uv pip` when uv is present, then pip, then `ensurepip`.
- A forced reinstall replaces the virtualenv instead of failing on the existing one.

# Meridian Beta 0.3.3

- Prices and analytics work on a fresh install with no data key: Yahoo Finance via yfinance supplies delayed quotes, daily history, ticker search and basic fundamentals.
- Schwab and Massive still take precedence when configured; a failed paid feed now falls back to Yahoo instead of an empty book.
- The free feed batches quotes, caches 30s/15m, and backs off five minutes on a Yahoo rate limit. It never fabricates a price.
- Massive HTTP errors no longer write the API key into logs.
- yfinance pin updated to 1.7.0.

# Meridian Beta 0.3.2

- `meridian report`: report a problem and a recommended solution from the CLI.
- A report is exactly those two fields plus version, OS, arch, Python, Node and agent — never the book, positions, theses, decisions, logs, paths, emails or keys.
- Reports are saved locally first (`~/.meridian/reports`, 0600) and can be sent later with `meridian report --send-all`.
- `CONTRIBUTING.md` and `docs/reporting.md` define the report-to-release loop.

# Meridian Beta 0.3.1

- Python 3.13/3.14 hosts (Omarchy, Arch, recent Fedora) now work: the installer fetches a private uv and provisions a managed Python 3.12, without sudo or touching the system Python.
- Passwords no longer repeat a word.

# Meridian Beta 0.3.0

- First free release: one-command install, `meridian` CLI, and agent-guided onboarding.
- Ships a synthetic demo book so the first run is populated, and a CSV import for a real book.
- `meridian update` checks a checksummed manifest, verifies the tarball, preserves data, and can roll back.
- Skills for Hermes, Claude Code, and Codex install the setup and day-to-day workflows.
