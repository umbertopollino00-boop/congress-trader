"""
Congress Trader Bot — v2
Uses CapitolTrades internal API (same endpoints the browser calls).
"""

import os
import json
import time
import logging
import smtplib
import requests
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from dotenv import load_dotenv

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

ALPACA_KEY     = os.getenv("ALPACA_KEY")
ALPACA_SECRET  = os.getenv("ALPACA_SECRET")
GMAIL_USER     = os.getenv("GMAIL_USER")
GMAIL_APP_PWD  = os.getenv("GMAIL_APP_PWD")
EMAIL_TO       = os.getenv("EMAIL_TO", GMAIL_USER)
TOP_N_MEMBERS  = int(os.getenv("TOP_N_MEMBERS", "5"))
TRADE_SIZE_USD = float(os.getenv("TRADE_SIZE_USD", "500"))

# CapitolTrades internal API — these are the XHR calls the browser makes
CT_API = "https://api.capitoltrades.com"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
    "Origin": "https://www.capitoltrades.com",
    "Referer": "https://www.capitoltrades.com/",
}

# ══════════════════════════════════════════════════════════════════════════════
# 1. CAPITOL TRADES API
# ══════════════════════════════════════════════════════════════════════════════

def fetch_top_members(n=TOP_N_MEMBERS) -> list[dict]:
    """Fetch top-n politicians sorted by 12-month return via CapitolTrades API."""
    url = f"{CT_API}/politicians"
    params = {
        "sortBy": "perf_last_12m",
        "sortOrder": "desc",
        "page": 1,
        "pageSize": n,
    }
    log.info("Fetching leaderboard from CapitolTrades API…")
    try:
        r = requests.get(url, params=params, headers=HEADERS, timeout=15)
        r.raise_for_status()
        data = r.json()
        politicians = data.get("data", data.get("politicians", data if isinstance(data, list) else []))
        members = []
        for p in politicians[:n]:
            perf = p.get("perfLast12m") or p.get("perf_last_12m") or p.get("performance", {}).get("last12m", 0) or 0
            pid  = p.get("id") or p.get("politicianId") or p.get("slug") or ""
            members.append({
                "name":       p.get("fullName") or p.get("name") or "Unknown",
                "party":      p.get("party") or "",
                "chamber":    p.get("chamber") or "",
                "return_pct": float(perf),
                "id":         str(pid),
            })
        members.sort(key=lambda x: x["return_pct"], reverse=True)
        log.info(f"Top members: {[m['name'] for m in members]}")
        return members
    except Exception as e:
        log.error(f"API fetch failed: {e}")
        return _fetch_members_fallback(n)


def _fetch_members_fallback(n: int) -> list[dict]:
    """Fallback: fetch trades endpoint and derive top traders by volume/recency."""
    log.info("Trying fallback: /trades endpoint…")
    try:
        r = requests.get(f"{CT_API}/trades", headers=HEADERS, params={"pageSize": 100}, timeout=15)
        r.raise_for_status()
        trades = r.json().get("data", [])
        counts = {}
        for t in trades:
            pol = t.get("politician") or {}
            name = pol.get("fullName") or pol.get("name") or t.get("politicianName") or "Unknown"
            pid  = pol.get("id") or t.get("politicianId") or name
            if name not in counts:
                counts[name] = {
                    "name": name, "party": pol.get("party", ""),
                    "chamber": pol.get("chamber", ""), "return_pct": 0.0,
                    "id": str(pid), "count": 0,
                }
            counts[name]["count"] += 1
        top = sorted(counts.values(), key=lambda x: x["count"], reverse=True)[:n]
        log.info(f"Fallback top members: {[m['name'] for m in top]}")
        return top
    except Exception as e:
        log.error(f"Fallback also failed: {e}")
        return []


