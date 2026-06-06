"""
Congress Trader Bot — v4
Combina 4 strategie in un unico bot:
  1. Congressional Trades (Quiver Quantitative)
  2. Hedge Fund 13F: Situational Awareness (Aschenbrenner), Renaissance, Citadel, Buffett, Ackman
  3. GenAI Earnings (PEAD + LLM transcript scoring via Claude API)
Esecuzione su Alpaca Paper. Report giornaliero via email (Mailjet).
"""

import os, json, time, logging, smtplib, requests, schedule
from datetime import datetime, timedelta
from collections import defaultdict
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from xml.etree import ElementTree as ET
import numpy as np
from dotenv import load_dotenv

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
ALPACA_KEY      = os.getenv("ALPACA_KEY")
ALPACA_SECRET   = os.getenv("ALPACA_SECRET")
GMAIL_USER      = os.getenv("GMAIL_USER")
GMAIL_APP_PWD   = os.getenv("GMAIL_APP_PWD")
EMAIL_TO        = os.getenv("EMAIL_TO", GMAIL_USER)
MAILJET_KEY     = os.getenv("MAILJET_KEY", "")
MAILJET_SECRET  = os.getenv("MAILJET_SECRET", "")
QUIVER_KEY      = os.getenv("QUIVER_KEY", "")
TOP_N_MEMBERS   = int(os.getenv("TOP_N_MEMBERS", "5"))
TRADE_SIZE_USD  = float(os.getenv("TRADE_SIZE_USD", "500"))
DRY_RUN         = os.getenv("DRY_RUN", "false").lower() == "true"
ENABLE_PEAD     = os.getenv("ENABLE_PEAD", "true").lower() == "true"

QH = {"Accept": "application/json"}
if QUIVER_KEY:
    QH["Authorization"] = f"Token {QUIVER_KEY}"

SEC_HEADERS = {"User-Agent": "CongressTraderBot research@example.com", "Accept": "application/json"}

# ── Hedge Funds (CIK SEC) ─────────────────────────────────────────────────────
HEDGE_FUNDS = {
    "Situational Awareness (Aschenbrenner)": "0002045724",
    "Renaissance Technologies (Simons)":     "0001037389",
    "Citadel Advisors (Griffin)":            "0001423053",
}

# ══════════════════════════════════════════════════════════════════════════════
# 1. CONGRESSIONAL TRADES
# ══════════════════════════════════════════════════════════════════════════════

def fetch_congressional_trades(days: int = 365) -> list[dict]:
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    log.info(f"[Congress] Fetching trades since {since}…")
    try:
        r = requests.get("https://api.quiverquant.com/beta/live/congresstrading",
                         headers=QH, timeout=20)
        r.raise_for_status()
        trades = [t for t in r.json() if t.get("TransactionDate","9999") >= since]
        log.info(f"[Congress] {len(trades)} trades fetched")
        return trades
    except Exception as e:
        log.error(f"[Congress] fetch error: {e}")
        return []

def rank_congress_members(trades, top_n=TOP_N_MEMBERS):
    buys = defaultdict(lambda: {"name":"","party":"","count":0,"tickers":[]})
    for t in trades:
        tx = (t.get("Transaction") or "").upper()
        if "PURCHASE" not in tx and "BUY" not in tx:
            continue
        name   = t.get("Representative") or "Unknown"
        ticker = (t.get("Ticker") or "").upper().strip()
        if name == "Unknown" or not ticker:
            continue
        buys[name]["name"]    = name
        buys[name]["party"]   = t.get("Party","")
        buys[name]["count"]  += 1
        if ticker not in buys[name]["tickers"]:
            buys[name]["tickers"].append(ticker)
    ranked = sorted(buys.values(), key=lambda x: x["count"], reverse=True)[:top_n]
    for m in ranked:
        m["return_pct"] = float(m["count"])
        m["chamber"]    = ""
    log.info(f"[Congress] Top members: {[m['name'] for m in ranked]}")
    return ranked

def get_recent_congress_trades(member_name, all_trades, days=7):
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    result = []
    for t in all_trades:
        if (t.get("Representative") or "") != member_name:
            continue
        date   = t.get("TransactionDate","")
        if date < since:
            continue
        ticker = (t.get("Ticker") or "").upper().strip()
        if not ticker or ticker in ("--","N/A"):
            continue
        tx        = (t.get("Transaction") or "").upper()
        direction = "buy" if ("PURCHASE" in tx or "BUY" in tx) else "sell"
        result.append({"ticker": ticker, "direction": direction,
                        "date": date[:10], "member": member_name, "source": "Congress"})
    return result

# ══════════════════════════════════════════════════════════════════════════════
# 2. HEDGE FUND 13F — via Quiver Quantitative sec13f endpoint
# ══════════════════════════════════════════════════════════════════════════════

