"""
Congress Trader Bot — v3
Usa l'API pubblica di Quiver Quantitative per i congressional trades.
API gratuita, nessun blocco bot, dati affidabili.
"""

import os
import json
import time
import logging
import smtplib
import requests
import schedule
from datetime import datetime, timedelta
from collections import defaultdict
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
QUIVER_KEY     = os.getenv("QUIVER_KEY", "")          # opzionale, migliora i limiti
GMAIL_USER     = os.getenv("GMAIL_USER")
GMAIL_APP_PWD  = os.getenv("GMAIL_APP_PWD")
EMAIL_TO       = os.getenv("EMAIL_TO", GMAIL_USER)
TOP_N_MEMBERS  = int(os.getenv("TOP_N_MEMBERS", "5"))
TRADE_SIZE_USD = float(os.getenv("TRADE_SIZE_USD", "500"))
DRY_RUN        = os.getenv("DRY_RUN", "false").lower() == "true"

QUIVER_BASE = "https://api.quiverquant.com/beta"
QH = {"Accept": "application/json", "X-CSRFToken": "quiver"}
if QUIVER_KEY:
    QH["Authorization"] = f"Token {QUIVER_KEY}"

# ══════════════════════════════════════════════════════════════════════════════
# 1. DATI CONGRESSIONAL TRADES — Quiver Quantitative (gratuito)
# ══════════════════════════════════════════════════════════════════════════════

def fetch_all_trades_last_n_days(days: int = 365) -> list[dict]:
    """Scarica tutti i congressional trades degli ultimi N giorni."""
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    url   = f"{QUIVER_BASE}/live/congresstrading"
    log.info(f"Fetching congressional trades since {since}…")
    try:
        r = requests.get(url, headers=QH, timeout=20)
        r.raise_for_status()
        trades = r.json()
        # filtra per data
        result = [
            t for t in trades
            if t.get("TransactionDate", "9999") >= since
        ]
        log.info(f"  {len(result)} trades fetched (last {days} days)")
        return result
    except Exception as e:
        log.error(f"Quiver fetch error: {e}")
        return []


def rank_members_by_performance(trades: list[dict], top_n: int = TOP_N_MEMBERS) -> list[dict]:
    """
    Calcola un proxy di performance per ogni membro:
    conta quanti BUY hanno fatto sui titoli che sono poi saliti
    (proxy semplice: volume di acquisti nell'anno).
    Restituisce i top_n con più acquisti — sono i più "attivi/fiduciosi".
    """
    buys = defaultdict(lambda: {"name": "", "party": "", "count": 0, "tickers": []})
    for t in trades:
        tx = (t.get("Transaction") or "").upper()
        if "PURCHASE" not in tx and "BUY" not in tx:
            continue
        name   = t.get("Representative") or t.get("Name") or "Unknown"
        party  = t.get("Party") or ""
        ticker = (t.get("Ticker") or "").upper().strip()
        if name == "Unknown" or not ticker:
            continue
        buys[name]["name"]  = name
        buys[name]["party"] = party
        buys[name]["count"] += 1
        if ticker not in buys[name]["tickers"]:
            buys[name]["tickers"].append(ticker)

    ranked = sorted(buys.values(), key=lambda x: x["count"], reverse=True)[:top_n]
    for m in ranked:
        m["return_pct"] = float(m["count"])   # proxy: usa conteggio come score
        m["chamber"]    = ""
    log.info(f"Top {top_n} members by buy activity: {[m['name'] for m in ranked]}")
    return ranked


def get_recent_buys(member_name: str, all_trades: list[dict], days: int = 1) -> list[dict]:
    """Restituisce i BUY recenti di un membro specifico."""
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    result = []
    for t in all_trades:
        if (t.get("Representative") or t.get("Name") or "") != member_name:
            continue
        tx   = (t.get("Transaction") or "").upper()
        date = t.get("TransactionDate") or t.get("DisclosureDate") or ""
        if date < since:
            continue
        ticker = (t.get("Ticker") or "").upper().strip()
        if not ticker or ticker in ("--", "N/A"):
            continue
        direction = "buy" if "PURCHASE" in tx or "BUY" in tx else "sell"
        result.append({
            "ticker":    ticker,
            "direction": direction,
            "date":      date[:10],
            "member":    member_name,
        })
    return result