def fetch_member_trades(member: dict, lookback_days: int = 1) -> list[dict]:
    """Fetch recent trades for a member via the API."""
    since = (datetime.now() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    pid = member["id"]
    trades = []

    # Try by politician id
    for url, params in [
        (f"{CT_API}/trades", {"politicianId": pid, "dateFrom": since, "pageSize": 50}),
        (f"{CT_API}/politicians/{pid}/trades", {"dateFrom": since, "pageSize": 50}),
    ]:
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=15)
            if r.status_code == 200:
                items = r.json().get("data", [])
                for t in items:
                    issuer = t.get("issuer") or t.get("stock") or {}
                    ticker = issuer.get("ticker") or t.get("ticker") or ""
                    if ":" in str(ticker):
                        ticker = ticker.split(":")[0]
                    ticker = str(ticker).upper().strip()
                    if not ticker or ticker in ("--", "N/A", "NONE"):
                        continue
                    tx = (t.get("txType") or t.get("type") or t.get("transactionType") or "").lower()
                    direction = "buy" if any(w in tx for w in ("purchase", "buy")) else "sell"
                    date_str  = t.get("transactionDate") or t.get("tradeDate") or t.get("date") or ""
                    trades.append({
                        "ticker":    ticker,
                        "direction": direction,
                        "date":      date_str[:10],
                        "member":    member["name"],
                    })
                if trades:
                    break
        except Exception as e:
            log.debug(f"  Trade fetch error ({url}): {e}")

    log.info(f"  {member['name']}: {len(trades)} trade(s)")
    return trades


# ══════════════════════════════════════════════════════════════════════════════
# 2. ALPACA TRADING
# ══════════════════════════════════════════════════════════════════════════════

def get_client() -> TradingClient:
    return TradingClient(ALPACA_KEY, ALPACA_SECRET, paper=True)


def execute_trades(trades: list[dict], dry_run: bool = False) -> list[dict]:
    client  = get_client()
    account = client.get_account()
    bp      = float(account.buying_power)
    log.info(f"Buying power: ${bp:,.2f}")

    results, seen = [], set()
    for trade in trades:
        t = trade["ticker"]
        if t in seen:
            continue
        seen.add(t)

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

        notional = min(TRADE_SIZE_USD, bp * 0.1)
        if dry_run:
            results.append({**trade, "status": "dry_run", "notional": notional})
            continue

        try:
            order = client.submit_order(MarketOrderRequest(
                symbol=t, notional=round(notional, 2),
                side=side, time_in_force=TimeInForce.DAY,
            ))
            log.info(f"  ✓ {side.value} ${notional:.0f} {t}")
            results.append({**trade, "status": "submitted", "order_id": str(order.id), "notional": notional})
            bp -= notional
        except Exception as e:
            log.error(f"  ✗ {t}: {e}")
            results.append({**trade, "status": "error", "reason": str(e)})

    return results


# ══════════════════════════════════════════════════════════════════════════════
# 3. EMAIL
# ══════════════════════════════════════════════════════════════════════════════