# Fund name → owner string used by Quiver sec13f API
FUND_OWNERS = {
    "Situational Awareness (Aschenbrenner)": "SITUATIONAL AWARENESS",
    "Renaissance Technologies (Simons)":     "RENAISSANCE TECHNOLOGIES",
    "Citadel Advisors (Griffin)":            "CITADEL ADVISORS",
}

def fetch_13f_quiver(fund_name: str, owner_query: str) -> list[dict]:
    """Fetch top 15 positions for a fund via Quiver sec13f endpoint."""
    log.info(f"[13F] Fetching {fund_name}…")
    url = f"https://api.quiverquant.com/beta/live/sec13f?owner={requests.utils.quote(owner_query)}"
    headers = {"Accept": "application/json"}
    if QUIVER_KEY:
        headers["Authorization"] = f"Token {QUIVER_KEY}"
    try:
        r = requests.get(url, headers=headers, timeout=20)
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, str):
                log.warning(f"[13F] {fund_name}: {data}")
                return []
            # Sort by value descending, take top 15
            positions = []
            for item in data:
                ticker = (item.get("Ticker") or "").upper().strip()
                if not ticker or ticker in ("--", "N/A"):
                    continue
                try:
                    value = float(item.get("Value") or item.get("value") or 0)
                    shares = int(item.get("Shares") or item.get("shares") or 0)
                    period = str(item.get("ReportPeriod") or item.get("Date") or "")[:10]
                except Exception:
                    continue
                positions.append({
                    "ticker":    ticker,
                    "name":      item.get("SecurityName") or item.get("Name") or ticker,
                    "value_usd": value * 1000,   # Quiver reports in $thousands
                    "shares":    shares,
                    "fund":      fund_name,
                    "date":      period,
                })
            positions.sort(key=lambda x: x["value_usd"], reverse=True)
            top15 = positions[:15]
            log.info(f"[13F] {fund_name}: {len(top15)} positions")
            return top15
        else:
            log.warning(f"[13F] {fund_name}: HTTP {r.status_code} — {r.text[:100]}")
            return []
    except Exception as e:
        log.error(f"[13F] {fund_name} error: {e}")
        return []

GITHUB_RAW = os.getenv("GITHUB_RAW_BASE", "")
# Format: https://raw.githubusercontent.com/USERNAME/REPO/main

def fetch_all_13f() -> dict:
    """
    Legge i dati 13F dal cache JSON aggiornato da GitHub Actions ogni domenica.
    Fallback su Quiver se il cache non è disponibile.
    """
    if GITHUB_RAW:
        url = f"{GITHUB_RAW}/data/13f_cache.json"
        try:
            r = requests.get(url, timeout=10)
            if r.status_code == 200:
                data = r.json()
                funds = data.get("funds", {})
                updated = data.get("updated_at", "")[:10]
                total = sum(len(v) for v in funds.values())
                log.info(f"[13F] Cache loaded from GitHub (updated {updated}, {total} total positions)")
                # Ensure all expected funds are present
                for name in FUND_OWNERS:
                    if name not in funds:
                        funds[name] = []
                return funds
            else:
                log.warning(f"[13F] GitHub cache fetch error: {r.status_code}")
        except Exception as e:
            log.warning(f"[13F] GitHub cache error: {e}")

    # Fallback: try Quiver
    log.info("[13F] Falling back to Quiver sec13f endpoint…")
    results = {}
    for name, owner in FUND_OWNERS.items():
        results[name] = fetch_13f_quiver(name, owner)
        time.sleep(0.3)
    return results

def get_13f_trades(fund_positions):
    """Top 15 posizioni per fondo → trades BUY per Alpaca."""
    trades = []
    seen   = set()
    for fund, positions in fund_positions.items():
        for p in positions[:15]:
            t = p["ticker"]
            if t in seen:
                continue
            seen.add(t)
            trades.append({
                "ticker":    t,
                "direction": "buy",
                "date":      p["date"],
                "member":    fund,
                "source":    "13F",
                "value_usd": p["value_usd"],
            })
    return trades

def compute_overlap(congress_members, fund_positions) -> list[dict]:
    """
    Trova i ticker presenti sia nei portafogli dei congressisti
    sia nelle posizioni dei fondi hedge — segnale ad alta conviction.
    """
    # Raccogli tutti i ticker dei congressisti
    congress_tickers = set()
    for m in congress_members:
        for t in m.get("tickers", []):
            congress_tickers.add(t.upper())

    # Conta overlap con i fondi
    overlap = {}
    for fund, positions in fund_positions.items():
        for p in positions:
            t = p["ticker"].upper()
            if t in congress_tickers:
                if t not in overlap:
                    overlap[t] = {
                        "ticker":    t,
                        "name":      p.get("name", t),
                        "funds":     [],
                        "value_usd": 0,
                    }
                overlap[t]["funds"].append(fund.split("(")[0].strip())
                overlap[t]["value_usd"] += p["value_usd"]

    result = sorted(overlap.values(), key=lambda x: (len(x["funds"]), x["value_usd"]), reverse=True)
    log.info(f"[Overlap] {len(result)} ticker in comune tra politici e fondi")
    return result

