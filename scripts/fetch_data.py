"""
Script eseguito da GitHub Actions ogni domenica.
Usa sec-api.io Query API per i 13F.
"""

import json, time, requests, os
from datetime import datetime, timedelta

SEC_API_KEY = os.environ.get("SEC_API_KEY", "")

FUNDS = {
    "Situational Awareness (Aschenbrenner)": "2045724",
    "Renaissance Technologies (Simons)":     "1037389",
    "Citadel Advisors (Griffin)":            "1423053",
}

# ── 13F via sec-api.io Query API ──────────────────────────────────────────────

def fetch_13f_secapi(fund_name: str, cik: str) -> list[dict]:
    print(f"Fetching 13F for {fund_name} (CIK {cik})…")
    if not SEC_API_KEY:
        print("  ✗ SEC_API_KEY not set")
        return []

    # Step 1: find latest 13F-HR filing via Query API
    headers = {"Authorization": SEC_API_KEY, "Content-Type": "application/json"}
    payload = {
        "query": {
            "query_string": {
                "query": f'cik:"{cik}" AND formType:"13F-HR"'
            }
        },
        "from": "0",
        "size": "1",
        "sort": [{"filedAt": {"order": "desc"}}]
    }

    try:
        r = requests.post(
            "https://api.sec-api.io",
            headers=headers,
            json=payload,
            timeout=20
        )
        if r.status_code != 200:
            print(f"  ✗ Query error: {r.status_code} — {r.text[:200]}")
            return []

        data     = r.json()
        filings  = data.get("filings", [])
        if not filings:
            print(f"  ✗ No filings found")
            return []

        filing      = filings[0]
        filed_at    = filing.get("filedAt", "")[:10]
        period      = filing.get("periodOfReport", filed_at)
        accession   = filing.get("accessionNo", "").replace("-", "")
        print(f"  Latest 13F: {filed_at} | Period: {period} | Accession: {accession}")

        if not accession:
            print("  ✗ No accession number")
            return []

        # Step 2: fetch holdings via 13F Holdings API
        time.sleep(0.5)
        holdings_url = f"https://api.sec-api.io/form-13f/{accession}"
        r2 = requests.get(holdings_url, headers=headers, timeout=20)

        if r2.status_code != 200:
            print(f"  ✗ Holdings error: {r2.status_code} — {r2.text[:200]}")
            return []

        holdings_data = r2.json()
        holdings = (
            holdings_data.get("holdings") or
            holdings_data.get("data", {}).get("holdings") or
            holdings_data.get("tableData") or
            []
        )

        print(f"  {len(holdings)} raw holdings found")

        # Parse positions — filter out options
        positions = []
        for h in holdings:
            option = (h.get("putCall") or h.get("option") or "").upper()
            if option in ("PUT", "CALL"):
                continue

            ticker = (h.get("ticker") or "").upper().strip()
            name   = h.get("nameOfIssuer") or h.get("issuerName") or ticker

            # value in $thousands
            raw_value = h.get("value") or h.get("marketValue") or 0
            try:
                value_usd = float(raw_value) * 1000
            except Exception:
                continue

            # shares
            raw_shares = (
                h.get("shrsOrPrnAmt", {}).get("sshPrnamt")
                if isinstance(h.get("shrsOrPrnAmt"), dict)
                else h.get("shares") or h.get("sharesHeld") or 0
            )
            try:
                shares = int(raw_shares)
            except Exception:
                shares = 0

            if not ticker or len(ticker) > 6 or value_usd <= 0:
                continue

            positions.append({
                "ticker":    ticker,
                "name":      name,
                "value_usd": value_usd,
                "shares":    shares,
                "fund":      fund_name,
                "date":      period,
            })

        positions.sort(key=lambda x: x["value_usd"], reverse=True)
        top15 = positions[:15]
        print(f"  ✓ Top 15: {[p['ticker'] for p in top15]}")
        return top15

    except Exception as e:
        print(f"  ✗ Exception: {e}")
        return []


# ── EARNINGS via yfinance ─────────────────────────────────────────────────────

def fetch_earnings_surprises() -> list[dict]:
    print("Fetching earnings surprises via yfinance…")
    results = []
    tickers = [
        "AAPL","MSFT","NVDA","GOOGL","META","AMZN","TSLA","AMD","INTC","CRM",
        "NFLX","ADBE","QCOM","TXN","MU","AMAT","LRCX","MRVL","ORCL","AVGO",
        "NOW","SNOW","PLTR","UBER","DDOG","ZS","CRWD","NET","ARM","SMCI",
    ]
    try:
        import yfinance as yf
        since = (datetime.now() - timedelta(days=14)).strftime("%Y-%m-%d")
        for ticker in tickers:
            try:
                hist = yf.Ticker(ticker).earnings_history
                if hist is None or hist.empty:
                    continue
                latest   = hist.iloc[-1]
                actual   = float(latest.get("epsActual",   0) or 0)
                estimate = float(latest.get("epsEstimate", 0) or 0)
                surprise = actual - estimate
                date_str = str(latest.name)[:10]
                if surprise > 0 and date_str >= since:
                    results.append({
                        "ticker":   ticker,
                        "surprise": round(surprise, 4),
                        "actual":   round(actual,   4),
                        "estimate": round(estimate, 4),
                        "date":     date_str,
                    })
            except Exception:
                continue
    except Exception as e:
        print(f"  yfinance error: {e}")

    print(f"  ✓ {len(results)} positive surprises found")
    return results


# ── MAIN ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    os.makedirs("data", exist_ok=True)

    cache_13f = {}
    for fund_name, cik in FUNDS.items():
        cache_13f[fund_name] = fetch_13f_secapi(fund_name, cik)
        time.sleep(1)

    with open("data/13f_cache.json", "w") as f:
        json.dump({"updated_at": datetime.utcnow().isoformat(), "funds": cache_13f}, f, indent=2)
    print("✓ 13F cache saved → data/13f_cache.json")

    earnings = fetch_earnings_surprises()
    with open("data/earnings_cache.json", "w") as f:
        json.dump({"updated_at": datetime.utcnow().isoformat(), "surprises": earnings}, f, indent=2)
    print("✓ Earnings cache saved → data/earnings_cache.json")
