"""
Tax Resolution Business Listing Scraper
Runs daily via GitHub Actions, appends new listings to Google Sheets.
"""

import os
import json
import anthropic
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime
import hashlib
import time

# ── Config ──────────────────────────────────────────────────────────────────

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
GOOGLE_SHEET_ID   = os.environ["GOOGLE_SHEET_ID"]          # from Sheet URL
GOOGLE_CREDS_JSON = os.environ["GOOGLE_CREDENTIALS_JSON"]  # service account JSON string

SHEET_NAME = "Listings"
MIN_REVENUE_SIGNAL = 5_000_000  # $5M floor — used for filtering guidance in prompt

SEARCH_PROMPT = """
You are a deal sourcing analyst for a private equity firm seeking to acquire a 
tax settlement/resolution business in the United States.

Your job: conduct a thorough sweep of the internet to find EVERY currently active 
listing for tax resolution/settlement businesses for sale — through brokers, 
marketplaces, or any other channel — with annual revenue of approximately $5M or more.

A perfect example of what we're looking for:
https://synergybb.com/listings/tax-resolution-company-high-growth-ca/

STEP 1 — Search these marketplaces systematically:
- bizbuysell.com (search "tax resolution", "tax settlement", "IRS resolution")
- bizquest.com
- axial.net
- dealstream.com
- acquire.com
- businessbroker.net
- loopnet.com
- mergernetwork.com

STEP 2 — Search these brokers' listing pages directly:
- synergybb.com/listings
- sunbeltnetwork.com
- transworldma.com
- murphybusiness.com
- vestedbb.com
- exitadviser.com
- Any other broker whose site surfaces in results

STEP 3 — Run these Google searches and fetch promising results:
- "tax resolution company for sale" $5M OR $10M OR revenue
- "tax settlement business" acquisition listing 2024 OR 2025
- "IRS resolution firm" for sale broker
- "tax relief company" M&A lower middle market
- site:bizbuysell.com "tax resolution"
- site:bizquest.com "tax resolution"

STEP 4 — For EACH listing found, extract:
1. listing_title: Name or title of listing (keep confidential if so labeled)
2. platform: Where found (e.g., BizBuySell, Synergy BB, direct broker)
3. url: Direct URL to the listing
4. asking_price: Asking price if disclosed (as number, e.g. 8500000)
5. annual_revenue: Revenue if disclosed (as number, e.g. 5200000)
6. revenue_disclosed: true/false
7. location: State or region
8. description: 1-2 sentence summary of the business
9. highlights: Key selling points (recurring revenue, growth rate, client mix, etc.)
10. listing_date: Date listed if visible, else "Unknown"
11. status: "Active", "Under LOI", "Sold", or "Unknown"
12. meets_threshold: true if revenue >= $5M OR asking price suggests $5M+ revenue (at ~1-2x multiple)

IMPORTANT RULES:
- Focus on TAX RESOLUTION/SETTLEMENT specifically — not general CPA or tax prep firms 
  unless they have a substantial resolution practice
- Flag listings where revenue isn't disclosed but asking price > $5M (implies scale)
- Include listings marked "Under LOI" or recently sold — useful market comps
- Be exhaustive. Run as many searches as needed. More is better.
- Fetch full listing pages when snippets lack detail

Return your findings as a JSON array only — no preamble, no markdown, just raw JSON:
[
  {
    "listing_title": "...",
    "platform": "...",
    "url": "...",
    "asking_price": null or number,
    "annual_revenue": null or number,
    "revenue_disclosed": true/false,
    "location": "...",
    "description": "...",
    "highlights": "...",
    "listing_date": "...",
    "status": "Active",
    "meets_threshold": true/false
  }
]
"""

# ── Claude API Call ──────────────────────────────────────────────────────────

def run_claude_search():
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    
    print("Running Claude search with web search tool...")
    response = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=8000,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        messages=[{"role": "user", "content": SEARCH_PROMPT}]
    )
    
    # Extract text content from response
    full_text = ""
    for block in response.content:
        if hasattr(block, "text"):
            full_text += block.text
    
    return full_text

# ── Parse JSON from Claude response ─────────────────────────────────────────

def parse_listings(raw_text):
    # Strip markdown fences if present
    text = raw_text.strip()
    if "```" in text:
        start = text.find("[")
        end   = text.rfind("]") + 1
        text  = text[start:end]
    
    try:
        listings = json.loads(text)
        print(f"Parsed {len(listings)} listings from Claude.")
        return listings
    except json.JSONDecodeError as e:
        print(f"JSON parse error: {e}")
        print("Raw output snippet:", raw_text[:500])
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
        ws = sh.add_worksheet(title=SHEET_NAME, rows=1000, cols=20)
        ws.append_row(HEADERS)
        # Format header row bold
        ws.format("A1:N1", {"textFormat": {"bold": True}, "backgroundColor": {"red": 0.15, "green": 0.25, "blue": 0.45}, "horizontalAlignment": "CENTER"})
        # Set column widths via API isn't directly supported in gspread, but freeze header
        ws.freeze(rows=1)
    
    return ws

def make_listing_id(listing):
    """Stable hash for deduplication — based on URL or title+platform."""
    key = listing.get("url") or f"{listing.get('listing_title','')}-{listing.get('platform','')}"
    return hashlib.md5(key.encode()).hexdigest()[:10]

def get_existing_ids(ws):
    try:
        col_index = HEADERS.index("Listing ID") + 1
        ids = ws.col_values(col_index)
        return set(ids[1:])  # skip header
    except Exception:
        return set()

def append_new_listings(ws, listings):
    existing_ids = get_existing_ids(ws)
    today        = datetime.utcnow().strftime("%Y-%m-%d")
    new_rows     = []

    for l in listings:
        lid = make_listing_id(l)
        if lid in existing_ids:
            continue  # already captured
        
        row = [
            today,
            l.get("listing_title", ""),
            l.get("platform", ""),
            l.get("url", ""),
            l.get("asking_price", ""),
            l.get("annual_revenue", ""),
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
        print(f"✅ Appended {len(new_rows)} new listing(s) to Google Sheet.")
    else:
        print("No new listings found today — sheet already up to date.")
    
    return len(new_rows)

# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    print(f"=== Tax Resolution Scraper — {datetime.utcnow().date()} ===")
    
    raw      = run_claude_search()
    listings = parse_listings(raw)
    
    if not listings:
        print("No listings parsed. Check Claude output above.")
        return
    
    ws  = get_sheet()
    new = append_new_listings(ws, listings)
    
    print(f"=== Done. {new} new listing(s) added. ===")

if __name__ == "__main__":
    main()