# ══════════════════════════════════════════════════════════════════════════════
# 3. PEAD — Gen AI Earnings (replica fedele del whitepaper)
# ══════════════════════════════════════════════════════════════════════════════

def fetch_earnings_surprises() -> list[dict]:
    """
    Legge i dati di earnings surprise dal cache JSON aggiornato da GitHub Actions.
    Fallback su Quiver o Yahoo se il cache non è disponibile.
    """
    log.info("[PEAD] Fetching earnings surprises…")

    # 1. Prova il cache GitHub
    if GITHUB_RAW:
        url = f"{GITHUB_RAW}/data/earnings_cache.json"
        try:
            r = requests.get(url, timeout=10)
            if r.status_code == 200:
                data = r.json()
                surprises = data.get("surprises", [])
                updated   = data.get("updated_at", "")[:10]
                since     = (datetime.now() - timedelta(days=14)).strftime("%Y-%m-%d")
                recent    = [s for s in surprises if s.get("date","") >= since]
                log.info(f"[PEAD] {len(recent)} surprises from GitHub cache (updated {updated})")
                return recent
        except Exception as e:
            log.warning(f"[PEAD] GitHub earnings cache error: {e}")

    # 2. Fallback Quiver
    candidates = []
    try:
        r = requests.get("https://api.quiverquant.com/beta/live/earningssurprises",
                         headers=QH, timeout=15)
        if r.status_code == 200:
            data = r.json()
            since = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
            for item in data:
                date = item.get("Date","")
                if date < since:
                    continue
                eps_actual   = item.get("Actual") or item.get("EPS_Actual")
                eps_estimate = item.get("Estimate") or item.get("EPS_Estimate")
                ticker       = (item.get("Ticker") or "").upper()
                if not ticker or eps_actual is None or eps_estimate is None:
                    continue
                try:
                    surprise = float(eps_actual) - float(eps_estimate)
                    if surprise > 0:
                        candidates.append({"ticker": ticker, "surprise": surprise,
                                           "date": date, "actual": float(eps_actual),
                                           "estimate": float(eps_estimate)})
                except Exception:
                    continue
            log.info(f"[PEAD] {len(candidates)} surprises from Quiver")
    except Exception as e:
        log.warning(f"[PEAD] Quiver error: {e}")

    if not candidates:
        candidates = _fetch_yahoo_earnings()

    return candidates

def _fetch_yahoo_earnings() -> list[dict]:
    """Fallback: Yahoo Finance earnings calendar per la settimana corrente."""
    try:
        import yfinance as yf
        # Usa un campione di ticker S&P500 noti per trovare earnings recenti
        sp500_sample = [
            "AAPL","MSFT","NVDA","GOOGL","META","AMZN","TSLA","AMD","INTC","CRM",
            "NFLX","PYPL","ADBE","QCOM","TXN","MU","AMAT","LRCX","KLAC","MRVL",
        ]
        candidates = []
        for ticker in sp500_sample:
            try:
                info = yf.Ticker(ticker).earnings_history
                if info is None or info.empty:
                    continue
                latest = info.iloc[-1]
                surprise = float(latest.get("epsActual",0)) - float(latest.get("epsEstimate",0))
                if surprise > 0:
                    candidates.append({
                        "ticker":   ticker,
                        "surprise": surprise,
                        "date":     str(latest.name)[:10] if hasattr(latest.name,"__str__") else "",
                        "actual":   float(latest.get("epsActual",0)),
                        "estimate": float(latest.get("epsEstimate",0)),
                    })
            except Exception:
                continue
        log.info(f"[PEAD] {len(candidates)} candidates from Yahoo Finance")
        return candidates
    except Exception as e:
        log.error(f"[PEAD] Yahoo fallback error: {e}")
        return []

def compute_sue(candidates: list[dict]) -> list[dict]:
    """
    Gate 2: Standardized Unexpected Earnings (SUE).
    SUE = surprise / std(historical_surprises) — solo surprises > 0 e SUE > 0.5
    """
    scored = []
    for c in candidates:
        # Semplificazione: normalizza per il valore assoluto dell'estimate
        estimate = abs(c.get("estimate", 1)) or 1
        sue = c["surprise"] / estimate
        if sue > 0.1:  # soglia minima
            c["sue"] = sue
            scored.append(c)
    scored.sort(key=lambda x: x["sue"], reverse=True)
    return scored

