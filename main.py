from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import anthropic
import httpx
import json

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env


class AnalyzeRequest(BaseModel):
    ticker: str


def get_cik(ticker: str) -> str:
    """Look up SEC CIK number for a ticker symbol."""
    url = "https://efts.sec.gov/LATEST/search-index?q=%22{}%22&dateRange=custom&startdt=2020-01-01&category=form-type&forms=10-K".format(ticker)
    # Use the company tickers JSON from SEC
    tickers_url = "https://www.sec.gov/files/company_tickers.json"
    with httpx.Client(headers={"User-Agent": "FinancialAnalyzer contact@example.com"}) as http:
        resp = http.get(tickers_url)
        resp.raise_for_status()
        data = resp.json()
    for entry in data.values():
        if entry["ticker"].upper() == ticker.upper():
            return str(entry["cik_str"]).zfill(10)
    raise HTTPException(status_code=404, detail=f"Ticker '{ticker}' not found in SEC database")


def get_latest_10k_url(cik: str) -> tuple[str, str]:
    url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    with httpx.Client(headers={"User-Agent": "FinancialAnalyzer contact@example.com"}) as http:
        resp = http.get(url)
        resp.raise_for_status()
        data = resp.json()

    company_name = data.get("name", "Unknown")
    filings = data["filings"]["recent"]
    forms = filings["form"]
    accession_numbers = filings["accessionNumber"]

    for i, form in enumerate(forms):
        if form == "10-K":
            accession = accession_numbers[i].replace("-", "")
            # Return the filing index page instead of primary doc
            index_url = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={int(cik)}&type=10-K&dateb=&owner=include&count=1"
            filing_index = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession}/0000320193-24-000123-index.htm"
            # Use the XBRL viewer data endpoint which has structured financials
            facts_url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
            return facts_url, company_name

    raise HTTPException(status_code=404, detail="No 10-K filing found")


def fetch_filing_text(url: str) -> str:
    """Fetch structured financial facts from SEC XBRL API."""
    with httpx.Client(
        headers={"User-Agent": "FinancialAnalyzer contact@example.com"},
        timeout=30.0
    ) as http:
        resp = http.get(url)
        resp.raise_for_status()
        data = resp.json()

    # Extract key financial facts from XBRL data
    facts = data.get("facts", {})
    us_gaap = facts.get("us-gaap", {})
    dei = facts.get("dei", {})

    def get_latest_value(concept: dict) -> str:
        try:
            units = concept.get("units", {})
            values = units.get("USD", units.get("shares", []))
            # Filter for annual (10-K) filings only
            annual = [v for v in values if v.get("form") == "10-K"]
            if annual:
                latest = sorted(annual, key=lambda x: x["end"])[-1]
                val = latest["val"]
                # Format large numbers
                if val >= 1_000_000_000:
                    return f"${val/1_000_000_000:.2f}B"
                elif val >= 1_000_000:
                    return f"${val/1_000_000:.2f}M"
                return str(val)
        except:
            pass
        return "N/A"

    # Build a structured text summary for Claude
    revenue = get_latest_value(us_gaap.get("RevenueFromContractWithCustomerExcludingAssessedTax", 
                               us_gaap.get("Revenues", us_gaap.get("SalesRevenueNet", {}))))
    net_income = get_latest_value(us_gaap.get("NetIncomeLoss", {}))
    total_assets = get_latest_value(us_gaap.get("Assets", {}))
    total_debt = get_latest_value(us_gaap.get("LongTermDebt", us_gaap.get("LongTermDebtNoncurrent", {})))
    cash = get_latest_value(us_gaap.get("CashAndCashEquivalentsAtCarryingValue", {}))
    gross_profit = get_latest_value(us_gaap.get("GrossProfit", {}))

    summary = f"""
Company Financial Data from SEC XBRL:
Revenue: {revenue}
Net Income: {net_income}
Total Assets: {total_assets}
Long Term Debt: {total_debt}
Cash and Equivalents: {cash}
Gross Profit: {gross_profit}
"""
    return summary


@app.post("/analyze")
async def analyze(req: AnalyzeRequest):
    ticker = req.ticker.strip().upper()

    # 1. Get CIK
    cik = get_cik(ticker)

    # 2. Get latest 10-K URL
    filing_url, company_name = get_latest_10k_url(cik)

    # 3. Fetch filing text
    filing_text = fetch_filing_text(filing_url)

    # 4. Send to Claude
    prompt = f"""You are a financial analyst. Below is an excerpt from {company_name}'s most recent 10-K SEC filing.

Extract and analyze the following. Respond ONLY with a valid JSON object, no markdown, no preamble:

{{
  "company": "<company name>",
  "ticker": "<ticker>",
  "fiscal_year": "<fiscal year end>",
  "summary": "<2-3 sentence plain English summary of business and financial health>",
  "key_metrics": {{
    "revenue": "<revenue with units>",
    "net_income": "<net income with units>",
    "total_assets": "<total assets with units>",
    "total_debt": "<total debt with units>",
    "cash": "<cash and equivalents with units>"
  }},
  "ratios": {{
    "gross_margin": "<gross margin %>",
    "net_margin": "<net margin %>",
    "debt_to_equity": "<ratio>"
  }},
  "risks": ["<top risk 1>", "<top risk 2>", "<top risk 3>"],
  "outlook": "<1-2 sentence forward looking statement from management>"
}}

If a value cannot be found, use "N/A".

10-K EXCERPT:
{filing_text}"""

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1000,
        messages=[{"role": "user", "content": prompt}]
    )

    raw = message.content[0].text.strip()

    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        # Try to extract JSON if Claude added any extra text
        import re
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            result = json.loads(match.group())
        else:
            raise HTTPException(status_code=500, detail="Failed to parse Claude response")

    result["filing_url"] = filing_url
    return result
