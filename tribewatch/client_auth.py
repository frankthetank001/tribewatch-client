"""Client-side Discord OAuth flow for obtaining a client token.

Supports three authentication methods (tried in order):
1. Device flow — display QR code / URL, poll server until user completes OAuth
2. Localhost callback — open browser, capture token via local HTTP server
3. Manual paste — show token on page for user to copy/paste
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


# ---------------------------------------------------------------------------
# Device-flow authentication
# ---------------------------------------------------------------------------

def _display_device_instructions(verification_url: str, user_code: str) -> None:
    """Print verification URL, user code, and optional QR code to terminal."""
    print()
    print("=" * 60)
    print("  TribeWatch Device Authentication")
    print("=" * 60)
    print()
    print("  Open this URL on any device:")
    print(f"    {verification_url}")
    print()

    try:
        import qrcode  # type: ignore[import-untyped]
        qr = qrcode.QRCode(border=1)
        qr.add_data(verification_url)
        qr.make(fit=True)
        qr.print_tty()
    except ImportError:
        print("  (Install 'qrcode' package for QR code display)")
    except Exception:
        log.debug("QR code rendering failed", exc_info=True)

    print()
    print(f"  Your code: {user_code}")
    print()
    print("  Waiting for authorization...")
    print("=" * 60)


async def obtain_client_token_device(
    server_url: str,
    *,
    tribe_hint: str = "",
    timeout: float = 300.0,
    poll_interval: float = 5.0,
) -> str:
    """Device-flow OAuth: request codes, display QR + URL, poll for token.

    Returns the client token string, or "" on timeout/failure.
    """
    import aiohttp
    from tribewatch.http import make_session

    url = server_url.rstrip("/")
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"

    # Step 1: Request device code from server
    log.info("Requesting device code from %s", url)
    async with make_session() as session:
        try:
            async with session.post(
                f"{url}/api/v1/auth/device",
                json={"tribe_hint": tribe_hint},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    log.debug("Device auth request returned %s", resp.status)
                    return ""
                data = await resp.json()
        except Exception:
            log.debug("Device auth request failed", exc_info=True)
            return ""

    device_code = data.get("device_code", "")
    user_code = data.get("user_code", "")
    verification_url = data.get("verification_url", "")
    expires_in = data.get("expires_in", 300)
    interval = data.get("interval", poll_interval)

    if not device_code or not verification_url:
        return ""

    # Step 2: Display instructions + QR, and auto-open the browser
    _display_device_instructions(verification_url, user_code)
    try:
        log.info("Opening browser to %s", verification_url)
        webbrowser.open(verification_url)
    except Exception:
        log.debug("Failed to auto-open browser for device flow", exc_info=True)

    # Step 3: Poll until complete, expired, or timeout
    deadline = asyncio.get_event_loop().time() + min(timeout, expires_in)
    async with make_session() as session:
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(interval)
            try:
                async with session.get(
                    f"{url}/api/v1/auth/device/poll",
                    params={"device_code": device_code},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    result = await resp.json()
                    status = result.get("status")
                    if status == "complete":
                        token = result.get("client_token", "")
                        if token:
                            log.info("Device flow: token received (len=%d)", len(token))
                            print("\n  Authentication successful!")
                            return token
                    elif status == "expired":
                        log.warning("Device code expired")
                        print("\n  Device code expired.")
                        return ""
                    # status == "pending" → keep polling
            except Exception:
                log.debug("Device poll error", exc_info=True)

    log.warning("Device flow timed out after %.0fs", timeout)
    print("\n  Device flow timed out.")
    return ""


# ---------------------------------------------------------------------------
# Localhost callback flow (existing)
# ---------------------------------------------------------------------------

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
                self.send_header("Access-Control-Allow-Origin", "*")
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


async def _localhost_callback_flow(
    server_url: str,
    *,
    local_port: int = _DEFAULT_LOCAL_PORT,
    timeout: float = 120.0,
    tribe_hint: str = "",
) -> str:
    """Open browser for OAuth, capture token automatically via localhost redirect.

    Returns the token string, or "" on timeout/failure.
    """
    url = server_url.rstrip("/")
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"

    _TokenHandler.token = ""
    event = threading.Event()
    _TokenHandler._event = event

    try:
        server = HTTPServer(("127.0.0.1", local_port), _TokenHandler)
    except OSError:
        log.warning("Could not bind localhost:%d", local_port)
        return ""

    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    login_url = f"{url}/api/v1/auth/client-login?port={local_port}"
    if tribe_hint:
        from urllib.parse import quote
        login_url += f"&tribe_hint={quote(tribe_hint)}"
    log.info("Opening browser for Discord OAuth: %s", login_url)
    webbrowser.open(login_url)

    print("\nOpening browser for Discord authentication...")
    print("Waiting for authorization...")

    got_token = await asyncio.get_event_loop().run_in_executor(
        None, lambda: event.wait(timeout),
    )

    threading.Thread(target=server.shutdown, daemon=True).start()

    if got_token and _TokenHandler.token:
        token = _TokenHandler.token
        log.info("Client token received via localhost callback (len=%d)", len(token))
        return token

    return ""


# ---------------------------------------------------------------------------
# Manual paste fallback
# ---------------------------------------------------------------------------

async def _fallback_paste_prompt(server_url: str) -> str:
    """Fall back to prompting the user to paste the token manually."""
    import sys

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


# ---------------------------------------------------------------------------
# Main entry point — tries methods in order
# ---------------------------------------------------------------------------

async def obtain_client_token_interactive(
    server_url: str,
    *,
    local_port: int = _DEFAULT_LOCAL_PORT,
    timeout: float = 120.0,
    tribe_hint: str = "",
) -> str:
    """Obtain a client token interactively. Tries in order:

    1. Device flow (QR code / URL + polling)
    2. Localhost callback (browser + local HTTP server)
    3. Manual paste (browser + copy/paste prompt)
    """
    url = server_url.rstrip("/")
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"

    # 1. Try device flow first (works headless, on any device)
    try:
        token = await obtain_client_token_device(
            url, tribe_hint=tribe_hint, timeout=300.0,
        )
        if token:
            return token
        log.info("Device flow returned no token, trying localhost callback")
    except Exception:
        log.info("Device flow unavailable, trying localhost callback", exc_info=True)

    # 2. Try localhost callback (opens browser on same machine)
    print("\nFalling back to browser authentication...")
    token = await _localhost_callback_flow(
        url, local_port=local_port, timeout=timeout, tribe_hint=tribe_hint,
    )
    if token:
        return token

    # 3. Fall back to manual paste
    log.warning("Localhost callback failed, falling back to manual paste")
    return await _fallback_paste_prompt(url)