def compute_announcement_return(candidates: list[dict]) -> list[dict]:
    """Gate 3: verifica che la reazione di mercato sia positiva (abnormal return > 0)."""
    try:
        import yfinance as yf
        passed = []
        for c in candidates:
            try:
                hist = yf.Ticker(c["ticker"]).history(period="5d")
                if hist.empty or len(hist) < 2:
                    continue
                ret = (hist["Close"].iloc[-1] - hist["Close"].iloc[-2]) / hist["Close"].iloc[-2]
                if ret > 0:
                    c["ann_return"] = ret
                    passed.append(c)
            except Exception:
                continue
        log.info(f"[PEAD] Gate 3 passed: {len(passed)}/{len(candidates)}")
        return passed
    except Exception:
        return candidates  # se yfinance non disponibile, passa tutti

def score_transcripts_llm(candidates: list[dict]) -> list[dict]:
    """
    Gate 4: AI Transcript Score via Claude API.
    Analizza il sentiment dei 7 temi del whitepaper sulle ultime news del ticker.
    """
    if not candidates:
        return []

    log.info(f"[PEAD] Scoring {len(candidates)} transcripts via Claude API…")
    scored = []

    THEMES = [
        "Revenue backlog (unfilled orders/contracted revenue)",
        "Contract length (duration of agreements)",
        "Outlook/guidance (forward guidance tone)",
        "Revenue momentum (top-line growth trajectory)",
        "Management tone (confidence and specificity)",
        "Q&A resilience (quality of analyst Q&A responses)",
        "Capital allocation (buybacks vs equity issuance)",
    ]

    for c in candidates:
        ticker = c["ticker"]
        prompt = f"""You are analyzing an earnings call for {ticker}.
The company reported EPS of {c['actual']:.2f} vs estimate {c['estimate']:.2f} (surprise: +{c['surprise']:.2f}).

Score this company on these 7 themes based on what you know about its most recent earnings:
{chr(10).join(f'{i+1}. {t}' for i,t in enumerate(THEMES))}

For each theme, assign a score from -1 (very negative) to +1 (very positive).
Also assign a weight (0-1) reflecting how material this theme is for this company.
Weights must sum to 1.0.

Respond ONLY with valid JSON:
{{"scores": [s1,s2,s3,s4,s5,s6,s7], "weights": [w1,w2,w3,w4,w5,w6,w7], "summary": "one line"}}"""

        try:
            resp = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={"Content-Type": "application/json"},
                json={
                    "model": "claude-sonnet-4-20250514",
                    "max_tokens": 300,
                    "messages": [{"role": "user", "content": prompt}]
                },
                timeout=20,
            )
            if resp.status_code == 200:
                text = resp.json()["content"][0]["text"].strip()
                text = text.replace("```json","").replace("```","").strip()
                data = json.loads(text)
                scores  = data.get("scores",[0]*7)
                weights = data.get("weights",[1/7]*7)
                # Normalizza pesi
                w_sum = sum(weights) or 1
                weights = [w/w_sum for w in weights]
                composite = sum(s*w for s,w in zip(scores,weights))
                c["transcript_score"] = composite
                c["transcript_summary"] = data.get("summary","")
                if composite > 0:
                    scored.append(c)
            else:
                # Se API non disponibile, passa con score neutro
                c["transcript_score"] = 0.5
                c["transcript_summary"] = "Score non disponibile"
                scored.append(c)
        except Exception as e:
            log.warning(f"[PEAD] Transcript score error {ticker}: {e}")
            c["transcript_score"] = 0.3
            scored.append(c)

    scored.sort(key=lambda x: x.get("transcript_score",0), reverse=True)
    return scored

def risk_parity_weights(tickers: list[str]) -> dict[str, float]:
    """
    Inverse-volatility weighting (risk parity semplificato).
    Usa volatilità storica 30 giorni.
    """
    try:
        import yfinance as yf
        vols = {}
        for t in tickers:
            try:
                hist = yf.Ticker(t).history(period="30d")["Close"]
                if len(hist) > 5:
                    ret  = hist.pct_change().dropna()
                    vols[t] = float(ret.std()) or 0.02
            except Exception:
                vols[t] = 0.02
        if not vols:
            return {t: 1/len(tickers) for t in tickers}
        inv_vols = {t: 1/v for t,v in vols.items()}
        total    = sum(inv_vols.values())
        weights  = {t: max(v/total, 0.03) for t,v in inv_vols.items()}  # min 3%
        return weights
    except Exception:
        return {t: 1/len(tickers) for t in tickers}

