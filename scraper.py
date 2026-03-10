"""
Tax Resolution Business Listing Scraper
Expansive internet-wide search across brokers, directories, press, forums, and more.
Runs multiple focused searches in sequence, deduplicates, appends to Google Sheets.
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

JSON_TEMPLATE = """
Return ONLY a raw JSON array. No explanation, no markdown, no preamble. Just the array:
[{"listing_title":"...","platform":"...","url":"...","asking_price":null,"annual_revenue":null,"revenue_disclosed":false,"location":"...","description":"...","highlights":"...","listing_date":"...","status":"Active","meets_threshold":true}]

If you find no listings, return exactly: []
"""

SEARCH_BATCHES = [

    # ── TIER 1: Major business-for-sale marketplaces ──────────────────────────
    {
        "label": "BizBuySell — all tax resolution variants",
        "prompt": f"""You are searching BizBuySell.com for tax resolution and settlement businesses for sale.

Search for and fetch results from:
- https://www.bizbuysell.com/financial-services-businesses-for-sale/?q=tax+resolution
- https://www.bizbuysell.com/financial-services-businesses-for-sale/?q=tax+settlement
- https://www.bizbuysell.com/financial-services-businesses-for-sale/?q=tax+relief
- https://www.bizbuysell.com/financial-services-businesses-for-sale/?q=IRS+resolution
- https://www.bizbuysell.com/financial-services-businesses-for-sale/?q=offer+in+compromise

Also search: site:bizbuysell.com "tax resolution" business for sale

Fetch each results page and extract every listing. Focus on businesses with $5M+ revenue signals.
{JSON_TEMPLATE}"""
    },
    {
        "label": "BizQuest + BusinessBroker.net + LoopNet",
        "prompt": f"""Search these major business-for-sale marketplaces for tax resolution and settlement businesses:

BizQuest: fetch https://www.bizquest.com/search/?q=tax+resolution and https://www.bizquest.com/search/?q=tax+settlement
BusinessBroker.net: search site:businessbroker.net "tax resolution" OR "tax settlement"
LoopNet business section: search site:loopnet.com/biz "tax resolution"

Also try these direct searches:
- bizquest.com tax resolution company for sale
- businessbroker.net tax settlement business United States

Fetch each page and pull all listings. Prioritize $5M+ revenue signals.
{JSON_TEMPLATE}"""
    },
    {
        "label": "DealStream + MergerNetwork + Acquire.com + ExitAdviser",
        "prompt": f"""Search these business acquisition marketplaces for tax resolution and settlement companies:

- DealStream: fetch https://dealstream.com and search site:dealstream.com "tax resolution" OR "tax settlement"
- MergerNetwork: search site:mergernetwork.com "tax resolution"
- Acquire.com: search site:acquire.com "tax resolution" OR "tax settlement"
- ExitAdviser: search site:exitadviser.com "tax resolution"
- BusinessesForSale.com: search site:businessesforsale.com "tax resolution"
- BizBen.com: search site:bizben.com "tax resolution"

Fetch results pages for each. Extract all listings with revenue or pricing signals.
{JSON_TEMPLATE}"""
    },

    # ── TIER 2: M&A advisor and lower-middle-market platforms ────────────────
    {
        "label": "Axial + Cyndx + Capital IQ deal flow",
        "prompt": f"""Search middle-market M&A platforms and deal flow databases for tax resolution companies:

- Axial: search site:axial.net "tax resolution" OR "tax settlement" OR "tax relief"
- Search: axial.net tax resolution company for sale lower middle market
- Search: "tax resolution" company "seeking buyer" OR "available for acquisition" site:axial.net
- Search: "tax settlement" OR "IRS resolution" company M&A deal 2024 2025 lower middle market
- Search: tax resolution business EBITDA acquisition "private equity"
- Search: "tax resolution" company "confidential information memorandum" OR "CIM" for sale

Fetch any pages that surface deal listings, teasers, or CIMs.
{JSON_TEMPLATE}"""
    },
    {
        "label": "IBBA + M&A Source + broker association listings",
        "prompt": f"""Search industry association listing platforms used by certified business brokers:

- IBBA (International Business Brokers Association): search site:ibba.org listings OR search ibba.org "tax resolution" business listing
- M&A Source: search site:masource.org "tax resolution"
- CABB (California Association of Business Brokers): search site:cabb.org "tax resolution"
- Search: IBBA member broker "tax resolution" business for sale listing 2024 2025
- Search: certified business broker "tax resolution" OR "tax settlement" company for sale
- Search: "business broker" "tax resolution" listing confidential United States

