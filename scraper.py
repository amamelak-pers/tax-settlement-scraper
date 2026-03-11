"""
Tax Resolution Business Listing Scraper
- Active listings only (no Sold/Completed)
- Cleaner sheet schema with broker contact field
- Strict URL requirement in every listing
- Tier-1 rate limit safe: short prompts + 70s pauses
"""

import os
import json
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

# JSON schema instruction — kept short for token efficiency
# url is REQUIRED — Claude must always include the direct listing URL
J = (
    'Return ONLY a JSON array, no other text. '
    'ONLY include listings with status "Active" or "Under LOI" — exclude Sold/Completed. '
    'url field is REQUIRED — always include the direct URL to the listing page, never leave blank. '
    'Schema: [{'
    '"listing_title":"full title of listing",'
    '"platform":"site or broker name",'
    '"url":"REQUIRED direct URL to listing",'
    '"asking_price":null or number,'
    '"annual_revenue":null or number,'
    '"revenue_notes":"any revenue context if not a clean number",'
    '"location":"state or region",'
    '"description":"2 sentence business summary",'
    '"highlights":"key selling points",'
    '"broker_contact":"broker name and contact info if available",'
    '"status":"Active or Under LOI"'
    '}]. If none found return []'
)

SEARCH_BATCHES = [
    {
        "label": "BizBuySell — tax resolution",
        "prompt": f"Search bizbuysell.com for ACTIVE tax resolution and tax settlement businesses currently for sale (not sold). Search 'site:bizbuysell.com tax resolution for sale' and 'site:bizbuysell.com tax settlement business'. Fetch the result pages and extract each listing's direct URL, title, price, revenue, location, and broker. {J}"
    },
    {
        "label": "BizBuySell — IRS and tax relief variants",
        "prompt": f"Search bizbuysell.com for ACTIVE IRS resolution and tax relief businesses for sale (not sold). Search 'site:bizbuysell.com IRS resolution', 'site:bizbuysell.com tax relief company for sale', 'site:bizbuysell.com offer in compromise'. Fetch pages and extract each listing's direct URL, title, price, revenue, location, broker. {J}"
    },
    {
        "label": "BizQuest + BusinessBroker.net",
        "prompt": f"Search BizQuest.com and BusinessBroker.net for ACTIVE tax resolution and tax settlement businesses for sale. Search 'site:bizquest.com tax resolution', 'site:businessbroker.net tax resolution', 'site:bizquest.com tax settlement'. Fetch result pages and get each listing's direct URL, title, price, revenue, location, broker. {J}"
    },
    {
        "label": "Synergy + Sunbelt + Murphy brokers",
        "prompt": f"Search broker sites for ACTIVE tax resolution business listings. Fetch https://synergybb.com/listings/ and search 'site:synergybb.com tax resolution'. Search 'site:sunbeltnetwork.com tax resolution OR tax settlement' and 'site:murphybusiness.com tax resolution'. Get each listing's direct URL, title, price, revenue, location, broker contact. {J}"
    },
    {
        "label": "Axial + DealStream + MergerNetwork",
        "prompt": f"Search M&A platforms for ACTIVE tax resolution company acquisition opportunities. Search 'site:axial.net tax resolution', 'site:dealstream.com tax resolution', 'site:mergernetwork.com tax resolution', 'axial.net tax settlement company for sale lower middle market'. Get each listing's direct URL, title, revenue, location. {J}"
    },
    {
        "label": "IBBA + broker association listings",
        "prompt": f"Search broker association databases for ACTIVE tax resolution practices for sale. Search 'IBBA member broker tax resolution business for sale 2025', 'certified business broker tax resolution company for sale 2025', 'tax resolution practice confidential listing broker', 'site:ibba.org tax resolution'. Get direct URLs, titles, prices, broker contacts. {J}"
    },
    {
        "label": "Tax professional associations",
        "prompt": f"Search tax professional associations for ACTIVE practices for sale. Search 'enrolled agent tax resolution practice for sale 2025', 'site:naea.org practice for sale', 'ASTPS tax resolution practice sale', 'NATP tax resolution practice for sale', 'tax resolution enrolled agent practice transition 2025'. Get direct URLs, titles, revenue info, contact details. {J}"
    },
    {
        "label": "LinkedIn and off-market opportunities",
        "prompt": f"Search for ACTIVE off-market tax resolution business sale opportunities. Search 'site:linkedin.com tax resolution company for sale 2025', 'seeking acquirer tax resolution company 2025', 'exploring sale tax resolution firm 2025', 'tax resolution business exit opportunity', 'off-market tax settlement company acquisition'. Get direct URLs and contact info. {J}"
    },
    {
        "label": "Long-tail IRS terminology search",
        "prompt": f"Search for ACTIVE tax resolution businesses using IRS-specific terms. Search 'IRS debt resolution company for sale 2025', 'offer in compromise firm for sale', 'tax lien resolution business for sale', 'IRS Fresh Start program company for sale', 'tax controversy firm for sale', 'tax representation firm sale 2025'. Get direct listing URLs, prices, revenue, contacts. {J}"
    },
    {
        "label": "Named brands — exploring sale",
        "prompt": f"Search for news or listings suggesting well-known tax resolution brands may be for sale. Search 'Optima Tax Relief acquisition OR for sale', 'Community Tax LLC acquisition', 'Tax Defense Network sale', 'Anthem Tax Services for sale', 'Larson Tax Relief acquisition', 'tax resolution company sale 2025 private equity'. Get direct URLs and any deal details. {J}"
    },
    {
        "label": "Acquire.com + ExitAdviser + BizBen",
        "prompt": f"Search smaller marketplaces for ACTIVE tax resolution listings. Search 'site:acquire.com tax resolution', 'site:exitadviser.com tax resolution', 'site:bizben.com tax resolution', 'site:businessesforsale.com tax resolution OR tax settlement'. Fetch result pages and get each listing's direct URL, title, price, revenue, location. {J}"
    },
    {
        "label": "Private equity and M&A advisor sourcing",
        "prompt": f"Search for tax resolution companies being actively marketed for sale by M&A advisors. Search 'tax resolution company confidential information memorandum 2025', 'tax settlement firm investment banker for sale', 'tax resolution company EBITDA for sale private equity', 'lower middle market tax resolution acquisition 2025'. Get direct URLs and deal details. {J}"
    },
    {
        "label": "Broad catch-all sweep",
        "prompt": f"Search broadly for any ACTIVE tax resolution or settlement businesses for sale not found by other searches. Search 'tax resolution company for sale 2025', 'tax settlement business acquisition opportunity', 'IRS resolution firm for sale', 'tax relief company revenue for sale United States 2025'. Fetch promising pages and extract listing details including direct URLs. {J}"
    },
]

