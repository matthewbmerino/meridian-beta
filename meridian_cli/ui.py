"""Terminal UI helpers for the Meridian CLI.

Stdlib only: the CLI has to work before (and without) the app's virtualenv
being healthy, so it must not import rich/click.
"""

from __future__ import annotations

import getpass
import json
import os
import sys
from typing import NoReturn

_TTY = sys.stdout.isatty() and os.getenv("NO_COLOR") is None
QUIET = False
_BOLD = "\033[1m" if _TTY else ""
_DIM = "\033[2m" if _TTY else ""
_CYAN = "\033[36m" if _TTY else ""
_GREEN = "\033[32m" if _TTY else ""
_YELLOW = "\033[33m" if _TTY else ""
_RED = "\033[31m" if _TTY else ""
_RESET = "\033[0m" if _TTY else ""


def bold(s: str) -> str:
    return f"{_BOLD}{s}{_RESET}"


def dim(s: str) -> str:
    return f"{_DIM}{s}{_RESET}"


def cyan(s: str) -> str:
    return f"{_CYAN}{s}{_RESET}"


def green(s: str) -> str:
    return f"{_GREEN}{s}{_RESET}"


def yellow(s: str) -> str:
    return f"{_YELLOW}{s}{_RESET}"


def red(s: str) -> str:
    return f"{_RED}{s}{_RESET}"


def banner(title: str, subtitle: str = "") -> None:
    if QUIET:
        return
    line = "─" * min(58, max(24, len(title) + 4))
    print()
    print(cyan(line))
    print(bold(f"  {title}"))
    if subtitle:
        for part in subtitle.splitlines():
            print(dim(f"  {part}"))
    print(cyan(line))
    print()


def step(msg: str) -> None:
    if QUIET:
        return
    print(f"{cyan('→')} {msg}")


def ok(msg: str) -> None:
    if QUIET:
        return
    print(f"{green('✓')} {msg}")


def warn(msg: str) -> None:
    if not QUIET:
        print(f"{yellow('!')} {msg}")


def fail(msg: str) -> None:
    print(f"{red('✗')} {msg}", file=sys.stderr)


def info(msg: str) -> None:
    if QUIET:
        return
    print(f"  {msg}")


def ask(prompt: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    try:
        answer = input(f"{prompt}{suffix}: ").strip()
    except EOFError:
        return default or ""
    return answer or (default or "")


def ask_secret(prompt: str) -> str:
    return getpass.getpass(f"{prompt}: ")


def confirm(prompt: str, default: bool = True) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    try:
        answer = input(f"{prompt} {suffix} ").strip().lower()
    except EOFError:
        return default
    if not answer:
        return default
    return answer in ("y", "yes")


def ask_choice(prompt: str, options: list[tuple[str, str]], default: int = 1) -> str:
    """options: list of (value, label). Returns the value."""
    print()
    print(bold(prompt))
    for i, (_value, label) in enumerate(options, 1):
        marker = cyan("›") if i == default else " "
        print(f"  {marker} {i}. {label}")
    while True:
        raw = ask("Choose", str(default))
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1][0]
        # allow the value itself
        for value, _label in options:
            if raw == value:
                return value
        warn(f"Enter a number between 1 and {len(options)}.")


def table(headers: list[str], rows: list[list[str]]) -> None:
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))
    print("  " + "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)))
    print("  " + "  ".join("─" * w for w in widths))
    for row in rows:
        print("  " + "  ".join(str(c).ljust(widths[i]) for i, c in enumerate(row)))


def kv(pairs: list[tuple[str, str]], indent: str = "  ") -> None:
    width = max((len(k) for k, _ in pairs), default=0)
    for key, value in pairs:
        print(f"{indent}{dim(key.ljust(width))}  {value}")


def json_out(obj) -> None:
    print(json.dumps(obj, indent=2, sort_keys=True, default=str))


def die(msg: str, code: int = 1) -> NoReturn:
    fail(msg)
    raise SystemExit(code)