def run_pead_strategy() -> list[dict]:
    """
    Esegue la pipeline completa Gen AI Earnings (PEAD):
    Gate1 → Gate2(SUE) → Gate3(ann.return) → Gate4(LLM) → risk parity sizing
    """
    if not ENABLE_PEAD:
        return []

    log.info("[PEAD] Running Gen AI Earnings pipeline…")

    # Gate 1+2: earnings surprises positive + SUE
    candidates = fetch_earnings_surprises()
    candidates = compute_sue(candidates)
    if not candidates:
        log.info("[PEAD] No candidates after Gate 2")
        return []

    # Gate 3: positive announcement return
    candidates = compute_announcement_return(candidates[:50])

    # Gate 4: AI transcript score
    candidates = score_transcripts_llm(candidates[:30])

    # Seleziona top 10-20
    final = candidates[:15]
    if not final:
        log.info("[PEAD] No stocks passed all 4 gates")
        return []

    tickers = [c["ticker"] for c in final]
    weights = risk_parity_weights(tickers)

    trades = []
    for c in final:
        trades.append({
            "ticker":     c["ticker"],
            "direction":  "buy",
            "date":       c.get("date",""),
            "member":     "Gen AI Earnings (PEAD)",
            "source":     "PEAD",
            "sue":        round(c.get("sue",0), 3),
            "transcript": round(c.get("transcript_score",0), 2),
            "summary":    c.get("transcript_summary",""),
            "weight":     round(weights.get(c["ticker"], 1/len(final)), 3),
        })

    log.info(f"[PEAD] {len(trades)} stocks selected")
    return trades

# ══════════════════════════════════════════════════════════════════════════════
# 4. ALPACA EXECUTION
# ══════════════════════════════════════════════════════════════════════════════

def get_client():
    return TradingClient(ALPACA_KEY, ALPACA_SECRET, paper=True)

def execute_trades(trades: list[dict]) -> list[dict]:
    if not trades:
        return []
    client  = get_client()
    account = client.get_account()
    bp      = float(account.buying_power)
    log.info(f"[Alpaca] Buying power: ${bp:,.2f}")

    results, seen = [], set()
    for trade in trades:
        t    = trade["ticker"]
        key  = f"{t}_{trade.get('source','')}"
        if key in seen:
            continue
        seen.add(key)

        side = OrderSide.BUY if trade["direction"] == "buy" else OrderSide.SELL

        try:
            asset = client.get_asset(t)
            if not (asset.tradable and asset.status == "active"):
                raise ValueError("not tradable")
        except Exception:
            results.append({**trade, "status": "skipped", "reason": "not tradable"})
            continue

        if side == OrderSide.SELL:
            positions = {p.symbol: float(p.qty) for p in client.get_all_positions()}
            if t not in positions:
                results.append({**trade, "status": "skipped", "reason": "no position"})
                continue

        # Risk parity sizing per PEAD, fisso per gli altri
        if trade.get("source") == "PEAD" and trade.get("weight"):
            notional = min(TRADE_SIZE_USD * trade["weight"] * 20, bp * 0.05)
        else:
            notional = min(TRADE_SIZE_USD, bp * 0.03)

        notional = max(notional, 1.0)

        if DRY_RUN:
            log.info(f"  [DRY RUN] {side.value} ${notional:.0f} {t} [{trade.get('source')}]")
            results.append({**trade, "status": "dry_run", "notional": notional})
            continue

        try:
            order = client.submit_order(MarketOrderRequest(
                symbol=t, notional=round(notional,2),
                side=side, time_in_force=TimeInForce.DAY,
            ))
            log.info(f"  ✓ {side.value} ${notional:.0f} {t} [{trade.get('source')}]")
            results.append({**trade, "status": "submitted",
                            "order_id": str(order.id), "notional": notional})
            bp -= notional
        except Exception as e:
            log.error(f"  ✗ {t}: {e}")
            results.append({**trade, "status": "error", "reason": str(e)})

    return results

# ══════════════════════════════════════════════════════════════════════════════
# 5. EMAIL REPORT
# ══════════════════════════════════════════════════════════════════════════════

def badge(s):
    c = {"submitted":"#22c55e","skipped":"#f59e0b","error":"#ef4444","dry_run":"#6366f1"}.get(s,"#6b7280")
    return f"<span style='background:{c};color:#fff;padding:2px 8px;border-radius:99px;font-size:11px'>{s}</span>"

def source_badge(s):
    c = {"Congress":"#3b82f6","13F":"#8b5cf6","PEAD":"#f59e0b"}.get(s,"#6b7280")
    return f"<span style='background:{c};color:#fff;padding:2px 8px;border-radius:99px;font-size:11px'>{s}</span>"


