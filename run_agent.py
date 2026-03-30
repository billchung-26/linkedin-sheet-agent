#!/usr/bin/env python3
"""
LinkedIn Sheet Agent: reads LinkedIn profile URLs from a Google Sheet,
visits each profile to extract title and company, then writes results
back to new columns in the sheet.

Supports two scraping backends:
  --bb-browser (default): Uses bb-browser to scrape via your real Chrome session.
                          No new window, no login needed — uses your existing login.
  --playwright:           Falls back to Playwright (opens a separate browser).
"""

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional, Tuple

import gspread
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request

# ---------------------------------------------------------------------------
# Config (from env or defaults)
# ---------------------------------------------------------------------------
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
CREDENTIALS_PATH = Path(__file__).parent / "credentials.json"
TOKEN_PATH = Path(__file__).parent / "token.json"
BROWSER_DATA_DIR = Path(__file__).parent / "browser-data"
VIEWPORT = {"width": 1280, "height": 800}
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def load_config():
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
    sheet_id = os.getenv("GOOGLE_SHEET_ID")
    sheet_name = os.getenv("SHEET_NAME", "Sheet1")
    url_col = os.getenv("URL_COLUMN", "A")
    start_row = int(os.getenv("DATA_START_ROW", "2"))
    company_col = os.getenv("COMPANY_COLUMN")
    title_col = os.getenv("TITLE_COLUMN")
    if not sheet_id:
        raise SystemExit(
            "Set GOOGLE_SHEET_ID in .env (from your sheet URL: .../d/SHEET_ID/edit)"
        )
    return {
        "sheet_id": sheet_id,
        "sheet_name": sheet_name,
        "url_column": url_col.upper(),
        "data_start_row": start_row,
        "company_column": company_col,
        "title_column": title_col,
    }


def get_sheet_client():
    creds = None
    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CREDENTIALS_PATH.exists():
                raise SystemExit(
                    f"Place your Google OAuth credentials file at {CREDENTIALS_PATH}\n"
                    "See README for how to create it."
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                str(CREDENTIALS_PATH), SCOPES
            )
            creds = flow.run_local_server(port=0)
        with open(TOKEN_PATH, "w") as f:
            f.write(creds.to_json())
    return gspread.authorize(creds)


def ensure_columns(worksheet, url_col: str, company_col_override=None, title_col_override=None):
    """Return (title_col_letter, company_col_letter). Use overrides if specified."""
    if title_col_override and company_col_override:
        return title_col_override.upper(), company_col_override.upper()

    row1 = worksheet.row_values(1)
    if not row1:
        return "B", "C"
    col_idx = gspread.utils.a1_to_rowcol(f"{url_col}1")[1]
    title_col = gspread.utils.rowcol_to_a1(1, col_idx + 1)
    company_col = gspread.utils.rowcol_to_a1(1, col_idx + 2)
    headers = [c.strip().lower() for c in row1]
    if "title" not in headers:
        worksheet.update_acell(title_col, "Title")
    if "company" not in headers:
        worksheet.update_acell(company_col, "Company")
    return title_col[0], company_col[0]


def normalize_linkedin_url(url: str) -> Optional[str]:
    """Return canonical profile URL or None if invalid."""
    if not url or not isinstance(url, str):
        return None
    url = url.strip()
    if not url.startswith("http"):
        url = "https://" + url
    # Allow linkedin.com/in/username
    m = re.search(r"linkedin\.com/in/([a-zA-Z0-9\-]+)", url)
    if m:
        return f"https://www.linkedin.com/in/{m.group(1)}"
    return None


def extract_title_company(page) -> Tuple[str, str]:
    """
    Extract title and company from LinkedIn profile page via Playwright DOM parsing.
    Tries several selectors; LinkedIn's DOM changes often.
    """
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    title, company = "", ""

    selectors_headline = [
        "div.ph5 .text-body-medium.break-words",
        ".top-card-layout__headline",
        "div[class*='headline']",
        "h2.top-card-layout__headline",
        ".pv-top-card--list li:first-child",
    ]
    for sel in selectors_headline:
        try:
            el = page.locator(sel).first
            if el.count() > 0:
                text = el.inner_text(timeout=3000).strip()
                if text and len(text) < 500:
                    title, company = parse_headline(text)
                    if title or company:
                        return title, company
        except (PlaywrightTimeout, Exception):
            continue

    exp_selectors = [
        "#experience ~ div section ul li",
        "section[data-section='experience'] ul li",
        ".pv-profile-section__section-info li",
    ]
    for sel in exp_selectors:
        try:
            items = page.locator(sel).all()
            for item in items[:3]:
                try:
                    t_el = item.locator("h3, span[aria-hidden='true']").first
                    if t_el.count() > 0:
                        title = (title or "") or t_el.inner_text(timeout=2000).strip()
                    c_el = item.locator(
                        "span[class*='company'], a[href*='/company/']"
                    ).first
                    if c_el.count() > 0:
                        company = (company or "") or c_el.inner_text(timeout=2000).strip()
                    if title and company:
                        return title[:500], company[:500]
                except Exception:
                    continue
        except Exception:
            continue

    return title or "", company or ""