These are often overlooked by buyers going direct to consumer marketplaces.
{JSON_TEMPLATE}"""
    },

    # ── TIER 3: Broker websites — well-known nationals ───────────────────────
    {
        "label": "Synergy + Sunbelt + Murphy + Transworld broker listings",
        "prompt": f"""Search listing pages on major national business broker networks:

- Synergy Business Brokers: fetch https://synergybb.com/listings/ and search site:synergybb.com "tax resolution" OR "tax settlement" OR "tax relief"
- Sunbelt Business Brokers: search site:sunbeltnetwork.com "tax resolution" OR "tax settlement"
- Murphy Business: search site:murphybusiness.com "tax resolution"
- Transworld Business Advisors: search site:transworldma.com "tax resolution"
- Vested Business Brokers: search site:vestedbb.com "tax resolution"

Fetch each broker's listings page and extract any matching businesses.
{JSON_TEMPLATE}"""
    },
    {
        "label": "Boutique and regional broker websites",
        "prompt": f"""Search boutique and regional M&A advisory firm websites that specialize in financial services or professional services deals — these are less visible and often overlooked:

Search:
- "tax resolution company" for sale broker site:.com -bizbuysell.com -bizquest.com
- boutique M&A advisor "tax resolution" OR "tax settlement" business for sale
- "financial services" business broker "tax resolution" listing
- site:sunbeltnetwork.com OR site:murphybusiness.com OR site:pacificbusiness.com "tax resolution"
- "tax resolution" "tax settlement" business sale advisor "lower middle market" 2024 2025
- "tax relief" company for sale "confidential" broker advisor

Fetch any pages that surface as broker deal listings.
{JSON_TEMPLATE}"""
    },

    # ── TIER 4: Press, news, and deal announcements ───────────────────────────
    {
        "label": "Press releases and deal announcements",
        "prompt": f"""Search for press releases, news articles, and deal announcements about tax resolution companies being sold or seeking buyers. These often surface deals before or after they hit marketplaces:

Search:
- "tax resolution" company acquired OR acquisition OR sold 2024 2025
- "tax settlement" business "private equity" investment OR acquisition 2024 2025
- "tax relief" company "strategic acquisition" 2024 2025
- site:prnewswire.com "tax resolution" acquired OR sale
- site:businesswire.com "tax resolution" OR "tax settlement" acquisition
- site:globenewswire.com "tax resolution" company sale
- "tax resolution" company "has been acquired" OR "was acquired" 2023 2024 2025
- "IRS resolution" firm merger acquisition deal announcement

Fetch any press releases or news pages with deal details.
{JSON_TEMPLATE}"""
    },
    {
        "label": "LinkedIn and professional network deal postings",
        "prompt": f"""Search for tax resolution business sale postings on LinkedIn and professional networks:

Search:
- site:linkedin.com "tax resolution" company for sale OR acquisition opportunity
- site:linkedin.com "tax settlement" business sale
- linkedin.com "tax resolution" business broker listing 2024 2025
- "tax resolution" business "for sale" linkedin post 2024 2025
- "seeking acquirer" OR "exploring sale" "tax resolution" company
- "tax resolution" "tax settlement" firm "exit" OR "sale process" 2024 2025
- professional services firm "tax resolution" niche "for sale" OR "acquisition"

Also search:
- site:reddit.com/r/smallbusiness "tax resolution" for sale
- site:reddit.com/r/entrepreneur "tax resolution" business sale

{JSON_TEMPLATE}"""
    },

    # ── TIER 5: Industry-specific and niche directories ───────────────────────
    {
        "label": "Tax industry associations and niche directories",
        "prompt": f"""Search industry-specific directories and associations related to tax resolution professionals — these sometimes have unlisted or off-market opportunities:

Search:
- NAEA (National Association of Enrolled Agents) site: search site:naea.org "for sale" OR "practice for sale"
- NATP (National Association of Tax Professionals): search site:natp.com practice for sale
- ASTPS (American Society of Tax Problem Solvers): search site:astps.org member listings or for sale
- "enrolled agent" "tax resolution" practice for sale 2024 2025
- "tax resolution" "offer in compromise" firm for sale enrolled agent CPA
- niche tax resolution industry forum "for sale" OR "selling my practice"
- "tax resolution" practice "transition" OR "succession" for sale

