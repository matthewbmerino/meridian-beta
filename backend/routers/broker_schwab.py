"""Schwab broker connection: authorize, callback, status.

The callback is the ONE unauthenticated path in this flow, because Schwab
redirects a plain browser to it, with no Meridian credential attached. It is
therefore guarded by a single-use, ten-minute state minted by ``/authorize``
(which itself sits behind the normal auth gate): a callback whose state this
app did not just issue is refused before any code is exchanged. The endpoint
never echoes the code or a token, and a replay of the same callback fails
because the state is spent before the exchange.

Why a public callback at all: the alternative (127.0.0.1) forces the operator to
copy an addressed-bar URL into a terminal every week. This route puts the code
on the box and exchanges it there, so reconnecting is: open one link, approve,
done.
"""

from __future__ import annotations

from html import escape

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse

router = APIRouter(prefix="/api/broker/schwab", tags=["broker"])

_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Meridian — Schwab</title>
<style>
  :root {{ color-scheme: dark; }}
  body {{ margin:0; min-height:100vh; display:grid; place-items:center;
         background:#0b0d10; color:#e8eaed;
         font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }}
  main {{ max-width:30rem; padding:2rem; text-align:center; }}
  h1 {{ font-size:1.25rem; margin:0 0 .5rem; }}
  p {{ color:#9aa0a6; margin:0 0 1.25rem; }}
  .ok {{ color:#5fd68a; }} .bad {{ color:#f28b82; }}
  a {{ display:inline-block; padding:.6rem 1rem; border-radius:.5rem;
      background:#1e2126; color:#e8eaed; text-decoration:none; }}
</style></head>
<body><main>
  <h1 class="{cls}">{title}</h1>
  <p>{body}</p>
  <a href="/settings">Back to Meridian</a>
</main></body></html>"""


def _page(title: str, body: str, ok: bool) -> str:
    return _PAGE.format(title=title, body=body, cls="ok" if ok else "bad")


@router.get("/authorize")
def authorize():
    """Start the OAuth flow: mint state, then bounce to Schwab."""
    from data import schwab

    try:
        state = schwab.begin_authorization()
        return RedirectResponse(schwab.authorize_url(state), status_code=307)
    except schwab.SchwabError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/status")
def status():
    """Connection state and days remaining. Never returns a token value."""
    from data import schwab

    return schwab.token_status()


@router.get("/callback", response_class=HTMLResponse)
def callback(code: str = "", state: str = "", error: str = ""):
    """Schwab's redirect target. Exempt from the auth gate — see module docstring."""
    from data import schwab

    if error:
        return HTMLResponse(
            # `error` is caller-controlled query text on an unauthenticated page.
            # Escaping it here is the XSS fix; the CSP header in nginx is the
            # second layer, because this page runs on the app's own origin where
            # script can reach every /api route with the operator's cookie.
            _page("Schwab declined", f"Schwab returned: {escape(error)}", ok=False),
            status_code=400,
            headers={"Cache-Control": "no-store"},
        )
    if not code:
        return HTMLResponse(
            _page(
                "No code received",
                "Schwab did not return an authorization code. Start again from Settings.",
                ok=False,
            ),
            status_code=400,
            headers={"Cache-Control": "no-store"},
        )

    try:
        schwab.complete_authorization(code, state)
    except schwab.SchwabError as exc:
        return HTMLResponse(
            _page("Could not connect", exc.args[0] if exc.args else "Unknown error", ok=False),
            status_code=400,
            headers={"Cache-Control": "no-store"},
        )

    status_after = schwab.token_status()
    return HTMLResponse(
        _page(
            "Schwab connected",
            f"Market data is authorized. {status_after['days_left']} day(s) before you need to reconnect.",
            ok=True,
        ),
        headers={"Cache-Control": "no-store"},
    )
