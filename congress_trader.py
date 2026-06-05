"""
Congress Trader Bot
Tracks top-performing Congress members on CapitolTrades and mirrors their trades
on an Alpaca paper trading account.
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
from bs4 import BeautifulSoup
from dotenv import load_dotenv

# ─── Alpaca SDK ────────────────────────────────────────────────────────────────
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, GetAssetsRequest
from alpaca.trading.enums import OrderSide, TimeInForce, AssetClass

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ─── Config ────────────────────────────────────────────────────────────────────
ALPACA_KEY    = os.getenv("ALPACA_KEY")
ALPACA_SECRET = os.getenv("ALPACA_SECRET")
ALPACA_BASE   = os.getenv("ALPACA_BASE", "https://paper-api.alpaca.markets")

GMAIL_USER    = os.getenv("GMAIL_USER")
GMAIL_APP_PWD = os.getenv("GMAIL_APP_PWD")   # Gmail App Password (not your login password)
EMAIL_TO      = os.getenv("EMAIL_TO", GMAIL_USER)

TOP_N_MEMBERS = int(os.getenv("TOP_N_MEMBERS", "5"))
TRADE_SIZE_USD = float(os.getenv("TRADE_SIZE_USD", "500"))   # $ per position

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}


# ══════════════════════════════════════════════════════════════════════════════
# 1. CAPITOL TRADES SCRAPER
# ══════════════════════════════════════════════════════════════════════════════

def fetch_top_members(n=TOP_N_MEMBERS) -> list[dict]:
    """
    Scrape CapitolTrades leaderboard for the top-n members by 12-month return.
    Returns a list of dicts: {name, party, chamber, return_pct, url}
    """
    url = "https://www.capitoltrades.com/politicians?sortBy=perf_last_12m&sortOrder=desc"
    log.info("Fetching leaderboard from CapitolTrades…")

    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        log.error(f"Failed to fetch leaderboard: {e}")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    members = []

    # CapitolTrades uses a data-driven table; rows are <tr> inside <tbody>
    rows = soup.select("table tbody tr")
    if not rows:
        # Fallback: try JSON embedded in <script id="__NEXT_DATA__">
        members = _extract_from_next_data(soup, n)
        return members

    for row in rows[:n]:
        cols = row.find_all("td")
        if len(cols) < 4:
            continue
        name_tag = cols[0].find("a")
        name = name_tag.get_text(strip=True) if name_tag else cols[0].get_text(strip=True)
        href = name_tag["href"] if name_tag and name_tag.get("href") else ""
        party = cols[1].get_text(strip=True)
        chamber = cols[2].get_text(strip=True)
        perf_raw = cols[-1].get_text(strip=True).replace("%", "").replace("+", "")
        try:
            perf = float(perf_raw)
        except ValueError:
            perf = 0.0

        members.append({
            "name": name,
            "party": party,
            "chamber": chamber,
            "return_pct": perf,
            "url": f"https://www.capitoltrades.com{href}" if href.startswith("/") else href,
        })

    members.sort(key=lambda x: x["return_pct"], reverse=True)
    log.info(f"Top {n} members fetched: {[m['name'] for m in members[:n]]}")
    return members[:n]


def _extract_from_next_data(soup: BeautifulSoup, n: int) -> list[dict]:
    """Fallback: extract politician data from Next.js __NEXT_DATA__ JSON blob."""
    script = soup.find("script", {"id": "__NEXT_DATA__"})
    if not script:
        log.warning("No __NEXT_DATA__ found on CapitolTrades — structure may have changed.")
        return []

    try:
        data = json.loads(script.string)
        politicians = (
            data.get("props", {})
                .get("pageProps", {})
                .get("politicians", [])
        )
        members = []
        for p in politicians:
            perf = p.get("perfLast12m") or p.get("perf_last_12m") or 0
            members.append({
                "name": p.get("fullName", p.get("name", "Unknown")),
                "party": p.get("party", ""),
                "chamber": p.get("chamber", ""),
                "return_pct": float(perf),
                "url": f"https://www.capitoltrades.com/politicians/{p.get('slug', '')}",
            })
        members.sort(key=lambda x: x["return_pct"], reverse=True)
        return members[:n]
    except Exception as e:
        log.error(f"__NEXT_DATA__ parse failed: {e}")
        return []


def fetch_member_trades(member: dict, lookback_days: int = 30) -> list[dict]:
    """
    Fetch recent trades for a single Congress member.
    Returns list of {ticker, direction, size, date}
    """
    url = member["url"] + "?tab=trades"
    log.info(f"  Fetching trades for {member['name']} → {url}")

    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        log.warning(f"  Could not fetch trades for {member['name']}: {e}")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    trades = []
    cutoff = datetime.now() - timedelta(days=lookback_days)

    # Try __NEXT_DATA__ first
    script = soup.find("script", {"id": "__NEXT_DATA__"})
    if script:
        try:
            data = json.loads(script.string)
            raw_trades = (
                data.get("props", {})
                    .get("pageProps", {})
                    .get("trades", [])
            )
            for t in raw_trades:
                date_str = t.get("transactionDate") or t.get("reportedDate") or ""
                try:
                    trade_date = datetime.strptime(date_str[:10], "%Y-%m-%d")
                except Exception:
                    continue
                if trade_date < cutoff:
                    continue
                ticker = t.get("ticker") or t.get("symbol") or ""
                if not ticker or ticker in ("--", "N/A"):
                    continue
                tx_type = (t.get("txType") or t.get("type") or "").lower()
                direction = "buy" if "purchase" in tx_type or "buy" in tx_type else "sell"
                trades.append({
                    "ticker": ticker.upper(),
                    "direction": direction,
                    "date": date_str[:10],
                    "member": member["name"],
                })
            return trades
        except Exception as e:
            log.debug(f"  __NEXT_DATA__ trade parse error: {e}")

    # Fallback: HTML table
    rows = soup.select("table tbody tr")
    for row in rows:
        cols = row.find_all("td")
        if len(cols) < 3:
            continue
        ticker = cols[0].get_text(strip=True).upper()
        direction_raw = cols[1].get_text(strip=True).lower()
        direction = "buy" if "purchase" in direction_raw else "sell"
        date_str = cols[-1].get_text(strip=True)
        try:
            trade_date = datetime.strptime(date_str[:10], "%Y-%m-%d")
        except Exception:
            continue
        if trade_date < cutoff:
            continue
        if ticker and ticker not in ("--", "N/A"):
            trades.append({
                "ticker": ticker,
                "direction": direction,
                "date": date_str[:10],
                "member": member["name"],
            })

    return trades


# ══════════════════════════════════════════════════════════════════════════════
# 2. ALPACA TRADING
# ══════════════════════════════════════════════════════════════════════════════

def get_trading_client() -> TradingClient:
    return TradingClient(ALPACA_KEY, ALPACA_SECRET, paper=True)


def is_tradable(client: TradingClient, ticker: str) -> bool:
    """Check if asset exists and is tradable on Alpaca."""
    try:
        asset = client.get_asset(ticker)
        return asset.tradable and asset.status == "active"
    except Exception:
        return False


def get_current_positions(client: TradingClient) -> dict[str, float]:
    """Return {ticker: qty} for all current open positions."""
    positions = client.get_all_positions()
    return {p.symbol: float(p.qty) for p in positions}


def execute_trades(trades: list[dict], dry_run: bool = False) -> list[dict]:
    """
    Execute a list of trades on Alpaca paper account.
    Returns list of results with status for each trade.
    """
    client = get_trading_client()
    account = client.get_account()
    buying_power = float(account.buying_power)
    log.info(f"Account buying power: ${buying_power:,.2f}")

    results = []
    seen = set()  # deduplicate tickers

    for trade in trades:
        ticker = trade["ticker"]
        if ticker in seen:
            continue
        seen.add(ticker)

        if not is_tradable(client, ticker):
            log.warning(f"  {ticker} not tradable on Alpaca — skipping")
            results.append({**trade, "status": "skipped", "reason": "not tradable"})
            continue

        side = OrderSide.BUY if trade["direction"] == "buy" else OrderSide.SELL

        # For sells, check we actually hold the position
        if side == OrderSide.SELL:
            positions = get_current_positions(client)
            if ticker not in positions:
                log.info(f"  {ticker} SELL skipped — no position held")
                results.append({**trade, "status": "skipped", "reason": "no position to sell"})
                continue

        # Calculate notional size
        notional = min(TRADE_SIZE_USD, buying_power * 0.1)  # never more than 10% of BP

        if dry_run:
            log.info(f"  [DRY RUN] Would {side.value} ${notional:.0f} of {ticker}")
            results.append({**trade, "status": "dry_run", "notional": notional})
            continue

        try:
            order = client.submit_order(
                MarketOrderRequest(
                    symbol=ticker,
                    notional=round(notional, 2),
                    side=side,
                    time_in_force=TimeInForce.DAY,
                )
            )
            log.info(f"  ✓ Order submitted: {side.value} ${notional:.0f} of {ticker} | id={order.id}")
            results.append({
                **trade,
                "status": "submitted",
                "order_id": str(order.id),
                "notional": notional,
            })
            buying_power -= notional  # rough running balance
        except Exception as e:
            log.error(f"  ✗ Order failed for {ticker}: {e}")
            results.append({**trade, "status": "error", "reason": str(e)})

    return results


# ══════════════════════════════════════════════════════════════════════════════
# 3. EMAIL REPORT
# ══════════════════════════════════════════════════════════════════════════════

def build_html_report(top_members: list[dict], trade_results: list[dict], date_str: str) -> str:
    member_rows = "".join(
        f"""<tr>
          <td>{m['name']}</td>
          <td>{m['party']}</td>
          <td>{m['chamber']}</td>
          <td style="color:{'#22c55e' if m['return_pct']>=0 else '#ef4444'};font-weight:600">
            {'+' if m['return_pct']>=0 else ''}{m['return_pct']:.1f}%
          </td>
        </tr>"""
        for m in top_members
    )

    def status_badge(s):
        colors = {"submitted": "#22c55e", "skipped": "#f59e0b",
                  "error": "#ef4444", "dry_run": "#6366f1"}
        return f"<span style='background:{colors.get(s,'#6b7280')};color:#fff;padding:2px 8px;border-radius:99px;font-size:12px'>{s}</span>"

    trade_rows = "".join(
        f"""<tr>
          <td><strong>{r['ticker']}</strong></td>
          <td>{r['direction'].upper()}</td>
          <td>{r['member']}</td>
          <td>{r.get('date','')}</td>
          <td>{status_badge(r['status'])}</td>
          <td>${r.get('notional',0):.0f}</td>
        </tr>"""
        for r in trade_results
    ) or "<tr><td colspan='6' style='text-align:center;color:#6b7280'>No trades today</td></tr>"

    submitted = sum(1 for r in trade_results if r["status"] == "submitted")
    skipped   = sum(1 for r in trade_results if r["status"] == "skipped")
    errors    = sum(1 for r in trade_results if r["status"] == "error")

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
  body {{ font-family: 'Helvetica Neue', Arial, sans-serif; background:#f8fafc; color:#1e293b; margin:0; padding:0; }}
  .container {{ max-width:680px; margin:32px auto; background:#fff; border-radius:12px;
                box-shadow:0 4px 24px rgba(0,0,0,.08); overflow:hidden; }}
  .header {{ background:linear-gradient(135deg,#0f172a 0%,#1e3a5f 100%);
             padding:32px 40px; color:#fff; }}
  .header h1 {{ margin:0 0 4px; font-size:22px; font-weight:700; letter-spacing:-.5px; }}
  .header p  {{ margin:0; opacity:.6; font-size:13px; }}
  .section   {{ padding:28px 40px; border-bottom:1px solid #f1f5f9; }}
  .section h2 {{ font-size:13px; font-weight:700; text-transform:uppercase;
                 letter-spacing:1px; color:#64748b; margin:0 0 16px; }}
  .stats {{ display:flex; gap:16px; margin-bottom:0; }}
  .stat  {{ flex:1; background:#f8fafc; border-radius:8px; padding:14px 16px; text-align:center; }}
  .stat .val {{ font-size:28px; font-weight:800; }}
  .stat .lbl {{ font-size:11px; color:#94a3b8; text-transform:uppercase; letter-spacing:.5px; }}
  table {{ width:100%; border-collapse:collapse; font-size:13px; }}
  th {{ text-align:left; padding:8px 10px; color:#94a3b8; font-size:11px;
        text-transform:uppercase; letter-spacing:.5px; border-bottom:2px solid #f1f5f9; }}
  td {{ padding:10px 10px; border-bottom:1px solid #f8fafc; vertical-align:middle; }}
  tr:last-child td {{ border-bottom:none; }}
  .footer {{ padding:20px 40px; text-align:center; font-size:12px; color:#94a3b8; }}
</style></head><body>
<div class="container">
  <div class="header">
    <h1>📊 Congress Trader — Daily Report</h1>
    <p>{date_str} · Alpaca Paper Account</p>
  </div>

  <div class="section">
    <h2>Summary</h2>
    <div class="stats">
      <div class="stat"><div class="val" style="color:#22c55e">{submitted}</div>
        <div class="lbl">Orders placed</div></div>
      <div class="stat"><div class="val" style="color:#f59e0b">{skipped}</div>
        <div class="lbl">Skipped</div></div>
      <div class="stat"><div class="val" style="color:#ef4444">{errors}</div>
        <div class="lbl">Errors</div></div>
      <div class="stat"><div class="val">{len(top_members)}</div>
        <div class="lbl">Members tracked</div></div>
    </div>
  </div>

  <div class="section">
    <h2>Top Congress Members (12-month return)</h2>
    <table>
      <thead><tr><th>Name</th><th>Party</th><th>Chamber</th><th>12m Return</th></tr></thead>
      <tbody>{member_rows}</tbody>
    </table>
  </div>

  <div class="section">
    <h2>Trades Executed Today</h2>
    <table>
      <thead><tr><th>Ticker</th><th>Side</th><th>Member</th><th>Date</th><th>Status</th><th>Size</th></tr></thead>
      <tbody>{trade_rows}</tbody>
    </table>
  </div>

  <div class="footer">
    Congress Trader Bot · Paper account only · Not financial advice
  </div>
</div></body></html>"""