def build_portfolio_section(congress_members, fund_positions, overlap=None) -> str:
    """Sezione fissa con la composizione completa dei portafogli."""

    # Congressisti: tutti i ticker noti per ciascun membro
    congress_rows = ""
    for m in congress_members:
        tickers = m.get("tickers", [])[:12]
        if not tickers:
            continue
        pills = " ".join(
            f"<span style='background:#eff6ff;color:#3b82f6;border:1px solid #bfdbfe;"
            f"padding:1px 6px;border-radius:4px;font-size:11px'>{t}</span>"
            for t in tickers
        )
        congress_rows += (
            f"<tr><td style='font-size:12px;font-weight:500'>{m['name']}</td>"
            f"<td style='font-size:11px;color:#64748b'>{m['party']}</td>"
            f"<td style='line-height:2'>{pills}</td></tr>"
        )

    congress_section = (
        "<div class='s'><h2>🏛 Portafoglio Congressisti — Posizioni note</h2>"
        "<table><thead><tr><th>Nome</th><th>Partito</th><th>Ticker in portafoglio</th></tr></thead>"
        f"<tbody>{congress_rows or '<tr><td colspan=3 style=text-align:center;color:#94a3b8>Nessun dato</td></tr>'}</tbody>"
        "</table></div>"
    )

    # Hedge fund: top 15 posizioni per fondo
    fund_sections = ""
    for fund, positions in fund_positions.items():
        if not positions:
            continue
        rows = ""
        for i, p in enumerate(positions[:15], 1):
            val_m = p["value_usd"] / 1_000_000
            rows += (
                f"<tr><td style='color:#94a3b8;font-size:11px'>{i}</td>"
                f"<td><b>{p['ticker']}</b></td>"
                f"<td style='font-size:11px;color:#64748b'>{p['name'][:35]}</td>"
                f"<td style='color:#22c55e;font-weight:600'>${val_m:.1f}M</td></tr>"
            )
        date_label = positions[0]["date"] if positions else "N/A"
        fund_sections += (
            f"<div class='s'><h2>🏦 {fund}</h2>"
            f"<p style='font-size:11px;color:#94a3b8;margin:0 0 10px'>13F al {date_label} — Top 15 posizioni</p>"
            "<table><thead><tr><th>#</th><th>Ticker</th><th>Società</th><th>Valore</th></tr></thead>"
            f"<tbody>{rows}</tbody></table></div>"
        )

    # Overlap section
    overlap_rows = ""
    for item in (overlap or []):
        funds_str = ", ".join(item["funds"])
        stars = "⭐" * len(item["funds"])
        val_m = item["value_usd"] / 1_000_000
        overlap_rows += (
            f"<tr><td><b style='color:#0f172a'>{item['ticker']}</b></td>"
            f"<td style='font-size:11px;color:#64748b'>{item['name'][:30]}</td>"
            f"<td>{stars}</td>"
            f"<td style='font-size:11px'>{funds_str}</td>"
            f"<td style='color:#22c55e;font-weight:600'>${val_m:.0f}M</td></tr>"
        )

    if overlap:
        overlap_section = (
            "<div class='s' style='background:#fefce8;border-left:4px solid #f59e0b;padding:20px 36px'>"
            "<h2 style='color:#92400e'>🎯 Alta Conviction — Overlap Politici + Fondi</h2>"
            "<p style='font-size:11px;color:#92400e;margin:0 0 10px'>"
            "Ticker presenti sia nei portafogli dei congressisti sia nelle posizioni 13F dei fondi hedge</p>"
            "<table><thead><tr>"
            "<th>Ticker</th><th>Società</th><th>Fondi</th><th>Dettaglio</th><th>Valore 13F</th>"
            "</tr></thead>"
            f"<tbody>{overlap_rows}</tbody></table></div>"
        )
    else:
        overlap_section = ""

    return overlap_section + congress_section + fund_sections

