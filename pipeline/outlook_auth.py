"""
pipeline/outlook_auth.py
-------------------------
Vaulter AI Stage 2 — Outlook Authentication

Handles OAuth2 authentication with Microsoft Graph API.
Run via: python main.py auth

Uses MSAL PublicClientApplication with device code flow.
Token is cached in outlook_token.json automatically.

NEVER commit outlook_token.json to git.
"""

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from core import safe_io
from config import (
    OUTLOOK_CLIENT_ID,
    OUTLOOK_TENANT_ID,
    OUTLOOK_TOKEN_FILE,
)

import msal

log = logging.getLogger("vaulter.outlook_auth")

SCOPES = ["https://graph.microsoft.com/Mail.Read"]


def get_access_token(interactive: bool = False) -> str:
    """
    Return a valid Microsoft Graph access token, refreshing silently from
    the cache in outlook_token.json when possible.

    interactive=False (the default -- used by every automated caller: the
    scheduler thread, the MCP server, `python main.py email`) -- if there's
    no valid cached token, raises a clear RuntimeError instead of launching
    the device code flow. That flow prints a sign-in code to stdout and
    blocks indefinitely waiting for a human to complete browser sign-in --
    in a background thread with nobody watching, this hangs forever, and
    printing to stdout risks corrupting the MCP stdio transport to claude.ai.

    interactive=True (used only by `python main.py auth`, a human running
    a terminal command on purpose) -- launches the device code flow as
    before.
    """
    if not OUTLOOK_CLIENT_ID:
        raise ValueError(
            "OUTLOOK_CLIENT_ID not set.\n"
            "Add it to your .env file:\n"
            "  OUTLOOK_CLIENT_ID=your-application-id\n"
        )

    # Use a serializable token cache so tokens persist between runs
    cache = msal.SerializableTokenCache()
    if OUTLOOK_TOKEN_FILE.exists():
        try:
            cache.deserialize(OUTLOOK_TOKEN_FILE.read_text())
        except Exception as e:
            # A corrupt cache (e.g. a crash mid-write before this file used
            # atomic writes) must not crash this function -- treat it the
            # same as no cache at all, so the normal re-auth path below
            # (the RuntimeError telling the operator to run `python main.py
            # auth`, or the device flow itself when interactive=True) can
            # actually recover, instead of failing before ever reaching it.
            log.warning(f"Outlook token cache was corrupt and could not be read ({e}) -- "
                        f"treating as not signed in.")

    # PublicClientApplication — correct for device code flow
    app = msal.PublicClientApplication(
        client_id=OUTLOOK_CLIENT_ID,
        authority=f"https://login.microsoftonline.com/{OUTLOOK_TENANT_ID}",
        token_cache=cache,
    )

    # Try silent refresh first if we have cached accounts
    accounts = app.get_accounts()
    if accounts:
        result = app.acquire_token_silent(SCOPES, account=accounts[0])
        if result and "access_token" in result:
            _save_cache(cache)
            return result["access_token"]

    if not interactive:
        raise RuntimeError(
            "Outlook needs re-authorization — the cached token is missing, "
            "expired, or revoked, and this is an automated (non-interactive) "
            "call so it can't launch a sign-in prompt. Run `python main.py auth` "
            "to re-authorize; email ingestion will resume automatically after that."
        )

    # Launch device code flow — only reached when interactive=True (a human
    # running `python main.py auth` at a terminal on purpose).
    flow = app.initiate_device_flow(scopes=SCOPES)
    if "user_code" not in flow:
        raise RuntimeError(f"Device flow failed: {flow.get('error_description')}")

    print("\n" + "=" * 60)
    print("Open this URL in your browser and enter the code shown:")
    print(f"\n  {flow['verification_uri']}\n")
    print(f"  Code: {flow['user_code']}")
    print("=" * 60 + "\n")

    # Blocks until user approves in the browser
    result = app.acquire_token_by_device_flow(flow)

    if "access_token" not in result:
        raise RuntimeError(
            f"Auth failed: {result.get('error_description', result.get('error'))}"
        )

    _save_cache(cache)
    print(f"✓ Token saved to {OUTLOOK_TOKEN_FILE}")
    return result["access_token"]


def run_auth_flow() -> str:
    """
    Public entry point called by main.py auth command — the one place
    this is run by a human at a terminal, so it's the only caller that
    opts into the interactive device code flow.
    """
    return get_access_token(interactive=True)


def _save_cache(cache: msal.SerializableTokenCache):
    """Save token cache to disk if it changed. Writes atomically (temp
    file + rename) so a crash/kill mid-write can't leave a corrupt token
    cache -- which would otherwise be worse than no cache at all, since
    get_access_token() calls cache.deserialize() unconditionally before
    ever reaching the interactive re-auth fallback."""
    if cache.has_state_changed:
        safe_io.save_text_atomic(OUTLOOK_TOKEN_FILE, cache.serialize())
