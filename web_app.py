#!/usr/bin/env python3
"""
Web UI for the LinkedIn Sheet Agent.
Run with: python web_app.py
Then open http://localhost:8000
"""

import os
import re
import threading
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

from run_agent import (
    SCOPES, CREDENTIALS_PATH, TOKEN_PATH,
    load_config, get_sheet_client, run, check_bb_browser_available,
)

BASE_DIR = Path(__file__).parent
ENV_PATH = BASE_DIR / ".env"
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title="LinkedIn Sheet Agent")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

_run_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ENV_KEYS = [
    "GOOGLE_SHEET_ID", "SHEET_NAME", "URL_COLUMN",
    "DATA_START_ROW", "COMPANY_COLUMN", "TITLE_COLUMN",
]


def _read_env() -> dict:
    """Parse .env into a dict (only the keys we care about)."""
    values = {k: "" for k in ENV_KEYS}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                k = k.strip()
                if k in values:
                    values[k] = v.strip()
    return values


def _write_env(values: dict):
    """Write config values to .env, preserving comments for known keys."""
    lines = [
        "# Your Google Sheet ID (from the sheet URL: .../d/SHEET_ID/edit)",
        f"GOOGLE_SHEET_ID={values.get('GOOGLE_SHEET_ID', '')}",
        "",
        "# Sheet name (tab name) that contains the LinkedIn links",
        f"SHEET_NAME={values.get('SHEET_NAME', 'Sheet1')}",
        "",
        "# Column letter that has LinkedIn profile URLs",
        f"URL_COLUMN={values.get('URL_COLUMN', 'A')}",
        "",
        "# Row number where data starts (header row is often 1, data from 2)",
        f"DATA_START_ROW={values.get('DATA_START_ROW', '2')}",
        "",
        "# Target columns (optional — if not set, uses URL_COLUMN+1 and +2)",
        f"COMPANY_COLUMN={values.get('COMPANY_COLUMN', '')}",
        f"TITLE_COLUMN={values.get('TITLE_COLUMN', '')}",
        "",
    ]
    ENV_PATH.write_text("\n".join(lines))


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index():
    return (STATIC_DIR / "index.html").read_text()


@app.get("/api/config")
async def get_config():
    return _read_env()


@app.post("/api/config")
async def save_config(request: Request):
    body = await request.json()
    current = _read_env()
    for k in ENV_KEYS:
        if k in body:
            current[k] = str(body[k]).strip()
    _write_env(current)
    return {"ok": True}


@app.get("/api/auth/status")
async def auth_status():
    if not CREDENTIALS_PATH.exists():
        return {"authenticated": False, "reason": "no_credentials",
                "message": "credentials.json not found. Download it from Google Cloud Console."}
    if not TOKEN_PATH.exists():
        return {"authenticated": False, "reason": "no_token",
                "message": "Not connected. Click Connect to authorize."}
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request as GRequest
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
        if creds.expired and creds.refresh_token:
            creds.refresh(GRequest())
            TOKEN_PATH.write_text(creds.to_json())
        if creds.valid:
            return {"authenticated": True, "message": "Connected to Google."}
        return {"authenticated": False, "reason": "invalid_token",
                "message": "Token expired. Click Connect to re-authorize."}
    except Exception as e:
        return {"authenticated": False, "reason": "error", "message": str(e)[:200]}


@app.post("/api/auth/start")
async def auth_start(request: Request):
    """Start OAuth flow. Returns the authorization URL for the browser."""
    if not CREDENTIALS_PATH.exists():
        return JSONResponse({"error": "credentials.json not found"}, status_code=400)
    from google_auth_oauthlib.flow import Flow
    flow = Flow.from_client_secrets_file(
        str(CREDENTIALS_PATH),
        scopes=SCOPES,
        redirect_uri="http://localhost:8000/api/auth/callback",
    )
    auth_url, _ = flow.authorization_url(prompt="consent", access_type="offline")
    return {"auth_url": auth_url}


@app.get("/api/auth/callback")
async def auth_callback(request: Request):
    """Handle the OAuth redirect from Google."""
    code = request.query_params.get("code")
    if not code:
        return HTMLResponse("<h2>Authorization failed — no code received.</h2>", status_code=400)
    try:
        from google_auth_oauthlib.flow import Flow
        flow = Flow.from_client_secrets_file(
            str(CREDENTIALS_PATH),
            scopes=SCOPES,
            redirect_uri="http://localhost:8000/api/auth/callback",
        )
        flow.fetch_token(code=code)
        TOKEN_PATH.write_text(flow.credentials.to_json())
        return RedirectResponse("/?auth=success")
    except Exception as e:
        return HTMLResponse(f"<h2>Authorization failed</h2><pre>{e}</pre>", status_code=500)


@app.get("/api/sheet/preview")
async def sheet_preview():
    """Return first ~20 rows of the configured sheet."""
    try:
        config = load_config()
    except SystemExit as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    try:
        client = get_sheet_client()
        sh = client.open_by_key(config["sheet_id"])
        worksheet = sh.worksheet(config["sheet_name"])
        rows = worksheet.get_all_values()
        headers = rows[0] if rows else []
        data = rows[1:20] if len(rows) > 1 else []
        return {"headers": headers, "rows": data, "total_rows": len(rows) - 1}
    except Exception as e:
        return JSONResponse({"error": str(e)[:300]}, status_code=500)


@app.get("/api/status")
async def agent_status():
    """Pre-flight check: is bb-browser available?"""
    return {
        "bb_browser_installed": check_bb_browser_available(),
        "credentials_exist": CREDENTIALS_PATH.exists(),
        "token_exists": TOKEN_PATH.exists(),
    }


@app.post("/api/run")
async def run_agent(request: Request):
    """Run the scraping agent. Blocks until done, returns summary."""
    if not _run_lock.acquire(blocking=False):
        return JSONResponse({"error": "A scraping run is already in progress."}, status_code=409)
    try:
        body = await request.json() if await request.body() else {}
        resume = body.get("resume", False)
        limit = body.get("limit")

        config = load_config()
        summary = run(
            config,
            use_bb_browser=True,
            resume=resume,
            limit=limit,
        )
        return summary
    except Exception as e:
        return JSONResponse({"error": str(e)[:500]}, status_code=500)
    finally:
        _run_lock.release()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    STATIC_DIR.mkdir(exist_ok=True)
    print("\n  LinkedIn Sheet Agent — Web UI")
    print("  Open http://localhost:8000 in your browser\n")
    uvicorn.run(app, host="0.0.0.0", port=8000)
