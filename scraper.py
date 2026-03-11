"""
Tax Resolution Business Listing Scraper
- Uses claude-haiku-4-5 (50K token/min Tier 1 limit vs 30K for Sonnet)
- 90s pauses between batches
- Debug logging to see raw Claude output
- Loose JSON parsing with fallback
- Active/Under LOI only
"""

import os
import json
import re
import anthropic
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime
import hashlib
import time

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
GOOGLE_SHEET_ID   = os.environ["GOOGLE_SHEET_ID"]
GOOGLE_CREDS_JSON = os.environ["GOOGLE_CREDENTIALS_JSON"]
SHEET_NAME        = "Listings"

J = (
    'Return ONLY a JSON array. No explanation, no markdown. '
    'Only include listings with status Active or Under LOI — skip Sold/Completed. '
    'Always include the direct URL to the listing. '
    'Schema: [{'
    '"listing_title":"title",'
    '"platform":"site or broker",'
    '"url":"direct listing URL",'
    '"asking_price":null or number,'
    '"annual_revenue":null or number,'
    '"revenue_notes":"any revenue context",'
    '"location":"state or region",'
    '"description":"2 sentence summary",'
    '"highlights":"key selling points",'
    '"broker_contact":"broker name and contact if available",'
    '"status":"Active or Under LOI"'
    '}]. If none found: []'
)

SEARCH_BATCHES = [
    {
        "label": "BizBuySell — tax resolution",
        "prompt": f"Search bizbuysell.com for active tax resolution and tax settlement businesses for sale. Search 'site:bizbuysell.com tax resolution for sale' and 'site:bizbuysell.com tax settlement business'. Fetch result pages. Extract listing title, direct URL, price, revenue, location, broker for each. {J}"
    },
    {
        "label": "BizBuySell — IRS and tax relief",
        "prompt": f"Search bizbuysell.com for active IRS resolution and tax relief businesses for sale. Search 'site:bizbuysell.com IRS resolution' and 'site:bizbuysell.com tax relief company for sale' and 'site:bizbuysell.com offer in compromise'. Fetch pages. Extract listing title, direct URL, price, revenue, location, broker. {J}"
    },
    {
        "label": "BizQuest + BusinessBroker.net",
        "prompt": f"Search BizQuest.com and BusinessBroker.net for active tax resolution businesses for sale. Search 'site:bizquest.com tax resolution', 'site:businessbroker.net tax resolution', 'site:bizquest.com tax settlement'. Fetch result pages. Extract title, direct URL, price, revenue, location, broker. {J}"
    },
    {
        "label": "Synergy + Sunbelt + Murphy brokers",
        "prompt": f"Find active tax resolution business listings on broker sites. Fetch https://synergybb.com/listings/ and search 'site:synergybb.com tax resolution'. Search 'site:sunbeltnetwork.com tax resolution' and 'site:murphybusiness.com tax resolution'. Extract title, direct URL, price, revenue, location, broker contact. {J}"
    },
    {
        "label": "Axial + DealStream + MergerNetwork",
        "prompt": f"Search M&A platforms for active tax resolution acquisition opportunities. Search 'site:axial.net tax resolution', 'site:dealstream.com tax resolution', 'site:mergernetwork.com tax resolution'. Extract title, direct URL, revenue, location. {J}"
    },
    {
        "label": "IBBA + broker association listings",
        "prompt": f"Search for active tax resolution practices listed through broker associations. Search 'IBBA broker tax resolution business for sale 2025', 'certified business broker tax resolution for sale 2025', 'tax resolution practice confidential listing 2025'. Extract title, direct URL, price, revenue, broker contact. {J}"
    },
    {
        "label": "Tax professional associations",
        "prompt": f"Search for active tax resolution practices for sale via professional associations. Search 'enrolled agent tax resolution practice for sale 2025', 'NAEA practice for sale tax resolution', 'ASTPS tax resolution practice sale 2025', 'tax resolution practice transition 2025'. Extract title, direct URL, revenue, contact. {J}"
    },
    {
        "label": "LinkedIn and off-market",
        "prompt": f"Search for active off-market tax resolution business opportunities. Search 'site:linkedin.com tax resolution company for sale 2025', 'seeking acquirer tax resolution 2025', 'exploring strategic sale tax resolution firm', 'off-market tax settlement company acquisition 2025'. Extract title, URL, details, contact. {J}"
    },
    {
        "label": "IRS terminology variants",
        "prompt": f"Search for active tax resolution businesses using IRS-specific terms. Search 'IRS debt resolution company for sale 2025', 'offer in compromise business for sale', 'tax lien resolution firm for sale', 'tax controversy firm acquisition 2025', 'wage garnishment relief company sale'. Extract title, direct URL, price, revenue, location. {J}"
    },
    {
        "label": "Named brands",
        "prompt": f"Search for news or listings about known tax resolution brands for sale. Search 'Optima Tax Relief acquisition 2025', 'Community Tax acquisition', 'Tax Defense Network for sale', 'Anthem Tax Services acquisition', 'tax resolution company private equity deal 2025'. Extract title, direct URL, deal details. {J}"
    },
    {
        "label": "Acquire.com + ExitAdviser + BizBen",
        "prompt": f"Search smaller marketplaces for active tax resolution listings. Search 'site:acquire.com tax resolution', 'site:exitadviser.com tax resolution', 'site:bizben.com tax resolution', 'site:businessesforsale.com tax resolution'. Fetch result pages. Extract title, direct URL, price, revenue, location. {J}"
    },
    {
        "label": "M&A advisor and PE sourcing",
        "prompt": f"Search for tax resolution companies being marketed by M&A advisors or investment bankers. Search 'tax resolution company confidential information memorandum 2025', 'tax settlement firm for sale investment banker', 'tax resolution EBITDA acquisition private equity 2025'. Extract title, direct URL, revenue, deal details. {J}"
    },
    {
        "label": "Broad catch-all",
        "prompt": f"Search broadly for any active tax resolution or settlement businesses for sale in the US. Search 'tax resolution company for sale United States 2025', 'tax settlement business acquisition 2025', 'IRS resolution firm for sale revenue', 'tax relief company for sale $5 million 2025'. Fetch promising pages. Extract title, direct URL, price, revenue, location, broker. {J}"
    },
]