def build_html(congress_members, fund_positions, results, date_str, all_congress_trades=None, overlap_data=None):
    mode = "<span style='background:#6366f1;color:#fff;padding:2px 10px;border-radius:99px;font-size:11px'>DRY RUN</span>" if DRY_RUN else "<span style='background:#22c55e;color:#fff;padding:2px 10px;border-radius:99px;font-size:11px'>LIVE PAPER</span>"

    # Stats
    sub = sum(1 for r in results if r["status"]=="submitted")
    skp = sum(1 for r in results if r["status"]=="skipped")
    err = sum(1 for r in results if r["status"]=="error")
    dry = sum(1 for r in results if r["status"]=="dry_run")

    # Congress members table
    congress_rows = "".join(
        f"<tr><td>{m['name']}</td><td>{m['party']}</td>"
        f"<td style='color:#3b82f6;font-weight:600'>{int(m['return_pct'])} buys/yr</td></tr>"
        for m in congress_members
    ) or "<tr><td colspan='3' style='text-align:center;color:#94a3b8'>Nessun dato</td></tr>"

    # 13F table
    fund_rows = ""
    for fund, positions in fund_positions.items():
        top5 = ", ".join(p["ticker"] for p in positions[:5])
        fund_rows += f"<tr><td style='font-size:12px'>{fund}</td><td>{len(positions)}</td><td style='font-size:11px'>{top5}</td></tr>"

    # All trades table
    trade_rows = "".join(
        f"<tr><td><b>{r['ticker']}</b></td>"
        f"<td>{source_badge(r.get('source',''))}</td>"
        f"<td style='font-size:11px'>{r['member'][:30]}</td>"
        f"<td>{badge(r['status'])}</td>"
        f"<td>${r.get('notional',0):.0f}</td></tr>"
        for r in results
    ) or "<tr><td colspan='5' style='text-align:center;color:#94a3b8;padding:16px'>Nessun trade oggi</td></tr>"

    # PEAD section
    pead_results = [r for r in results if r.get("source")=="PEAD"]
    pead_rows = "".join(
        f"<tr><td><b>{r['ticker']}</b></td>"
        f"<td>{r.get('sue','-')}</td>"
        f"<td>{r.get('transcript','-')}</td>"
        f"<td style='font-size:11px'>{r.get('summary','')[:50]}</td>"
        f"<td>{badge(r['status'])}</td></tr>"
        for r in pead_results
    ) or "<tr><td colspan='5' style='text-align:center;color:#94a3b8;padding:12px'>Nessun candidato PEAD questa settimana</td></tr>"

    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<style>