# ── Claude API ───────────────────────────────────────────────────────────────

def run_single_search(client, label, prompt):
    print(f"  [{label}]...")
    try:
        response = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=2000,
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            messages=[{"role": "user", "content": prompt}]
        )
        full_text = ""
        for block in response.content:
            if hasattr(block, "text"):
                full_text += block.text
        return full_text
    except anthropic.RateLimitError:
        print(f"    Rate limit hit — waiting 90s then retrying...")
        time.sleep(90)
        try:
            response = client.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=2000,
                tools=[{"type": "web_search_20250305", "name": "web_search"}],
                messages=[{"role": "user", "content": prompt}]
            )
            full_text = ""
            for block in response.content:
                if hasattr(block, "text"):
                    full_text += block.text
            return full_text
        except Exception as e2:
            print(f"    Retry failed: {e2}")
            return "[]"
    except Exception as e:
        print(f"    Error: {e}")
        return "[]"

def parse_listings(raw_text, label):
    text  = raw_text.strip()
    start = text.find("[")
    end   = text.rfind("]") + 1
    if start == -1 or end == 0:
        print(f"    No JSON found")
        return []
    try:
        listings = json.loads(text[start:end])
        # Filter to active only as a safety net
        active = [l for l in listings if l.get("status", "").lower() in ("active", "under loi", "under loi")]
        print(f"    Found {len(active)} active listing(s) (of {len(listings)} total)")
        return active
    except json.JSONDecodeError:
        print(f"    JSON parse error")
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
    "Listing ID",        # hidden dedup key — keep last so partner ignores it
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
    print(f"Running {len(SEARCH_BATCHES)} batches with 70s pause between each...\n")

    client       = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    all_listings = []

    for i, batch in enumerate(SEARCH_BATCHES, 1):
        print(f"Batch {i}/{len(SEARCH_BATCHES)}: {batch['label']}")
        raw      = run_single_search(client, batch["label"], batch["prompt"])
        listings = parse_listings(raw, batch["label"])
        all_listings.extend(listings)
        if i < len(SEARCH_BATCHES):
            print(f"    Pausing 70s...")
            time.sleep(70)

    print(f"\nTotal active listings found: {len(all_listings)}")

    ws  = get_sheet()
    new = append_new_listings(ws, all_listings)
    print(f"=== Done. {new} new listing(s) added today. ===")

if __name__ == "__main__":
    main()