# ── Claude API ───────────────────────────────────────────────────────────────

def run_single_search(client, label, prompt):
    print(f"  [{label}]...")
    for attempt in range(2):
        try:
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=2000,
                tools=[{"type": "web_search_20250305", "name": "web_search"}],
                messages=[{"role": "user", "content": prompt}]
            )
            full_text = ""
            for block in response.content:
                if hasattr(block, "text"):
                    full_text += block.text
            # Debug: print first 300 chars of response
            preview = full_text.strip()[:300].replace("\n", " ")
            print(f"    Response preview: {preview}")
            return full_text
        except anthropic.RateLimitError as e:
            wait = 120 if attempt == 0 else 0
            if wait:
                print(f"    Rate limit — waiting {wait}s...")
                time.sleep(wait)
            else:
                print(f"    Rate limit on retry — skipping")
                return "[]"
        except Exception as e:
            print(f"    Error: {e}")
            return "[]"
    return "[]"

def parse_listings(raw_text, label):
    text = raw_text.strip()

    # Try to find JSON array anywhere in the response
    match = re.search(r'\[.*\]', text, re.DOTALL)
    if not match:
        print(f"    No JSON array found in response")
        return []

    try:
        listings = json.loads(match.group())
        # Filter active only
        active = [
            l for l in listings
            if isinstance(l, dict) and
            l.get("status", "active").lower().replace(" ", "") in ("active", "underloi")
        ]
        print(f"    Parsed {len(active)} active listing(s) of {len(listings)} total")
        return active
    except json.JSONDecodeError as e:
        print(f"    JSON parse error: {e}")
        print(f"    Raw snippet: {match.group()[:200]}")
        return []

# ── Google Sheets ────────────────────────────────────────────────────────────

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

HEADERS = [
    "Date Found",
    "Listing Title",
    "Platform",
    "URL",
    "Asking Price ($)",
    "Annual Revenue ($)",
    "Revenue Notes",
    "Location",
    "Description",
    "Highlights",
    "Broker / Contact",
    "Status",
    "Listing ID",
]

def get_sheet():
    creds_dict = json.loads(GOOGLE_CREDS_JSON)
    creds      = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    gc         = gspread.authorize(creds)
    sh         = gc.open_by_key(GOOGLE_SHEET_ID)
    try:
        ws = sh.worksheet(SHEET_NAME)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=SHEET_NAME, rows=2000, cols=len(HEADERS))
        ws.append_row(HEADERS)
        ws.format(f"A1:{chr(64+len(HEADERS))}1", {
            "textFormat": {"bold": True},
            "backgroundColor": {"red": 0.12, "green": 0.22, "blue": 0.40},
            "horizontalAlignment": "CENTER"
        })
        ws.freeze(rows=1)
    return ws

def make_listing_id(listing):
    key = listing.get("url") or f"{listing.get('listing_title','')}-{listing.get('platform','')}"
    return hashlib.md5(key.encode()).hexdigest()[:10]

def get_existing_ids(ws):
    try:
        col_index = HEADERS.index("Listing ID") + 1
        ids = ws.col_values(col_index)
        return set(ids[1:])
    except Exception:
        return set()

def append_new_listings(ws, all_listings):
    existing_ids = get_existing_ids(ws)
    today        = datetime.utcnow().strftime("%Y-%m-%d")
    new_rows     = []
    seen_ids     = set()

    for l in all_listings:
        lid = make_listing_id(l)
        if lid in existing_ids or lid in seen_ids:
            continue
        seen_ids.add(lid)
        row = [
            today,
            l.get("listing_title", ""),
            l.get("platform", ""),
            l.get("url", ""),
            l.get("asking_price", "") or "",
            l.get("annual_revenue", "") or "",
            l.get("revenue_notes", ""),
            l.get("location", ""),
            l.get("description", ""),
            l.get("highlights", ""),
            l.get("broker_contact", ""),
            l.get("status", "Active"),
            lid,
        ]
        new_rows.append(row)

    if new_rows:
        ws.append_rows(new_rows, value_input_option="USER_ENTERED")
        print(f"\n✅ Appended {len(new_rows)} new listing(s) to Google Sheet.")
    else:
        print("\nNo new listings to add today.")

    return len(new_rows)

# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    print(f"=== Tax Resolution Scraper — {datetime.utcnow().date()} ===")
    print(f"Model: claude-haiku-4-5-20251001 | Batches: {len(SEARCH_BATCHES)} | Pause: 90s\n")

    client       = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    all_listings = []

    for i, batch in enumerate(SEARCH_BATCHES, 1):
        print(f"Batch {i}/{len(SEARCH_BATCHES)}: {batch['label']}")
        raw      = run_single_search(client, batch["label"], batch["prompt"])
        listings = parse_listings(raw, batch["label"])
        all_listings.extend(listings)
        if i < len(SEARCH_BATCHES):
            print(f"    Pausing 90s...")
            time.sleep(90)

    print(f"\nTotal active listings found: {len(all_listings)}")
    ws  = get_sheet()
    new = append_new_listings(ws, all_listings)
    print(f"=== Done. {new} new listing(s) added today. ===")

if __name__ == "__main__":
    main()