# ══════════════════════════════════════════════════════════════════════════════
# 2. ALPACA TRADING
# ══════════════════════════════════════════════════════════════════════════════

def get_client():
    return TradingClient(ALPACA_KEY, ALPACA_SECRET, paper=True)


def execute_trades(trades: list[dict]) -> list[dict]:
    if not trades:
        return []
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
            results.append({**trade, "status": "skipped", "reason": "not tradable on Alpaca"})
            continue

        if side == OrderSide.SELL:
            positions = {p.symbol: float(p.qty) for p in client.get_all_positions()}
            if t not in positions:
                results.append({**trade, "status": "skipped", "reason": "no position to sell"})
                continue

        notional = min(TRADE_SIZE_USD, bp * 0.1)

        if DRY_RUN:
            log.info(f"  [DRY RUN] {side.value} ${notional:.0f} {t}")
            results.append({**trade, "status": "dry_run", "notional": notional})
            continue

        try:
            order = client.submit_order(MarketOrderRequest(
                symbol=t, notional=round(notional, 2),
                side=side, time_in_force=TimeInForce.DAY,
            ))
            log.info(f"  ✓ {side.value} ${notional:.0f} {t} — order {order.id}")
            results.append({**trade, "status": "submitted", "order_id": str(order.id), "notional": notional})
            bp -= notional
        except Exception as e:
            log.error(f"  ✗ {t}: {e}")
            results.append({**trade, "status": "error", "reason": str(e)})

    return results


# ══════════════════════════════════════════════════════════════════════════════
# 3. EMAIL REPORT
# ══════════════════════════════════════════════════════════════════════════════