These niche sources are highly unlikely to be on the partner's radar.
{JSON_TEMPLATE}"""
    },
    {
        "label": "Law firm and financial advisor deal sourcing",
        "prompt": f"""Search law firm deal pages, investment bank deal tombstones, and financial advisor announcements related to tax resolution company sales:

Search:
- law firm "tax resolution" company sale OR acquisition "advised" 2023 2024 2025
- investment bank OR financial advisor "tax resolution" "completed transaction" OR "closed deal"
- "financial advisory" "tax resolution" OR "tax settlement" company M&A transaction
- site:dykema.com OR site:foley.com OR site:gtlaw.com "tax resolution" acquisition
- "tombstone" "tax resolution" OR "tax settlement" company acquired
- "has completed the sale" OR "is pleased to announce" "tax resolution" company 2024 2025
- boutique investment bank "tax services" OR "tax resolution" deal announcement

These tombstones reveal completed and near-completed transactions.
{JSON_TEMPLATE}"""
    },

    # ── TIER 6: Broad catch-all sweeps ───────────────────────────────────────
    {
        "label": "Broad Google sweep — all variants and long-tail",
        "prompt": f"""Run a broad internet sweep using long-tail and variant search terms for tax resolution businesses for sale that may not appear in standard searches:

Search ALL of these:
- "tax resolution" "for sale" -site:bizbuysell.com -site:bizquest.com 2024 2025
- "IRS debt resolution" company for sale acquisition
- "back taxes" resolution company for sale United States
- "tax debt relief" business acquisition opportunity
- "offer in compromise" company business for sale
- "currently not collectible" OR "installment agreement" tax firm for sale
- "tax lien" resolution company for sale
- "wage garnishment" relief company for sale acquisition
- "IRS Fresh Start" program company for sale
- "tax representation" firm for sale 2024 2025
- "tax controversy" firm for sale acquisition
- "tax problem" resolution company for sale M&A

Fetch any pages that surface real listings or opportunities.
{JSON_TEMPLATE}"""
    },
    {
        "label": "Franchise and aggregator listings",
        "prompt": f"""Search franchise directories and business aggregator sites for tax resolution businesses:

Search:
- site:franchisegator.com "tax resolution" OR "tax relief"
- site:franchisehelp.com "tax resolution"
- site:bizbuysell.com/franchise "tax resolution"
- Optima Tax Relief franchise OR company for sale
- Community Tax franchise OR company for sale acquisition
- Anthem Tax Services for sale acquisition
- "Tax Defense Network" for sale OR acquisition
- "Larson Tax Relief" for sale
- "Tax Group Center" acquisition
- "tax resolution" franchise for sale United States

Also search for well-known tax resolution brands that may be exploring a sale.
{JSON_TEMPLATE}"""
    },
]

# ── Claude API call ──────────────────────────────────────────────────────────

def run_single_search(client, label, prompt):
    print(f"  [{label}]...")
    try:
        response = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=4000,
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            messages=[{"role": "user", "content": prompt}]
        )
        full_text = ""
        for block in response.content:
            if hasattr(block, "text"):
                full_text += block.text
        return full_text
    except Exception as e:
        print(f"    Error: {e}")
        return "[]"

def parse_listings(raw_text, label):
    text  = raw_text.strip()
    start = text.find("[")
    end   = text.rfind("]") + 1
    if start == -1 or end == 0:
        print(f"    No JSON array in response")
        return []
    try:
        listings = json.loads(text[start:end])
        print(f"    Found {len(listings)} listing(s)")
        return listings
    except json.JSONDecodeError as e:
        print(f"    JSON parse error: {e}")
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
        print("\nNo new listings found today — sheet is up to date.")

    return len(new_rows)

# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    print(f"=== Tax Resolution Scraper — {datetime.utcnow().date()} ===")
    print(f"Running {len(SEARCH_BATCHES)} search batches...\n")

    client       = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    all_listings = []

    for i, batch in enumerate(SEARCH_BATCHES, 1):
        print(f"Batch {i}/{len(SEARCH_BATCHES)}: {batch['label']}")
        raw      = run_single_search(client, batch["label"], batch["prompt"])
        listings = parse_listings(raw, batch["label"])
        all_listings.extend(listings)
        time.sleep(4)  # pause between batches to avoid rate limits

    print(f"\nTotal raw listings found: {len(all_listings)}")
    ws  = get_sheet()
    new = append_new_listings(ws, all_listings)
    print(f"=== Done. {new} new listing(s) added today. ===")

if __name__ == "__main__":
    main()