def parse_headline(text: str) -> Tuple[str, str]:
    """Parse 'Title at Company' or 'Title | Company' style headline."""
    text = (text or "").strip()
    if not text:
        return "", ""
    # "Title at Company"
    if " at " in text:
        parts = text.split(" at ", 1)
        return parts[0].strip()[:500], (parts[1].strip()[:500] if len(parts) > 1 else "")
    # "Title @ Company"
    if " @ " in text:
        parts = text.split(" @ ", 1)
        return parts[0].strip()[:500], (parts[1].strip()[:500] if len(parts) > 1 else "")
    # "Title | Company" or "Title – Company"
    for sep in [" | ", " – ", " - ", " · "]:
        if sep in text:
            parts = text.split(sep, 1)
            return parts[0].strip()[:500], (parts[1].strip()[:500] if len(parts) > 1 else "")
    return text[:500], ""


# ---------------------------------------------------------------------------
# bb-browser backend
# ---------------------------------------------------------------------------

def extract_username_from_url(url: str) -> Optional[str]:
    """Extract the LinkedIn username from a profile URL."""
    m = re.search(r"linkedin\.com/in/([a-zA-Z0-9\-]+)", url)
    return m.group(1) if m else None


def check_bb_browser_available() -> bool:
    """Return True if bb-browser CLI is installed and on PATH."""
    return shutil.which("bb-browser") is not None


EXPERIENCE_JS = r"""(async function(username){
  var csrf=document.cookie.split(';').map(function(c){return c.trim()})
    .find(function(c){return c.startsWith('JSESSIONID=')});
  if(!csrf) return JSON.stringify({error:'Not logged in',hint:'Open LinkedIn in Chrome'});
  csrf=csrf.split('=')[1].replace(/"/g,'');
  var h={'csrf-token':csrf,'x-restli-protocol-version':'2.0.0'};
  var r=await fetch('/voyager/api/identity/dash/profiles?q=memberIdentity&memberIdentity='
    +encodeURIComponent(username)
    +'&decorationId=com.linkedin.voyager.dash.deco.identity.profile.FullProfileWithEntities-93',
    {headers:h,credentials:'include'});
  if(!r.ok) return JSON.stringify({error:'HTTP '+r.status,hint:r.status===403?'Profile restricted':'Check username'});
  var d=await r.json();
  var p=d.elements&&d.elements[0];
  if(!p) return JSON.stringify({error:'Profile not found'});
  var groups=p.profilePositionGroups&&p.profilePositionGroups.elements;
  var title='',company='';
  if(groups&&groups.length>0){
    var g=groups[0];
    company=g.companyName||'';
    var positions=g.profilePositionInPositionGroup&&g.profilePositionInPositionGroup.elements;
    if(positions&&positions.length>0){
      title=positions[0].title||'';
      if(!company) company=positions[0].companyName||'';
    }
  }
  var headline=p.multiLocaleHeadline&&p.multiLocaleHeadline.en_US||p.headline||'';
  return JSON.stringify({title:title,company:company,headline:headline,
    firstName:p.firstName||'',lastName:p.lastName||''});
})('__USERNAME__')"""