def build_html(top_members, results, date_str):
    member_rows = "".join(
        f"<tr><td>{m['name']}</td><td>{m['party']}</td>"
        f"<td style='font-weight:600;color:#3b82f6'>{int(m['return_pct'])} buys/yr</td></tr>"
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
    ) or "<tr><td colspan='6' style='text-align:center;color:#94a3b8;padding:16px'>Nessun trade oggi — i congressisti non hanno segnalato nuove operazioni</td></tr>"
    sub = sum(1 for r in results if r["status"]=="submitted")
    skp = sum(1 for r in results if r["status"]=="skipped")
    err = sum(1 for r in results if r["status"]=="error")
    mode_badge = "<span style='background:#6366f1;color:#fff;padding:2px 10px;border-radius:99px;font-size:11px'>DRY RUN</span>" if DRY_RUN else "<span style='background:#22c55e;color:#fff;padding:2px 10px;border-radius:99px;font-size:11px'>LIVE PAPER</span>"
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<style>body{{font-family:Arial,sans-serif;background:#f8fafc;color:#1e293b;margin:0}}
.c{{max-width:680px;margin:32px auto;background:#fff;border-radius:12px;overflow:hidden}}
.h{{background:#0f172a;padding:32px 40px;color:#fff}}
.h h1{{margin:0 0 6px;font-size:22px}}.h p{{margin:0;opacity:.6;font-size:13px}}
.s{{padding:24px 40px;border-bottom:1px solid #f1f5f9}}
.s h2{{font-size:12px;text-transform:uppercase;letter-spacing:1px;color:#64748b;margin:0 0 14px}}
.stats{{display:flex;gap:12px}}.stat{{flex:1;background:#f8fafc;border-radius:8px;padding:12px;text-align:center}}
.stat .v{{font-size:26px;font-weight:800}}.stat .l{{font-size:11px;color:#94a3b8;text-transform:uppercase}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th{{text-align:left;padding:7px 10px;color:#94a3b8;font-size:11px;text-transform:uppercase;border-bottom:2px solid #f1f5f9}}
td{{padding:9px 10px;border-bottom:1px solid #f8fafc}}
.f{{padding:16px 40px;text-align:center;font-size:12px;color:#94a3b8}}</style></head>
<body><div class="c">
<div class="h">
  <h1>Congress Trader — Daily Report</h1>
  <p>{date_str} &nbsp;·&nbsp; Alpaca Paper &nbsp;·&nbsp; {mode_badge}</p>
</div>
<div class="s"><h2>Summary</h2><div class="stats">
<div class="stat"><div class="v" style="color:#22c55e">{sub}</div><div class="l">Placed</div></div>
<div class="stat"><div class="v" style="color:#f59e0b">{skp}</div><div class="l">Skipped</div></div>
<div class="stat"><div class="v" style="color:#ef4444">{err}</div><div class="l">Errors</div></div>
<div class="stat"><div class="v">{len(top_members)}</div><div class="l">Members</div></div>
</div></div>
<div class="s"><h2>Top {len(top_members)} Congressisti più attivi (ultimi 12 mesi)</h2>
<table><thead><tr><th>Nome</th><th>Partito</th><th>Attività</th></tr></thead>
<tbody>{member_rows}</tbody></table></div>
<div class="s"><h2>Trade di oggi</h2>
<table><thead><tr><th>Ticker</th><th>Side</th><th>Membro</th><th>Data</th><th>Stato</th><th>Importo</th></tr></thead>
<tbody>{trade_rows}</tbody></table></div>
<div class="f">Congress Trader Bot · Dati via Quiver Quantitative · Solo paper trading · Non è consulenza finanziaria</div>
</div></body></html>"""


def send_email(subject, html):
    log.info(f"--- EMAIL DEBUG ---")
    log.info(f"GMAIL_USER set: {bool(GMAIL_USER)} ({GMAIL_USER})")
    log.info(f"GMAIL_APP_PWD set: {bool(GMAIL_APP_PWD)} (len={len(GMAIL_APP_PWD) if GMAIL_APP_PWD else 0})")
    log.info(f"EMAIL_TO: {EMAIL_TO}")
    if not GMAIL_USER or not GMAIL_APP_PWD:
        log.warning("Gmail credentials mancanti — email saltata")
        return
    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = subject, GMAIL_USER, EMAIL_TO
    msg.attach(MIMEText(html, "html"))
    try:
        log.info("Connecting to smtp.gmail.com:465…")
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            log.info("Connected — logging in…")
            s.login(GMAIL_USER, GMAIL_APP_PWD)
            log.info("Logged in — sending…")
            s.sendmail(GMAIL_USER, EMAIL_TO, msg.as_string())
        log.info(f"✓ Email inviata a {EMAIL_TO}")
    except smtplib.SMTPAuthenticationError as e:
        log.error(f"SMTP AUTH ERROR: {e} — controlla che sia una App Password Google, non la password normale")
    except smtplib.SMTPException as e:
        log.error(f"SMTP error: {e}")
    except Exception as e:
        log.error(f"Email error generico: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# 4. MAIN JOB
# ══════════════════════════════════════════════════════════════════════════════

def run_daily_job():
    date_str = datetime.now().strftime("%A, %d %B %Y · %H:%M")
    log.info(f"═══ Congress Trader v3 · {date_str} ═══")

    # 1. Scarica tutti i trade dell'anno
    all_trades = fetch_all_trades_last_n_days(days=365)
    if not all_trades:
        log.error("Nessun dato — aborting")
        send_email("Congress Trader ⚠️ — Nessun dato", "<p>Impossibile scaricare i dati da Quiver Quantitative.</p>")
        return

    # 2. Classifica i top membri
    top_members = rank_members_by_performance(all_trades, top_n=TOP_N_MEMBERS)

    # 3. Trova i loro trade di OGGI (o ieri se weekend)
    today_trades = []
    for m in top_members:
        today_trades.extend(get_recent_buys(m["name"], all_trades, days=1))
    log.info(f"Trade recenti da copiare: {len(today_trades)}")

    # 4. Esegui su Alpaca
    results = execute_trades(today_trades)

    # 5. Manda email
    html = build_html(top_members, results, date_str)
    subj = f"Congress Trader | {datetime.now().strftime('%d %b %Y')} | {sum(1 for r in results if r['status']=='submitted')} ordini"
    send_email(subj, html)
    log.info("═══ Done ═══")


# ══════════════════════════════════════════════════════════════════════════════
# 5. ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    RUN_NOW = os.getenv("RUN_NOW", "false").lower() == "true"

    if RUN_NOW:
        run_daily_job()

    # Ogni giorno feriale alle 09:35 ET (15:35 ora italiana)
    for day in [schedule.every().monday, schedule.every().tuesday, schedule.every().wednesday,
                schedule.every().thursday, schedule.every().friday]:
        day.at("09:35").do(run_daily_job)

    log.info("Scheduler attivo — si esegue alle 09:35 ET nei giorni feriali…")
    while True:
        schedule.run_pending()
        time.sleep(30)
