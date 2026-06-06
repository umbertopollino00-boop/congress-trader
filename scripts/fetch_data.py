"""
Script eseguito da GitHub Actions ogni domenica.
Scarica i 13F da SEC EDGAR e l'earnings calendar da Yahoo Finance.
Salva i risultati in data/13f_cache.json e data/earnings_cache.json
"""

import json, time, requests, os
from datetime import datetime, timedelta
from xml.etree import ElementTree as ET

SEC_HEADERS = {
    "User-Agent": "CongressTraderBot github-actions@users.noreply.github.com",
    "Accept":     "application/json",
}

FUNDS = {
    "Situational Awareness (Aschenbrenner)": "0002045724",
    "Renaissance Technologies (Simons)":     "0001037389",
    "Citadel Advisors (Griffin)":            "0001423053",
}

# ── 13F ───────────────────────────────────────────────────────────────────────

def fetch_13f(fund_name, cik):
    print(f"Fetching 13F for {fund_name} (CIK {cik})…")
    cik_padded = cik.zfill(10)

    # 1. Get submissions
    r = requests.get(
        f"https://data.sec.gov/submissions/CIK{cik_padded}.json",
        headers=SEC_HEADERS, timeout=20
    )
    if r.status_code != 200:
        print(f"  ✗ submissions error: {r.status_code}")
        return []

    data     = r.json()
    filings  = data.get("filings", {}).get("recent", {})
    forms    = filings.get("form", [])
    accs     = filings.get("accessionNumber", [])
    dates    = filings.get("filingDate", [])

    latest_acc = latest_date = None
    for form, acc, date in zip(forms, accs, dates):
        if "13F-HR" in form:
            latest_acc  = acc.replace("-", "")
            latest_date = date
            break

    if not latest_acc:
        print(f"  ✗ no 13F found")
        return []

    print(f"  Latest 13F: {latest_date} ({latest_acc})")
    time.sleep(0.5)

    # 2. Get filing index
    cik_int = int(cik)
    idx_url = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik_int}&type=13F-HR&dateb=&owner=include&count=1&search_text="
    
    # Use direct archive URL
    acc_fmt = f"{latest_acc[:10]}-{latest_acc[10:12]}-{latest_acc[12:]}"
    idx_url = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{latest_acc}/{acc_fmt}-index.json"
    
    r2 = requests.get(idx_url, headers={**SEC_HEADERS, "Accept": "*/*"}, timeout=20)
    
    if r2.status_code != 200:
        # Try alternative index format
        idx_url2 = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{latest_acc}/index.json"
        r2 = requests.get(idx_url2, headers={**SEC_HEADERS, "Accept": "*/*"}, timeout=20)
    
    if r2.status_code != 200:
        print(f"  ✗ index error: {r2.status_code} — trying EDGAR full-text search")
        return _fetch_via_efts(fund_name, cik, latest_date)

    try:
        idx = r2.json()
        items = idx.get("directory", {}).get("item", [])
    except Exception:
        return _fetch_via_efts(fund_name, cik, latest_date)

    # Find infotable XML
    xml_file = None
    for item in items:
        name = item.get("name", "").lower()
        if "infotable" in name and name.endswith(".xml"):
            xml_file = item["name"]
            break
    if not xml_file:
        for item in items:
            if item.get("name", "").lower().endswith(".xml"):
                xml_file = item["name"]
                break

    if not xml_file:
        print(f"  ✗ no XML file found")
        return []

    time.sleep(0.5)
    xml_url = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{latest_acc}/{xml_file}"
    r3 = requests.get(xml_url, headers={**SEC_HEADERS, "Accept": "*/*"}, timeout=20)
    if r3.status_code != 200:
        print(f"  ✗ XML error: {r3.status_code}")
        return []

    return _parse_infotable(r3.content, fund_name, latest_date)


def _fetch_via_efts(fund_name, cik, latest_date):
    """Fallback: EDGAR full-text search to find the infotable file."""
    cik_int = int(cik)
    # Try direct XML URL patterns
    patterns = [
        f"https://www.sec.gov/Archives/edgar/data/{cik_int}/",
    ]
    # Use EDGAR filing page to find the XML
    url = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik_int}&type=13F-HR&dateb=&owner=include&count=1&output=atom"
    r = requests.get(url, headers={"User-Agent": SEC_HEADERS["User-Agent"]}, timeout=15)
    print(f"  EFTS fallback: {r.status_code}")
    return []


