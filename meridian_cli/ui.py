"""Terminal UI helpers for the Meridian Beta CLI.

Stdlib only: the CLI has to work before (and without) the app's virtualenv
being healthy, so it must not import rich/click.

The visual language is deliberately restrained: monochrome, thin rules, generous
space, and one ASCII mark. Colour is used only to distinguish state (ok / warn /
fail), never as decoration. Every component degrades to plain ASCII when the
terminal is not a UTF-8 TTY or NO_COLOR is set.
"""

from __future__ import annotations

import getpass
import json
import os
import shutil
import sys
from typing import NoReturn

_COLOR = sys.stdout.isatty() and os.getenv("NO_COLOR") is None
_UTF8 = "utf" in (sys.stdout.encoding or "").lower()
QUIET = False

_RESET = "\033[0m" if _COLOR else ""
_BOLD = "\033[1m" if _COLOR else ""
_FAINT = "\033[2m" if _COLOR else ""
_INVERSE = "\033[7m" if _COLOR else ""
_INK = "\033[39m" if _COLOR else ""
_MUTED = "\033[90m" if _COLOR else ""
_GREEN = "\033[32m" if _COLOR else ""
_YELLOW = "\033[33m" if _COLOR else ""
_RED = "\033[31m" if _COLOR else ""

_RULE = "─" if _UTF8 else "-"
_MARK = "›" if _UTF8 else ">"
_CHECK = "✓" if _UTF8 else "ok"
_WARN = "!" if _UTF8 else "!"
_CROSS = "✗" if _UTF8 else "x"


def _width(default: int = 76) -> int:
    try:
        cols = shutil.get_terminal_size((80, 24)).columns
    except OSError:
        cols = 80
    return max(44, min(cols - 2, default))


def ink(s: str) -> str:
    return f"{_INK}{s}{_RESET}" if _COLOR else s


def bold(s: str) -> str:
    return f"{_BOLD}{s}{_RESET}"


def dim(s: str) -> str:
    return f"{_FAINT}{s}{_RESET}"


def faint(s: str) -> str:
    return f"{_FAINT}{_MUTED}{s}{_RESET}" if _COLOR else s


def label(s: str) -> str:
    return f"{_MUTED}{s.upper()}{_RESET}" if _COLOR else s.upper()


def cyan(s: str) -> str:
    """Compatibility alias: the palette is monochrome, not cyan."""
    return ink(s)


def green(s: str) -> str:
    return f"{_GREEN}{s}{_RESET}" if _COLOR else s


def yellow(s: str) -> str:
    return f"{_YELLOW}{s}{_RESET}" if _COLOR else s


def red(s: str) -> str:
    return f"{_RED}{s}{_RESET}" if _COLOR else s


def reverse(s: str) -> str:
    return f"{_INVERSE}{s}{_RESET}" if _COLOR else f"[ {s.strip()} ]"


def rule(ch: str = _RULE, width: int | None = None) -> str:
    return faint(ch * (width or _width()))


def hero(title: str = "MERIDIAN BETA", subtitle: str = "private portfolio desk",
         meta: str = "self-hosted · beta") -> None:
    """The one ceremonial mark: dithered smoke, a target, and the wordmark."""
    if QUIET:
        return
    print()
    if not _UTF8:
        print(f"  {bold(title.upper())}")
        print(f"  {dim(subtitle)}")
        print(f"  {faint(meta)}")
        print()
        return

    for char, length in (("░", 34), ("▒", 24), ("▓", 14)):
        print("  " + faint(char * length))
    print()
    art = [
        "    ╭─────╮",
        "   ╱ ╭───╮ ╲",
        "  │ ╱ ╭─╮ ╲ │",
        "  │ │ │●│ │ │",
        "  │ ╲ ╰─╯ ╱ │",
        "   ╲ ╰───╯ ╱",
        "    ╰─────╯",
    ]
    word = "   ".join(" ".join(part) for part in title.upper().split())
    copy = [
        bold(word),
        dim(subtitle),
        dim("one operator · one agent"),
        faint(meta),
    ]
    copy_at = {0: 0, 2: 1, 4: 2, 6: 3}
    for i, left in enumerate(art):
        right = copy[copy_at[i]] if i in copy_at else ""
        print("  " + faint(left.ljust(17)) + right)
    print()


def banner(title: str, subtitle: str = "") -> None:
    if QUIET:
        return
    print()
    print(f"  {bold(title.upper())}")
    if subtitle:
        for part in subtitle.splitlines():
            print(f"  {dim(part)}")
    print("  " + faint(_RULE * min(_width(), max(24, len(title) + 8))))
    print()


def step(msg: str) -> None:
    if QUIET:
        return
    print(f"  {faint(_MARK)} {msg}")


def ok(msg: str) -> None:
    if QUIET:
        return
    print(f"  {green(_CHECK)} {msg}")


def warn(msg: str) -> None:
    if not QUIET:
        print(f"  {yellow(_WARN)} {msg}")


def fail(msg: str) -> None:
    print(f"  {red(_CROSS)} {msg}", file=sys.stderr)


def info(msg: str) -> None:
    if QUIET:
        return
    print(f"  {dim(msg)}")


def ask(prompt: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    try:
        answer = input(f"  {faint(_MARK)} {prompt}{suffix}: ").strip()
    except EOFError:
        return default or ""
    return answer or (default or "")


def ask_secret(prompt: str) -> str:
    return getpass.getpass(f"  {prompt}: ")


def confirm(prompt: str, default: bool = True) -> bool:
    # The visual is the image's binary action: a ghost deny and a solid approve.
    if _COLOR and sys.stdin.isatty():
        deny = reverse(" deny ") if not default else " deny "
        approve = reverse(" approve ") if default else " approve "
        print()
        print(f"  {deny}   {approve}")
    suffix = "[Y/n]" if default else "[y/N]"
    try:
        answer = input(f"  {prompt} {suffix} {faint(_MARK)} ").strip().lower()
    except EOFError:
        return default
    if not answer:
        return default
    return answer in ("y", "yes")


def ask_choice(prompt: str, options: list[tuple[str, str]], default: int = 1) -> str:
    """options: list of (value, label). Returns the value."""
    print()
    print(f"  {label(prompt)}")
    print("  " + faint(_RULE * min(_width(), max(24, len(prompt) + 2))))
    for i, (_value, text) in enumerate(options, 1):
        marker = ink(_MARK) if i == default else " "
        number = f"{i:02d}"
        print(f"  {marker} {dim(number)}  {text}")
    print()
    while True:
        raw = ask("choose", str(default))
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1][0]
        for value, _text in options:
            if raw == value:
                return value
        warn(f"Enter a number between 1 and {len(options)}.")


def table(headers: list[str], rows: list[list[str]]) -> None:
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))
    print("  " + "  ".join(dim(h.upper().ljust(widths[i])) for i, h in enumerate(headers)))
    print("  " + faint("  ".join(_RULE * width for width in widths)))
    for row in rows:
        print("  " + "  ".join(str(c).ljust(widths[i]) for i, c in enumerate(row)))


def kv(pairs: list[tuple[str, str]], indent: str = "  ") -> None:
    width = max((len(k) for k, _ in pairs), default=0)
    for key, value in pairs:
        print(f"{indent}{label(key.ljust(width))}  {value}")


def json_out(obj) -> None:
    print(json.dumps(obj, indent=2, sort_keys=True, default=str))


def die(msg: str, code: int = 1) -> NoReturn:
    fail(msg)
    raise SystemExit(code)
