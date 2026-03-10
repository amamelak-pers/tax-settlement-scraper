"""
Tax Resolution Business Listing Scraper
Tier-1 rate limit safe: short prompts + 70s pauses between batches.
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

# Keep prompts SHORT — Tier 1 limit is 30K input tokens/minute.
# Each prompt here is ~300-400 tokens. With 70s pause between calls,
# we stay well under the limit.

J = 'Return ONLY a JSON array, no other text. Schema: [{"listing_title":"","platform":"","url":"","asking_price":null,"annual_revenue":null,"revenue_disclosed":false,"location":"","description":"","highlights":"","listing_date":"","status":"Active","meets_threshold":false}]. If none found return []'

SEARCH_BATCHES = [
    {
        "label": "BizBuySell tax resolution",
        "prompt": f"Search bizbuysell.com for tax resolution and tax settlement businesses for sale. Search: 'site:bizbuysell.com tax resolution for sale' and 'site:bizbuysell.com tax settlement business'. Fetch result pages and extract all listings. Focus on $5M+ revenue. {J}"
    },
    {
        "label": "BizBuySell IRS relief variants",
        "prompt": f"Search bizbuysell.com for IRS resolution and tax relief businesses for sale. Search: 'site:bizbuysell.com IRS resolution' and 'site:bizbuysell.com tax relief company for sale' and 'site:bizbuysell.com offer in compromise'. Fetch pages and extract listings. {J}"
    },
    {
        "label": "BizQuest + BusinessBroker.net",
        "prompt": f"Search BizQuest.com and BusinessBroker.net for tax resolution and tax settlement businesses for sale. Try: 'site:bizquest.com tax resolution', 'site:businessbroker.net tax resolution', 'site:bizquest.com tax settlement'. Fetch result pages. {J}"
    },
    {
        "label": "Synergy + Sunbelt broker listings",
        "prompt": f"Search broker sites for tax resolution businesses. Try: 'site:synergybb.com tax resolution', fetch https://synergybb.com/listings/, 'site:sunbeltnetwork.com tax resolution OR tax settlement', 'site:murphybusiness.com tax resolution'. Extract all listings found. {J}"
    },
    {
        "label": "Axial + DealStream + MergerNetwork",
        "prompt": f"Search M&A platforms for tax resolution company acquisitions. Try: 'site:axial.net tax resolution', 'site:dealstream.com tax resolution', 'site:mergernetwork.com tax resolution', 'axial.net tax settlement company for sale lower middle market'. {J}"
    },
    {
        "label": "Press releases and deal announcements",
        "prompt": f"Search for press releases about tax resolution companies being sold or acquired. Try: 'site:prnewswire.com tax resolution acquired 2024 2025', 'site:businesswire.com tax settlement company acquisition', 'tax resolution company has been acquired 2024 2025'. Fetch pages for details. {J}"
    },
    {
        "label": "IBBA + broker association listings",
        "prompt": f"Search broker association sites and niche directories for tax resolution practices. Try: 'IBBA member broker tax resolution business for sale', 'site:ibba.org tax resolution listing', 'certified business broker tax resolution company for sale 2024 2025', 'tax resolution practice confidential listing broker'. {J}"
    },
    {
        "label": "Tax professional association listings",
        "prompt": f"Search tax professional associations for practices for sale. Try: 'site:naea.org practice for sale', 'enrolled agent tax resolution practice for sale 2024 2025', 'site:astps.org for sale', 'NATP tax resolution practice sale', 'tax resolution enrolled agent practice transition 2024'. {J}"
    },
    {
        "label": "Law firm tombstones and deal announcements",
        "prompt": f"Search for law firm and investment bank deal announcements involving tax resolution companies. Try: 'law firm advised tax resolution company sale 2024 2025', 'completed transaction tax resolution OR tax settlement company', 'pleased to announce acquisition tax resolution firm', 'tombstone tax resolution company acquired'. {J}"
    },
    {
        "label": "LinkedIn and off-market listings",
        "prompt": f"Search for off-market tax resolution business sale postings. Try: 'site:linkedin.com tax resolution company for sale', 'seeking acquirer tax resolution company', 'exploring sale tax resolution firm 2024 2025', 'tax resolution business exit opportunity', 'off-market tax settlement company acquisition'. {J}"
    },
    {
        "label": "Long-tail IRS keyword variants",
        "prompt": f"Search for tax resolution businesses using niche IRS terminology. Try: 'IRS debt resolution company for sale', 'offer in compromise firm acquisition', 'tax lien resolution business for sale', 'IRS Fresh Start company for sale', 'tax controversy firm for sale acquisition', 'wage garnishment relief company for sale', 'tax representation firm sale'. {J}"
    },
    {
        "label": "Named brands and franchise search",
        "prompt": f"Search for specific well-known tax resolution brands exploring a sale. Try: 'Optima Tax Relief for sale OR acquisition', 'Community Tax company sale', 'Tax Defense Network acquisition', 'Anthem Tax Services for sale', 'Larson Tax Relief sale', 'Tax Group Center acquisition', 'tax resolution franchise for sale United States'. {J}"
    },
    {
        "label": "Acquire.com + ExitAdviser + BizBen",
        "prompt": f"Search smaller business marketplaces for tax resolution listings. Try: 'site:acquire.com tax resolution', 'site:exitadviser.com tax resolution', 'site:bizben.com tax resolution', 'site:businessesforsale.com tax resolution', 'acquire.com tax settlement business for sale'. Fetch result pages. {J}"
    },
    {
        "label": "Broad catch-all sweep",
        "prompt": f"Do a broad sweep for any tax resolution or settlement businesses for sale not found by other searches. Try: 'tax resolution company for sale 2025 -bizbuysell.com', 'tax settlement business acquisition opportunity United States', 'IRS resolution firm for sale private equity', 'tax relief company revenue $5 million for sale'. Fetch any promising pages. {J}"
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
    except anthropic.RateLimitError as e:
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
        print(f"    Found {len(listings)} listing(s)")
        return listings
    except json.JSONDecodeError:
        print(f"    JSON parse error")
        return []

# ── Google Sheets ────────────────────────────────────────────────────────────

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

HEADERS = [
    "Date Found", "Listing Title", "Platform", "URL", "Asking Price ($)",
    "Annual Revenue ($)", "Revenue Disclosed", "Location", "Description",
    "Highlights", "Listing Date", "Status", "Meets $5M Threshold", "Listing ID"
]

def get_sheet():
    creds_dict = json.loads(GOOGLE_CREDS_JSON)
    creds      = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    gc         = gspread.authorize(creds)
    sh         = gc.open_by_key(GOOGLE_SHEET_ID)
    try:
        ws = sh.worksheet(SHEET_NAME)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=SHEET_NAME, rows=2000, cols=20)
        ws.append_row(HEADERS)
        ws.format("A1:N1", {
            "textFormat": {"bold": True},
            "backgroundColor": {"red": 0.15, "green": 0.25, "blue": 0.45},
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
            "Yes" if l.get("revenue_disclosed") else "No",
            l.get("location", ""),
            l.get("description", ""),
            l.get("highlights", ""),
            l.get("listing_date", ""),
            l.get("status", ""),
            "Yes" if l.get("meets_threshold") else "No",
            lid
        ]
        new_rows.append(row)

    if new_rows:
        ws.append_rows(new_rows, value_input_option="USER_ENTERED")
        print(f"\n✅ Appended {len(new_rows)} new listing(s) to Google Sheet.")
    else:
        print("\nNo new listings found today.")

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
            print(f"    Pausing 70s before next batch...")
            time.sleep(70)

    print(f"\nTotal raw listings found: {len(all_listings)}")
    ws  = get_sheet()
    new = append_new_listings(ws, all_listings)
    print(f"=== Done. {new} new listing(s) added today. ===")

if __name__ == "__main__":
    main()