def _parse_infotable(xml_content, fund_name, date):
    """Parse 13F infotable XML into list of positions."""
    try:
        root = ET.fromstring(xml_content)
        positions = []
        for entry in root.findall(".//{*}infoTable"):
            def g(tag):
                el = entry.find(f"{{*}}{tag}")
                return el.text.strip() if el is not None and el.text else ""

            ticker   = g("ticker").upper()
            put_call = g("putCall")
            value    = g("value")
            shares   = g("sshPrnamt") or g("shrsOrPrnAmt")
            name     = g("nameOfIssuer")

            if not ticker or put_call in ("Put", "Call"):
                continue
            try:
                positions.append({
                    "ticker":    ticker,
                    "name":      name,
                    "value_usd": float(value) * 1000,
                    "shares":    int(shares),
                    "fund":      fund_name,
                    "date":      date,
                })
            except Exception:
                continue

        positions.sort(key=lambda x: x["value_usd"], reverse=True)
        print(f"  ✓ {len(positions)} positions parsed, top15: {[p['ticker'] for p in positions[:5]]}")
        return positions[:15]
    except Exception as e:
        print(f"  ✗ XML parse error: {e}")
        return []


# ── EARNINGS ──────────────────────────────────────────────────────────────────

def fetch_earnings_surprises():
    """Scarica earnings surprises degli ultimi 7 giorni da Yahoo Finance."""
    print("Fetching earnings surprises…")
    results = []

    # Sample di titoli S&P500 da controllare
    tickers = [
        "AAPL","MSFT","NVDA","GOOGL","META","AMZN","TSLA","AMD","INTC","CRM",
        "NFLX","PYPL","ADBE","QCOM","TXN","MU","AMAT","LRCX","KLAC","MRVL",
        "ORCL","SAP","IBM","CSCO","AVGO","NOW","SNOW","PLTR","UBER","LYFT",
        "SHOP","SQ","COIN","HOOD","RBLX","U","DDOG","ZS","CRWD","NET",
    ]

    import yfinance as yf
    since = (datetime.now() - timedelta(days=14)).strftime("%Y-%m-%d")

    for ticker in tickers:
        try:
            t = yf.Ticker(ticker)
            hist = t.earnings_history
            if hist is None or hist.empty:
                continue
            latest = hist.iloc[-1]
            actual   = float(latest.get("epsActual", 0) or 0)
            estimate = float(latest.get("epsEstimate", 0) or 0)
            surprise = actual - estimate
            date_str = str(latest.name)[:10] if hasattr(latest.name, "__str__") else ""
            if surprise > 0 and date_str >= since:
                results.append({
                    "ticker":   ticker,
                    "surprise": round(surprise, 4),
                    "actual":   round(actual, 4),
                    "estimate": round(estimate, 4),
                    "date":     date_str,
                })
        except Exception as e:
            pass

    print(f"  ✓ {len(results)} positive surprises found")
    return results


# ── MAIN ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    os.makedirs("data", exist_ok=True)

    # 1. Fetch 13F
    cache_13f = {}
    for fund_name, cik in FUNDS.items():
        positions = fetch_13f(fund_name, cik)
        cache_13f[fund_name] = positions
        time.sleep(1)  # rispetta rate limit SEC

    with open("data/13f_cache.json", "w") as f:
        json.dump({
            "updated_at": datetime.utcnow().isoformat(),
            "funds": cache_13f,
        }, f, indent=2)
    print(f"\n✓ 13F cache saved → data/13f_cache.json")

    # 2. Fetch earnings
    earnings = fetch_earnings_surprises()
    with open("data/earnings_cache.json", "w") as f:
        json.dump({
            "updated_at": datetime.utcnow().isoformat(),
            "surprises": earnings,
        }, f, indent=2)
    print(f"✓ Earnings cache saved → data/earnings_cache.json")
