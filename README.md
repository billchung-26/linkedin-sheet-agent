# LinkedIn Sheet Agent

Reads LinkedIn profile URLs from a Google Sheet, extracts each person's **current job title** and **company** from their Experience section, and writes the results back to the sheet — all using your real Chrome login session.

## How it works

```
Google Sheet (LinkedIn URLs)
        │
        ▼
   run_agent.py
        │
        ├──► bb-browser eval ──► Your Chrome ──► LinkedIn Voyager API
        │         (no new window, uses your real login)
        │
        ▼
   Google Sheet (filled Job Title + Company)
```

1. **Read** — Pulls LinkedIn profile URLs from a configured column in your Google Sheet.
2. **Scrape** — For each URL, calls LinkedIn's internal Voyager API through your real Chrome session via [bb-browser](https://github.com/epiral/bb-browser). Extracts the **first entry** from the Experience section (not the headline), giving you the exact current job title and company.
3. **Write** — Writes the Job Title and Company back to the columns you specify.

### Why bb-browser?

| | Playwright / Selenium | bb-browser |
|---|---|---|
| Browser | Opens a new, isolated window | Uses your **real Chrome** — no new window |
| Login | Must re-login or extract cookies | Already logged in — zero setup |
| Bot detection | Easily detected and blocked | Invisible — you **are** the user |
| Speed | ~6s per profile (page load + render) | **~0.5s per profile** (direct API call) |

### Why Experience section instead of headline?

LinkedIn headlines are freeform text like *"Product Manager \| AI & SaaS \| MBA \| Berkeley Haas"* — hard to parse into a clean title and company. The Experience section gives structured data:

| Source | Job Title | Company |
|--------|-----------|---------|
| **Headline** | "Senior Product Manager \| AI & Enterprise SaaS \| Mobility Enthusiast \| MBA \| Berkeley Haas" | ??? |
| **Experience** | Product Manager | Adobe |

## Quick start

### 1. Python environment

```bash
git clone https://github.com/billchung-26/linkedin-sheet-agent.git
cd linkedin-sheet-agent
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. bb-browser

```bash
npm install -g bb-browser
bb-browser site update
```

Install the Chrome extension:

1. Download the `.zip` from [bb-browser releases](https://github.com/epiral/bb-browser/releases/latest).
2. Unzip → Chrome → `chrome://extensions/` → Developer Mode → **Load unpacked**.

Launch Chrome with remote debugging (needed once per session):

```bash
# macOS
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --remote-debugging-port=9222
```

Open `https://www.linkedin.com` in Chrome and make sure you're logged in.

### 3. Google Sheets API

1. Go to [Google Cloud Console](https://console.cloud.google.com/).
2. Create a project → Enable **Google Sheets API**.
3. Create **OAuth 2.0 credentials** (Desktop app) → download JSON → save as `credentials.json` in the project folder.

### 4. Configure

```bash
cp config.example.env .env
```

Edit `.env`:

| Variable | Description |
|----------|-------------|
| `GOOGLE_SHEET_ID` | From your sheet URL: `.../d/<SHEET_ID>/edit` |
| `SHEET_NAME` | Tab name (default: `Sheet1`) |
| `URL_COLUMN` | Column with LinkedIn URLs (e.g. `F`) |
| `DATA_START_ROW` | First data row (e.g. `2` if row 1 is headers) |
| `COMPANY_COLUMN` | *(optional)* Target column for Company (e.g. `I`) |
| `TITLE_COLUMN` | *(optional)* Target column for Job Title (e.g. `J`) |

### 5. Run

```bash
# Test first
python run_agent.py --dry-run

# Run all rows
python run_agent.py

# Process only the first 10
python run_agent.py --limit 10

# Skip rows that already have data
python run_agent.py --resume
```

## CLI options

```
python run_agent.py [OPTIONS]

Options:
  --bb-browser    (default) Scrape via your real Chrome session
  --playwright    Fall back to Playwright (opens a separate browser)
  --dry-run       List URLs without scraping
  --resume        Skip rows where Job Title is already filled
  --limit N       Only process the first N rows
  --no-headless   Show browser window (Playwright only)
  --chrome        Use Chrome with persistent profile (Playwright only)
```

## Architecture

The bb-browser backend calls LinkedIn's internal Voyager API (`/voyager/api/identity/dash/profiles`) with the `FullProfileWithEntities-93` decoration, which returns structured Experience data including `profilePositionGroups`. The script extracts the first position group's title and company name.

Falls back to headline parsing if no Experience entries exist for a profile.

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `No page target found` | Launch Chrome with `--remote-debugging-port=9222` and open LinkedIn |
| `Not logged in` | Open `linkedin.com` in Chrome, make sure you see your feed |
| `HTTP 403` | That profile is restricted/private — cannot be scraped |
| `bb-browser not installed` | `npm install -g bb-browser && bb-browser site update` |
| Google auth popup | Normal on first run — `token.json` is saved for next time |

## Security

- `credentials.json`, `token.json`, and `.env` are in `.gitignore` and never committed.
- bb-browser runs inside your own Chrome — no cookies or tokens are extracted or stored externally.
- All data stays between your browser, Google Sheets, and LinkedIn's own servers.

## License

MIT
