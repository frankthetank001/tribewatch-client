"""Client-side Discord OAuth flow for obtaining a client token.

Opens the user's browser to the server's /auth/client-login endpoint,
runs a tiny local HTTP server to capture the token from the callback,
and returns the signed token string automatically.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

log = logging.getLogger(__name__)

_DEFAULT_LOCAL_PORT = 19283


class _TokenHandler(BaseHTTPRequestHandler):
    """HTTP handler that captures the token from the server's redirect."""

    token: str = ""
    _event: threading.Event | None = None

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/callback":
            params = parse_qs(parsed.query)
            token = params.get("token", [""])[0]
            if token:
                _TokenHandler.token = token
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(
                    b"<!DOCTYPE html><html><head><title>TribeWatch</title>"
                    b"<style>"
                    b"body{font-family:sans-serif;background:#1a1a2e;color:#e0e0e0;"
                    b"display:flex;justify-content:center;align-items:center;min-height:100vh;margin:0}"
                    b".card{background:#16213e;padding:2rem;border-radius:8px;max-width:500px;width:90%;"
                    b"box-shadow:0 4px 20px rgba(0,0,0,.5)}"
                    b"h2{color:#4ecca3;margin-top:0}"
                    b"p{line-height:1.5}"
                    b"</style></head><body>"
                    b"<div class='card'>"
                    b"<h2>Success!</h2>"
                    b"<p>TribeWatch has been authenticated. You can close this tab.</p>"
                    b"</div></body></html>"
                )
                if self._event:
                    self._event.set()
                return

        self.send_response(404)
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        """Suppress default HTTP server logging."""
        pass


async def obtain_client_token_interactive(
    server_url: str,
    *,
    local_port: int = _DEFAULT_LOCAL_PORT,
    timeout: float = 120.0,
) -> str:
    """Open browser for OAuth, capture token automatically via localhost redirect.

    1. Starts a tiny HTTP server on localhost:{local_port}
    2. Opens browser to server's /auth/client-login?port={local_port}
    3. User authenticates with Discord
    4. Server redirects token to http://localhost:{local_port}/callback?token=...
    5. Local server captures it and returns

    Falls back to manual paste prompt if the localhost callback times out.
    """
    url = server_url.rstrip("/")
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"

    # Reset state
    _TokenHandler.token = ""
    event = threading.Event()
    _TokenHandler._event = event

    # Start local HTTP server in a thread
    try:
        server = HTTPServer(("127.0.0.1", local_port), _TokenHandler)
    except OSError:
        log.warning("Could not bind localhost:%d, falling back to manual paste", local_port)
        return await _fallback_paste_prompt(url)

    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    login_url = f"{url}/api/v1/auth/client-login?port={local_port}"
    log.info("Opening browser for Discord OAuth: %s", login_url)
    webbrowser.open(login_url)

    print("\nOpening browser for Discord authentication...")
    print("Waiting for authorization...")

    # Wait for the callback
    got_token = await asyncio.get_event_loop().run_in_executor(
        None, lambda: event.wait(timeout),
    )

    if got_token and _TokenHandler.token:
        token = _TokenHandler.token
        log.info("Client token received (len=%d)", len(token))
        # Shut down server in a background thread to avoid blocking the event loop
        threading.Thread(target=server.shutdown, daemon=True).start()
        return token

    # Timeout — fall back to manual paste
    log.warning("Automatic token capture timed out, falling back to manual paste")
    threading.Thread(target=server.shutdown, daemon=True).start()
    return await _fallback_paste_prompt(url)


async def _fallback_paste_prompt(server_url: str) -> str:
    """Fall back to prompting the user to paste the token manually."""
    import sys

    # Re-open browser without port param (shows token on page for copying)
    login_url = f"{server_url}/api/v1/auth/client-login"
    webbrowser.open(login_url)

    if sys.platform == "win32":
        return await _win32_paste_prompt()
    return await asyncio.get_event_loop().run_in_executor(
        None, _console_paste_prompt,
    )


def _console_paste_prompt() -> str:
    """Console-based prompt for pasting the client token."""
    print("\n" + "=" * 60)
    print("A browser window has been opened for Discord authentication.")
    print("After authenticating, copy the token from the page and paste it below.")
    print("=" * 60)
    return input("\nPaste client token: ").strip()


async def _win32_paste_prompt() -> str:
    """Win32 VBScript InputBox prompt for pasting the client token."""
    import os
    import subprocess
    import tempfile

    vbs_content = (
        'token = InputBox("A browser window has opened for Discord authentication." & vbCrLf & vbCrLf & '
        '"After authenticating, copy the token from the page and paste it here:", '
        '"TribeWatch - Client Token", "")\n'
        'If token <> "" Then\n'
        '    Dim fso, f\n'
        '    Set fso = CreateObject("Scripting.FileSystemObject")\n'
        '    Set f = fso.CreateTextFile(WScript.Arguments(0), True)\n'
        '    f.Write token\n'
        '    f.Close\n'
        'End If\n'
    )

    loop = asyncio.get_event_loop()

    def _run_vbs() -> str:
        with tempfile.NamedTemporaryFile(suffix=".vbs", delete=False, mode="w") as vbs:
            vbs.write(vbs_content)
            vbs_path = vbs.name
        out_path = vbs_path + ".out"
        try:
            subprocess.run(
                ["cscript", "//Nologo", vbs_path, out_path],
                check=False,
                timeout=300,
            )
            if os.path.exists(out_path):
                with open(out_path) as f:
                    return f.read().strip()
            return ""
        finally:
            for p in (vbs_path, out_path):
                try:
                    os.unlink(p)
                except OSError:
                    pass

    return await loop.run_in_executor(None, _run_vbs)
