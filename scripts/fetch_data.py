"""
Script eseguito da GitHub Actions ogni domenica.
Usa sec-api.io (piano gratuito, 100 req/mese) per i 13F.
Salva i risultati in data/13f_cache.json e data/earnings_cache.json
"""

import json, time, requests, os
from datetime import datetime, timedelta

SEC_API_KEY = os.environ.get("SEC_API_KEY", "")

FUNDS = {
    "Situational Awareness (Aschenbrenner)": "0002045724",
    "Renaissance Technologies (Simons)":     "0001037389",
    "Citadel Advisors (Griffin)":            "0001423053",
}

# ── 13F via sec-api.io ────────────────────────────────────────────────────────

def fetch_13f_secapi(fund_name: str, cik: str) -> list[dict]:
    """Fetch top 15 positions via sec-api.io free tier."""
    print(f"Fetching 13F for {fund_name} (CIK {cik})…")
    if not SEC_API_KEY:
        print("  ✗ SEC_API_KEY not set")
        return []

    # Step 1: find latest 13F-HR filing
    query_url = "https://api.sec-api.io/form-13f"
    headers   = {"Authorization": SEC_API_KEY, "Content-Type": "application/json"}
    payload   = {
        "query": {
            "query_string": {
                "query": f'cik:"{cik.lstrip("0")}" AND formType:"13F-HR"'
            }
        },
        "from": "0",
        "size": "1",
        "sort": [{"filedAt": {"order": "desc"}}]
    }

    try:
        r = requests.post(query_url, headers=headers, json=payload, timeout=20)
        if r.status_code != 200:
            print(f"  ✗ Query error: {r.status_code} — {r.text[:150]}")
            return []

        filings = r.json().get("filings", [])
        if not filings:
            print(f"  ✗ No 13F filings found")
            return []

        filing   = filings[0]
        filed_at = filing.get("filedAt", "")[:10]
        period   = filing.get("periodOfReport", filed_at)
        holdings = filing.get("holdings", [])

        print(f"  Filing: {filed_at} | Period: {period} | {len(holdings)} holdings")

        # Filter out options, sort by value
        positions = []
        for h in holdings:
            option = (h.get("putCall") or "").upper()
            if option in ("PUT", "CALL"):
                continue
            ticker = (h.get("cusip") or "")  # sec-api may return cusip not ticker
            # Try ticker field directly
            ticker = h.get("ticker") or h.get("nameOfIssuer", "").split()[0]
            ticker = ticker.upper().strip()
            if not ticker or len(ticker) > 6:
                continue
            try:
                value  = float(h.get("value", 0)) * 1000  # value in $thousands
                shares = int(h.get("shrsOrPrnAmt", {}).get("sshPrnamt", 0)
                             if isinstance(h.get("shrsOrPrnAmt"), dict)
                             else h.get("shares", 0))
                positions.append({
                    "ticker":    ticker,
                    "name":      h.get("nameOfIssuer", ticker),
                    "value_usd": value,
                    "shares":    shares,
                    "fund":      fund_name,
                    "date":      period,
                })
            except Exception:
                continue

        positions.sort(key=lambda x: x["value_usd"], reverse=True)
        top15 = positions[:15]
        print(f"  ✓ Top 15: {[p['ticker'] for p in top15]}")
        return top15

    except Exception as e:
        print(f"  ✗ Error: {e}")
        return []


# ── EARNINGS via Yahoo Finance ────────────────────────────────────────────────

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

    # 1. Fetch 13F
    cache_13f = {}
    for fund_name, cik in FUNDS.items():
        cache_13f[fund_name] = fetch_13f_secapi(fund_name, cik)
        time.sleep(1)

    with open("data/13f_cache.json", "w") as f:
        json.dump({"updated_at": datetime.utcnow().isoformat(), "funds": cache_13f}, f, indent=2)
    print("✓ 13F cache saved → data/13f_cache.json")

    # 2. Fetch earnings
    earnings = fetch_earnings_surprises()
    with open("data/earnings_cache.json", "w") as f:
        json.dump({"updated_at": datetime.utcnow().isoformat(), "surprises": earnings}, f, indent=2)
    print("✓ Earnings cache saved → data/earnings_cache.json")