def send_email(subject: str, html_body: str):
    if not GMAIL_USER or not GMAIL_APP_PWD:
        log.warning("Gmail credentials not set — skipping email")
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = GMAIL_USER
    msg["To"]      = EMAIL_TO
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_USER, GMAIL_APP_PWD)
            server.sendmail(GMAIL_USER, EMAIL_TO, msg.as_string())
        log.info(f"Email sent to {EMAIL_TO}")
    except Exception as e:
        log.error(f"Email failed: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# 4. MAIN DAILY JOB
# ══════════════════════════════════════════════════════════════════════════════

def run_daily_job(dry_run: bool = False):
    now = datetime.now()
    date_str = now.strftime("%A, %B %d %Y · %H:%M")
    log.info(f"═══ Congress Trader running at {date_str} ═══")

    # 1. Get leaderboard
    top_members = fetch_top_members(TOP_N_MEMBERS)
    if not top_members:
        log.error("No members fetched — aborting run")
        return

    # 2. Collect their recent trades
    all_trades = []
    for m in top_members:
        trades = fetch_member_trades(m, lookback_days=1)  # last 24 h on daily run
        all_trades.extend(trades)
    log.info(f"Total trades to process: {len(all_trades)}")

    # 3. Execute on Alpaca
    results = execute_trades(all_trades, dry_run=dry_run)

    # 4. Email report
    html = build_html_report(top_members, results, date_str)
    subject = f"📊 Congress Trader | {now.strftime('%d %b %Y')} | {sum(1 for r in results if r['status']=='submitted')} orders"
    send_email(subject, html)

    log.info("═══ Daily job complete ═══")
    return {"top_members": top_members, "results": results}


# ══════════════════════════════════════════════════════════════════════════════
# 5. SCHEDULER ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import schedule

    DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

    # Run once immediately at startup (useful for testing)
    if os.getenv("RUN_NOW", "false").lower() == "true":
        run_daily_job(dry_run=DRY_RUN)

    # Schedule every weekday at 09:35 ET (market opens 09:30)
    schedule.every().monday.at("09:35").do(run_daily_job, dry_run=DRY_RUN)
    schedule.every().tuesday.at("09:35").do(run_daily_job, dry_run=DRY_RUN)
    schedule.every().wednesday.at("09:35").do(run_daily_job, dry_run=DRY_RUN)
    schedule.every().thursday.at("09:35").do(run_daily_job, dry_run=DRY_RUN)
    schedule.every().friday.at("09:35").do(run_daily_job, dry_run=DRY_RUN)

    log.info("Scheduler started — waiting for market open (09:35 ET on weekdays)…")
    while True:
        schedule.run_pending()
        time.sleep(30)
