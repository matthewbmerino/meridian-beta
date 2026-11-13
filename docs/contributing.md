# Contributing to Meridian Desk

## Reports first

The front door is a report, not a patch:

```bash
meridian report
```

A report is **a problem and a recommended solution**, and nothing else. One
problem per report. The CLI adds the version, OS, arch, Python, Node, and agent
runtime; it never adds the book, positions, theses, decisions, logs, paths,
emails, or keys.

If you have repo access, open a pull request that references the report id and
uses the same two fields in its body.

## Privacy is a hard rule

- Never paste `.env`, tokens, positions, theses, decisions, imports, or logs
  into an issue, a pull request, or a report.
- Reproduce with the synthetic demo book
  (`fixtures/demo-positions.csv`), never a real book.
- If a bug cannot be described without user data, describe the **shape** of the
  data instead (for example, "a positions CSV with a blank ticker row").
- A report that contains user data is deleted, not triaged.
- Security issues go to the private channel, not the public intake.

## Fixes

1. Reproduce from the report, on a clean install.
2. Add a test that fails before the fix.
3. Fix the smallest surface that explains the report.
4. Bump `VERSION`, add a line to `RELEASE_NOTES.md`, and cite the report id.
5. Build, publish, and verify the public install path.

## The release loop

```bash
scripts/build_release.sh
sudo cp dist/* /var/www/meridian-desk/
```

Then install into a throwaway home and run the verification steps in
`docs/update.md`. A release is not shipped until the public tarball's SHA-256
matches the manifest and a clean install reaches a healthy `meridian status`.

## What a good report looks like

```
Problem:  On Omarchy (Python 3.14), the installer exits before unpacking
          because it requires the system Python to be 3.11/3.12.
Solution: When the host Python is outside 3.11/3.12, fetch a private uv and
          provision a managed 3.12 for the venv; never touch the system Python.
```

No paths, no usernames, no portfolio values.