def scrape_with_bb_browser(url: str) -> Tuple[str, str]:
    """
    Use bb-browser to fetch a LinkedIn profile's first Experience entry
    via LinkedIn's internal Voyager API in your real Chrome session.
    Returns (job_title, company).
    """
    username = extract_username_from_url(url)
    if not username:
        return "(invalid URL)", "(invalid URL)"

    js_code = EXPERIENCE_JS.replace("__USERNAME__", username)

    try:
        result = subprocess.run(
            ["bb-browser", "eval", js_code, "--json"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            err_msg = (result.stderr or result.stdout or "unknown error").strip()
            if "no page target" in err_msg.lower():
                return "(bb-browser: no page target — open LinkedIn in Chrome)", "(error)"
            return f"(bb-browser error: {err_msg[:120]})", "(error)"

        output = result.stdout.strip()
        if not output:
            return "(empty response)", "(empty response)"

        raw = json.loads(output)
        inner = raw.get("data", {}).get("result", "")
        if not inner:
            return "(empty result)", "(empty result)"

        data = json.loads(inner)

        if "error" in data:
            hint = data.get("hint", "")
            return f"(error: {data['error']} {hint})"[:500], "(error)"

        title = data.get("title", "")
        company = data.get("company", "")

        if title or company:
            return title or "(not found)", company or "(not found)"

        headline = data.get("headline", "")
        if headline:
            return parse_headline(headline)

        return "(not found)", "(not found)"

    except subprocess.TimeoutExpired:
        return "(bb-browser timeout)", "(timeout)"
    except (json.JSONDecodeError, KeyError) as e:
        return f"(parse error: {str(e)[:80]})", "(error)"
    except FileNotFoundError:
        return "(bb-browser not installed)", "(not installed)"
    except Exception as e:
        return f"(error: {str(e)[:120]})", "(error)"


# ---------------------------------------------------------------------------
# Playwright backend (fallback)
# ---------------------------------------------------------------------------

NAV_TIMEOUT_MS = 45000
NAV_RETRY_DELAY_SEC = 5


def scrape_with_playwright(
    page, url: str, headless: bool = True, delay_seconds: float = 2.0
) -> Tuple[str, str]:
    """Navigate to LinkedIn profile and return (title, company). Retries once on timeout."""
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    last_error = None
    for attempt in range(2):
        try:
            page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=NAV_TIMEOUT_MS,
            )
            time.sleep(delay_seconds)
            title, company = extract_title_company(page)
            return title or "(not found)", company or "(not found)"
        except Exception as e:
            last_error = e
            if "timeout" in str(e).lower() or "Timeout" in str(e):
                if attempt == 0:
                    time.sleep(NAV_RETRY_DELAY_SEC)
                    continue
            return f"(error: {str(last_error)[:120]})", "(error)"
    return f"(error: {str(last_error)[:120]})", "(error)"


def _launch_browser(playwright, use_chrome: bool, headless: bool):
    if use_chrome:
        headless = False
        BROWSER_DATA_DIR.mkdir(parents=True, exist_ok=True)
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(BROWSER_DATA_DIR),
            channel="chrome",
            headless=False,
            viewport=VIEWPORT,
            user_agent=USER_AGENT,
            accept_downloads=False,
        )
        return context, lambda: context.close()
    browser = playwright.chromium.launch(headless=headless)
    context = browser.new_context(viewport=VIEWPORT, user_agent=USER_AGENT)

    def close_both():
        try:
            context.close()
        except Exception:
            pass
        try:
            browser.close()
        except Exception:
            pass
    return context, close_both


def _chrome_login_pause(context):
    page = context.new_page()
    try:
        page.goto("https://www.linkedin.com", wait_until="domcontentloaded", timeout=15000)
    except Exception:
        pass
    print("\n  >>> Log in to LinkedIn in the browser if needed.")
    print("  >>> When you're done, come back here and press Enter to continue...\n")
    try:
        input()
    except (EOFError, KeyboardInterrupt):
        print("(continuing)")
    return page


def _run_bb_browser(config, valid, start_row, title_col, company_col, worksheet):
    """Scrape profiles using bb-browser. Returns list of per-row result dicts."""
    if not check_bb_browser_available():
        raise RuntimeError(
            "bb-browser is not installed. Install it with:\n"
            "  npm install -g bb-browser\n"
            "Then install the Chrome extension and run the daemon.\n"
            "See: https://github.com/epiral/bb-browser"
        )

    print("\n  Using bb-browser (your real Chrome session).")
    print("  Make sure:")
    print("    1. Chrome extension is installed (from bb-browser releases)")
    print("    2. Daemon is running: bb-browser daemon")
    print("    3. You are logged into LinkedIn in Chrome\n")

    results = []
    for idx, url in valid:
        row = start_row + idx
        username = extract_username_from_url(url)
        print(f"Row {row}: {url}  (username: {username})")
        title, company = scrape_with_bb_browser(url)
        print(f"  -> Title: {title}  |  Company: {company}")
        worksheet.update_acell(f"{title_col}{row}", title)
        worksheet.update_acell(f"{company_col}{row}", company)
        is_error = title.startswith("(") and ("error" in title.lower() or "timeout" in title.lower())
        results.append({
            "row": row, "url": url, "title": title, "company": company,
            "status": "error" if is_error else "success",
        })
        time.sleep(2)
    return results


def _run_playwright(config, valid, start_row, title_col, company_col, worksheet,
                    headless, use_chrome):
    """Scrape profiles using Playwright. Returns list of per-row result dicts."""
    from playwright.sync_api import sync_playwright

    results = []
    with sync_playwright() as p:
        context, close_fn = _launch_browser(p, use_chrome=use_chrome, headless=headless)
        try:
            if use_chrome:
                page = _chrome_login_pause(context)
            else:
                page = context.new_page()
            for idx, url in valid:
                row = start_row + idx
                print(f"Row {row}: {url}")
                title, company = scrape_with_playwright(
                    page, url, headless=headless, delay_seconds=2.5
                )
                worksheet.update_acell(f"{title_col}{row}", title)
                worksheet.update_acell(f"{company_col}{row}", company)
                is_error = title.startswith("(") and ("error" in title.lower() or "timeout" in title.lower())
                results.append({
                    "row": row, "url": url, "title": title, "company": company,
                    "status": "error" if is_error else "success",
                })
                time.sleep(4)
        finally:
            try:
                close_fn()
            except Exception:
                pass
    return results