def build_html(top_members, results, date_str):
    member_rows = "".join(
        f"<tr><td>{m['name']}</td><td>{m['party']}</td><td>{m['chamber']}</td>"
        f"<td style='color:{'#22c55e' if m['return_pct']>=0 else '#ef4444'};font-weight:600'>"
        f"{'+' if m['return_pct']>=0 else ''}{m['return_pct']:.1f}%</td></tr>"
        for m in top_members
    )
    def badge(s):
        c = {"submitted":"#22c55e","skipped":"#f59e0b","error":"#ef4444","dry_run":"#6366f1"}.get(s,"#6b7280")
        return f"<span style='background:{c};color:#fff;padding:2px 8px;border-radius:99px;font-size:12px'>{s}</span>"
    trade_rows = "".join(
        f"<tr><td><b>{r['ticker']}</b></td><td>{r['direction'].upper()}</td>"
        f"<td>{r['member']}</td><td>{r.get('date','')}</td>"
        f"<td>{badge(r['status'])}</td><td>${r.get('notional',0):.0f}</td></tr>"
        for r in results
    ) or "<tr><td colspan='6' style='text-align:center;color:#6b7280'>No trades today</td></tr>"
    sub = sum(1 for r in results if r["status"]=="submitted")
    skp = sum(1 for r in results if r["status"]=="skipped")
    err = sum(1 for r in results if r["status"]=="error")
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<style>body{{font-family:Arial,sans-serif;background:#f8fafc;color:#1e293b;margin:0}}
.c{{max-width:680px;margin:32px auto;background:#fff;border-radius:12px;overflow:hidden}}
.h{{background:linear-gradient(135deg,#0f172a,#1e3a5f);padding:32px 40px;color:#fff}}
.h h1{{margin:0 0 4px;font-size:22px}}.h p{{margin:0;opacity:.6;font-size:13px}}
.s{{padding:24px 40px;border-bottom:1px solid #f1f5f9}}
.s h2{{font-size:12px;text-transform:uppercase;letter-spacing:1px;color:#64748b;margin:0 0 14px}}
.stats{{display:flex;gap:12px}}.stat{{flex:1;background:#f8fafc;border-radius:8px;padding:12px;text-align:center}}
.stat .v{{font-size:26px;font-weight:800}}.stat .l{{font-size:11px;color:#94a3b8;text-transform:uppercase}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th{{text-align:left;padding:7px 10px;color:#94a3b8;font-size:11px;text-transform:uppercase;border-bottom:2px solid #f1f5f9}}
td{{padding:9px 10px;border-bottom:1px solid #f8fafc}}
.f{{padding:16px 40px;text-align:center;font-size:12px;color:#94a3b8}}</style></head>
<body><div class="c">
<div class="h"><h1>Congress Trader — Daily Report</h1><p>{date_str}</p></div>
<div class="s"><h2>Summary</h2><div class="stats">
<div class="stat"><div class="v" style="color:#22c55e">{sub}</div><div class="l">Placed</div></div>
<div class="stat"><div class="v" style="color:#f59e0b">{skp}</div><div class="l">Skipped</div></div>
<div class="stat"><div class="v" style="color:#ef4444">{err}</div><div class="l">Errors</div></div>
<div class="stat"><div class="v">{len(top_members)}</div><div class="l">Tracked</div></div>
</div></div>
<div class="s"><h2>Top Members (12m return)</h2>
<table><thead><tr><th>Name</th><th>Party</th><th>Chamber</th><th>12m Return</th></tr></thead>
<tbody>{member_rows}</tbody></table></div>
<div class="s"><h2>Trades</h2>
<table><thead><tr><th>Ticker</th><th>Side</th><th>Member</th><th>Date</th><th>Status</th><th>Size</th></tr></thead>
<tbody>{trade_rows}</tbody></table></div>
<div class="f">Congress Trader · Paper account · Not financial advice</div>
</div></body></html>"""


def send_email(subject, html):
    if not GMAIL_USER or not GMAIL_APP_PWD:
        log.warning("Gmail credentials missing — skipping email")
        return
    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = subject, GMAIL_USER, EMAIL_TO
    msg.attach(MIMEText(html, "html"))
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(GMAIL_USER, GMAIL_APP_PWD)
            s.sendmail(GMAIL_USER, EMAIL_TO, msg.as_string())
        log.info(f"Email sent to {EMAIL_TO}")
    except Exception as e:
        log.error(f"Email error: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# 4. MAIN JOB
# ══════════════════════════════════════════════════════════════════════════════

def run_daily_job(dry_run: bool = False):
    date_str = datetime.now().strftime("%A, %B %d %Y · %H:%M")
    log.info(f"═══ Congress Trader v2 running at {date_str} ═══")

    top_members = fetch_top_members(TOP_N_MEMBERS)
    if not top_members:
        log.error("No members fetched — aborting")
        send_email("Congress Trader ⚠️ — No data", "<p>Could not fetch members from CapitolTrades.</p>")
        return

    all_trades = []
    for m in top_members:
        all_trades.extend(fetch_member_trades(m, lookback_days=1))
    log.info(f"Trades to process: {len(all_trades)}")

    results = execute_trades(all_trades, dry_run=dry_run)

    html = build_html(top_members, results, date_str)
    sub  = f"Congress Trader | {datetime.now().strftime('%d %b %Y')} | {sum(1 for r in results if r['status']=='submitted')} orders"
    send_email(sub, html)
    log.info("═══ Done ═══")


# ══════════════════════════════════════════════════════════════════════════════
# 5. SCHEDULER
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import schedule

    DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"
    RUN_NOW = os.getenv("RUN_NOW", "false").lower() == "true"

    if RUN_NOW:
        run_daily_job(dry_run=DRY_RUN)

    for day in [schedule.every().monday, schedule.every().tuesday, schedule.every().wednesday,
                schedule.every().thursday, schedule.every().friday]:
        day.at("09:35").do(run_daily_job, dry_run=DRY_RUN)

    log.info("Scheduler running — fires at 09:35 ET on weekdays…")
    while True:
        schedule.run_pending()
        time.sleep(30)