body{{font-family:Arial,sans-serif;background:#f8fafc;color:#1e293b;margin:0}}
.c{{max-width:720px;margin:32px auto;background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,.08)}}
.h{{background:linear-gradient(135deg,#0f172a,#1e3a5f);padding:28px 36px;color:#fff}}
.h h1{{margin:0 0 6px;font-size:20px;font-weight:700}}.h p{{margin:0;opacity:.6;font-size:12px}}
.s{{padding:20px 36px;border-bottom:1px solid #f1f5f9}}
.s h2{{font-size:11px;text-transform:uppercase;letter-spacing:1px;color:#64748b;margin:0 0 12px}}
.stats{{display:flex;gap:10px}}
.stat{{flex:1;background:#f8fafc;border-radius:8px;padding:10px;text-align:center}}
.stat .v{{font-size:24px;font-weight:800}}.stat .l{{font-size:10px;color:#94a3b8;text-transform:uppercase}}
table{{width:100%;border-collapse:collapse;font-size:12px}}
th{{text-align:left;padding:6px 8px;color:#94a3b8;font-size:10px;text-transform:uppercase;border-bottom:2px solid #f1f5f9}}
td{{padding:8px 8px;border-bottom:1px solid #f8fafc;vertical-align:middle}}
.f{{padding:14px 36px;text-align:center;font-size:11px;color:#94a3b8}}
</style></head><body><div class="c">

<div class="h">
  <h1>🏛 Congress + Hedge Fund Trader</h1>
  <p>{date_str} · Alpaca Paper · {mode}</p>
</div>

<div class="s"><h2>Riepilogo</h2>
<div class="stats">
  <div class="stat"><div class="v" style="color:#22c55e">{sub+dry}</div><div class="l">Ordini</div></div>
  <div class="stat"><div class="v" style="color:#f59e0b">{skp}</div><div class="l">Skippati</div></div>
  <div class="stat"><div class="v" style="color:#ef4444">{err}</div><div class="l">Errori</div></div>
  <div class="stat"><div class="v" style="color:#3b82f6">{sum(len(p) for p in fund_positions.values())}</div><div class="l">Posizioni 13F</div></div>
  <div class="stat"><div class="v" style="color:#f59e0b">{len(pead_results)}</div><div class="l">PEAD</div></div>
</div></div>

<div class="s"><h2>🏛 Top {len(congress_members)} Congressisti</h2>
<table><thead><tr><th>Nome</th><th>Partito</th><th>Attività</th></tr></thead>
<tbody>{congress_rows}</tbody></table></div>

<div class="s"><h2>🏦 Hedge Fund 13F — Top 15 posizioni per fondo</h2>
<table><thead><tr><th>Fondo</th><th>N. posizioni</th><th>Top 5 ticker</th></tr></thead>
<tbody>{fund_rows}</tbody></table></div>

<div class="s"><h2>📈 Gen AI Earnings (PEAD) — Selezione settimana</h2>
<table><thead><tr><th>Ticker</th><th>SUE</th><th>AI Score</th><th>Sintesi</th><th>Stato</th></tr></thead>
<tbody>{pead_rows}</tbody></table></div>

{build_portfolio_section(congress_members, fund_positions, overlap=overlap_data)}<div class="s"><h2>📋 Tutti i trade eseguiti oggi</h2>
<table><thead><tr><th>Ticker</th><th>Fonte</th><th>Manager</th><th>Stato</th><th>Importo</th></tr></thead>
<tbody>{trade_rows}</tbody></table></div>

<div class="f">Congress + HF Trader · Dati: Quiver Quantitative, SEC EDGAR · Paper only · Non è consulenza finanziaria</div>
</div></body></html>"""

def send_email(subject, html):
    # Prova SMTP prima
    if GMAIL_USER and GMAIL_APP_PWD:
        msg = MIMEMultipart("alternative")
        msg["Subject"], msg["From"], msg["To"] = subject, GMAIL_USER, EMAIL_TO
        msg.attach(MIMEText(html, "html"))
        for port, use_ssl in [(587, False), (465, True)]:
            try:
                if use_ssl:
                    import ssl
                    with smtplib.SMTP_SSL("smtp.gmail.com", port,
                                          context=ssl.create_default_context(), timeout=10) as s:
                        s.login(GMAIL_USER, GMAIL_APP_PWD)
                        s.sendmail(GMAIL_USER, EMAIL_TO, msg.as_string())
                else:
                    with smtplib.SMTP("smtp.gmail.com", port, timeout=10) as s:
                        s.ehlo(); s.starttls(); s.ehlo()
                        s.login(GMAIL_USER, GMAIL_APP_PWD)
                        s.sendmail(GMAIL_USER, EMAIL_TO, msg.as_string())
                log.info(f"✓ Email inviata via SMTP port {port}")
                return
            except smtplib.SMTPAuthenticationError as e:
                log.error(f"SMTP auth error: {e}")
                break
            except Exception as e:
                log.warning(f"SMTP port {port} failed: {e}")

    # Fallback Mailjet
    if MAILJET_KEY and MAILJET_SECRET:
        try:
            r = requests.post("https://api.mailjet.com/v3.1/send",
                auth=(MAILJET_KEY, MAILJET_SECRET),
                json={"Messages": [{"From": {"Email": GMAIL_USER or EMAIL_TO, "Name": "Congress Trader"},
                                    "To": [{"Email": EMAIL_TO}],
                                    "Subject": subject, "HTMLPart": html}]},
                timeout=15)
            if r.status_code == 200:
                log.info("✓ Email inviata via Mailjet")
            else:
                log.error(f"Mailjet error {r.status_code}: {r.text[:200]}")
        except Exception as e:
            log.error(f"Mailjet error: {e}")
    else:
        log.warning("Nessun metodo email configurato")

# ══════════════════════════════════════════════════════════════════════════════
# 6. MAIN JOB
# ══════════════════════════════════════════════════════════════════════════════

def run_daily_job():
    date_str = datetime.now().strftime("%A, %d %B %Y · %H:%M")
    log.info(f"═══ Congress + HF Trader v4 · {date_str} ═══")

    all_trades = []

    # 1. Congressional trades
    congress_raw  = fetch_congressional_trades(days=365)
    top_members   = rank_congress_members(congress_raw, TOP_N_MEMBERS)
    for m in top_members:
        all_trades.extend(get_recent_congress_trades(m["name"], congress_raw, days=7))

    # 2. Hedge Fund 13F
    fund_positions = fetch_all_13f()
    all_trades.extend(get_13f_trades(fund_positions))

    # 3. PEAD Gen AI Earnings
    pead_trades = run_pead_strategy()
    all_trades.extend(pead_trades)

    log.info(f"Totale trade da eseguire: {len(all_trades)}")

    # 4. Esegui su Alpaca
    results = execute_trades(all_trades)

    # 5. Email
    overlap = compute_overlap(top_members, fund_positions)
    html  = build_html(top_members, fund_positions, results, date_str,
                       all_congress_trades=congress_raw, overlap_data=overlap)
    subj  = f"Congress+HF Trader | {datetime.now().strftime('%d %b %Y')} | {sum(1 for r in results if r['status'] in ('submitted','dry_run'))} ordini"
    send_email(subj, html)
    log.info("═══ Done ═══")

# ══════════════════════════════════════════════════════════════════════════════
# 7. ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    RUN_NOW = os.getenv("RUN_NOW","false").lower() == "true"
    if RUN_NOW:
        run_daily_job()

    for day in [schedule.every().monday, schedule.every().tuesday,
                schedule.every().wednesday, schedule.every().thursday,
                schedule.every().friday]:
        day.at("09:35").do(run_daily_job)

    log.info("Scheduler attivo — 09:35 ET nei giorni feriali…")
    while True:
        schedule.run_pending()
        time.sleep(30)