def run(
    config: dict,
    headless: bool = True,
    dry_run: bool = False,
    resume: bool = False,
    use_chrome: bool = False,
    use_bb_browser: bool = True,
    limit: Optional[int] = None,
) -> dict:
    """Run the scraping pipeline. Returns a summary dict:
    {"total", "success", "errors", "skipped", "results": [...]}.
    """
    client = get_sheet_client()
    sh = client.open_by_key(config["sheet_id"])
    worksheet = sh.worksheet(config["sheet_name"])

    url_col = config["url_column"]
    start_row = config["data_start_row"]
    title_col, company_col = ensure_columns(
        worksheet, url_col,
        company_col_override=config.get("company_column"),
        title_col_override=config.get("title_column"),
    )

    col_idx = gspread.utils.a1_to_rowcol(f"{url_col}1")[1]
    col_values = worksheet.col_values(col_idx)
    urls = [""] * max(0, len(col_values) - (start_row - 1))
    for i in range(start_row - 1, len(col_values)):
        urls[i - (start_row - 1)] = col_values[i]
    urls = [normalize_linkedin_url(u) for u in urls]
    valid = [(i, u) for i, u in enumerate(urls) if u]
    total_urls = len(col_values) - (start_row - 1) if len(col_values) >= start_row else 0
    skipped = total_urls - len(valid)

    if resume:
        title_col_idx = gspread.utils.a1_to_rowcol(f"{title_col}1")[1]
        title_values = worksheet.col_values(title_col_idx)
        pre_resume = len(valid)
        valid = [
            (i, u)
            for i, u in valid
            if (start_row - 1 + i >= len(title_values))
            or not (title_values[start_row - 1 + i] or "").strip()
        ]
        skipped += pre_resume - len(valid)
        print(f"Resume: {len(valid)} rows left to fill (skipping already filled).")
    else:
        print(f"Found {len(valid)} valid LinkedIn URLs (from row {start_row}).")

    if limit is not None and limit > 0:
        valid = valid[:limit]
        print(f"Batch limit: processing only the first {len(valid)} rows this run.")

    if dry_run:
        for idx, u in valid[:5]:
            print(f"  Row {start_row + idx}: {u}")
        print("Dry run done. Remove --dry-run to run for real.")
        return {"total": total_urls, "success": 0, "errors": 0, "skipped": skipped, "results": []}

    if use_bb_browser:
        results = _run_bb_browser(config, valid, start_row, title_col, company_col, worksheet)
    else:
        results = _run_playwright(config, valid, start_row, title_col, company_col, worksheet,
                                  headless, use_chrome)

    success = sum(1 for r in results if r["status"] == "success")
    errors = sum(1 for r in results if r["status"] == "error")
    summary = {
        "total": total_urls,
        "processed": len(results),
        "success": success,
        "errors": errors,
        "skipped": skipped,
        "results": results,
    }
    print(f"Done. {success} succeeded, {errors} errors, {skipped} skipped.")
    return summary


def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="LinkedIn Sheet Agent",
        epilog=(
            "By default, uses bb-browser to scrape via your real Chrome session "
            "(no new window, no login needed). Use --playwright to fall back to "
            "Playwright if bb-browser is not set up."
        ),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--bb-browser", action="store_true", default=True,
        help="(default) Use bb-browser: scrapes via your real Chrome login session",
    )
    mode.add_argument(
        "--playwright", action="store_true",
        help="Fall back to Playwright (opens a separate browser window)",
    )
    parser.add_argument("--no-headless", action="store_true", help="Show browser window (Playwright only)")
    parser.add_argument("--dry-run", action="store_true", help="Only list URLs, do not scrape")
    parser.add_argument("--resume", action="store_true", help="Skip rows that already have Title filled")
    parser.add_argument("--chrome", action="store_true", help="Use Chrome with persistent profile (Playwright only)")
    parser.add_argument("--limit", type=int, metavar="N", help="Process only the first N rows this run (batch size, e.g. 10)")
    args = parser.parse_args()
    config = load_config()

    use_bb = not args.playwright

    run(
        config,
        headless=not args.no_headless,
        dry_run=args.dry_run,
        resume=args.resume,
        use_chrome=args.chrome,
        use_bb_browser=use_bb,
        limit=args.limit,
    )


if __name__ == "__main__":
    main()
