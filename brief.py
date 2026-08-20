#!/usr/bin/env python3
"""Daily pre-market Telegram briefing.

Reads TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID from .env (never hardcoded).
Every section degrades independently: a dead source yields a warning line,
never a dead brief. Exits non-zero only if nothing at all could be built.
"""
from __future__ import annotations

import datetime as dt
import html
import json
import logging
import os
import re
import sys
import zoneinfo
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
from dotenv import load_dotenv

ET = zoneinfo.ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parent
UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
TIMEOUT = 12

# --- thresholds (deterministic rule engine) -------------------------------
MEGACAP_USD = 200e9      # "top ~50 by market cap" floor for the catalyst rule
EARN_MCAP_USD = 10e9     # earnings calendar floor
MOVER_MCAP_USD = 2e9     # mover floors: kill thin-float noise
MOVER_MIN_PRICE = 5.0
MOVER_MIN_ADV = 1_000_000
MOVER_MIN_DOLLAR_VOL = 50e6  # price x 20d ADV; the filter that actually bites
FUT_MOVE_PCT = 0.75
VIX_3D_PCT = 10.0
VIX_ABS = 20.0
TEN_Y_BPS = 8.0

# Matched as whole words: a bare "gdp" substring also hits "GDPNow", which is
# a nowcast, not a scheduled tier-1 release.
TIER1 = (r"\bcpi\b", r"\bppi\b", r"\bnon-farm\b", r"\bnonfarm\b",
         r"\bpayrolls?\b", r"\bfomc\b", r"\bpce\b", r"\bretail sales\b",
         r"\bgdp\b")

# Windows consoles default to cp1252, which cannot encode the emoji in the
# brief; without this, printing the message raises UnicodeEncodeError.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

# Logs go to stderr always -- that is what Cloud Logging captures, and it
# keeps stdout clean so the brief text stays pipeable. The file handler is
# what makes local runs greppable; on a read-only container filesystem it
# simply is not attached.
_handlers: list[logging.Handler] = [logging.StreamHandler()]
try:
    _handlers.append(logging.FileHandler(ROOT / "run.log", encoding="utf-8"))
except OSError:
    pass
logging.basicConfig(
    level=logging.INFO, handlers=_handlers,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("brief")


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(UA)
    # default pool is 10; the sector/mover fan-out exceeds it and would
    # otherwise spam run.log with "connection pool is full" warnings
    ad = requests.adapters.HTTPAdapter(pool_connections=20, pool_maxsize=20)
    s.mount("https://", ad)
    return s


def esc(x) -> str:
    return html.escape(str(x), quote=False)


def pct(v: float) -> str:
    return f"{v:+.1f}%" if abs(v) < 100 else f"{v:+.0f}%"


def hhmm(t: dt.datetime) -> str:
    """12-hour clock without a leading zero. %-I is glibc-only and raises
    on Windows, so the hour is built by hand."""
    return f"{(t.hour - 1) % 12 + 1}:{t.minute:02d} {'AM' if t.hour < 12 else 'PM'}"


def datestamp(t: dt.datetime) -> str:
    return f"{t:%a %b} {t.day}"


# --------------------------------------------------------------------------
# fetchers -- each returns a value or raises; callers catch per section
# --------------------------------------------------------------------------
def _chart(s: requests.Session, sym: str, rng="1d", iv="1m", prepost=False) -> dict:
    u = (f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
         f"?range={rng}&interval={iv}&includePrePost={str(prepost).lower()}")
    r = s.get(u, timeout=TIMEOUT)
    r.raise_for_status()
    res = r.json()["chart"]["result"]
    if not res:
        raise ValueError(f"no chart result for {sym}")
    return res[0]


def fetch_futures() -> dict:
    """Index futures + commodity/FX gauges. Baseline is chartPreviousClose
    (the prior settle); fast_info.previousClose is NOT the settle."""
    s = _session()
    want = {"ES=F": "ES", "NQ=F": "NQ", "YM=F": "YM", "RTY=F": "RTY",
            "^VIX": "VIX", "DX-Y.NYB": "DXY", "CL=F": "WTI", "GC=F": "Gold"}
    out: dict[str, dict] = {}

    def one(item):
        sym, label = item
        m = _chart(s, sym)["meta"]
        last, prev = m.get("regularMarketPrice"), m.get("chartPreviousClose")
        if last is None or not prev:
            raise ValueError(f"{sym} missing price")
        return label, {"last": last, "prev": prev, "chg": (last / prev - 1) * 100}

    with ThreadPoolExecutor(max_workers=8) as ex:
        for label, d in ex.map(one, want.items()):
            out[label] = d
    return out


def fetch_vix_3d() -> float:
    """VIX % change over the trailing 3 sessions."""
    s = _session()
    r = _chart(s, "^VIX", rng="10d", iv="1d")
    closes = [c for c in r["indicators"]["quote"][0]["close"] if c]
    if len(closes) < 4:
        raise ValueError("insufficient VIX history")
    return (closes[-1] / closes[-4] - 1) * 100


def fetch_yields() -> dict:
    """CNBC real-time yields. Yahoo's ^TNX freezes at 15:00 ET and would
    serve yesterday's value pre-market, so it is only the fallback."""
    s = _session()
    u = ("https://quote.cnbc.com/quote-html-webservice/restQuote/symbolType/symbol"
         "?symbols=US10Y&requestMethod=itv&noform=1&partnerId=2&fund=1"
         "&exthrs=1&output=json&events=1")
    try:
        r = s.get(u, timeout=TIMEOUT)
        r.raise_for_status()
        q = r.json()["FormattedQuoteResult"]["FormattedQuote"][0]
        last = float(str(q["last"]).rstrip("%"))
        prev = float(str(q["previous_day_closing"]).rstrip("%"))
        return {"last": last, "bps": (last - prev) * 100, "src": "cnbc"}
    except Exception as e:                                   # noqa: BLE001
        log.warning("yields: cnbc failed (%s), falling back to stale ^TNX", e)
        m = _chart(s, "^TNX")["meta"]
        last, prev = m["regularMarketPrice"], m["chartPreviousClose"]
        return {"last": last, "bps": (last - prev) * 100, "src": "tnx-stale"}


def fetch_sectors() -> list[tuple[str, float]]:
    """11 SPDR sectors, prior completed session. Uses explicit daily bars:
    chartPreviousClose on a 2d range spans two sessions, not one."""
    s = _session()
    spdr = {"XLK": "Tech", "XLF": "Financials", "XLV": "Health",
            "XLY": "Discretionary", "XLP": "Staples", "XLE": "Energy",
            "XLI": "Industrials", "XLB": "Materials", "XLRE": "REITs",
            "XLU": "Utilities", "XLC": "Comm"}

    def one(item):
        sym, name = item
        r = _chart(s, sym, rng="5d", iv="1d")
        closes = [c for c in r["indicators"]["quote"][0]["close"] if c]
        if len(closes) < 2:
            raise ValueError(f"{sym} thin history")
        return name, (closes[-1] / closes[-2] - 1) * 100

    with ThreadPoolExecutor(max_workers=11) as ex:
        rows = list(ex.map(one, spdr.items()))
    return sorted(rows, key=lambda x: -x[1])


def _nasdaq_earnings(day: dt.date) -> list[dict]:
    s = _session()
    r = s.get(f"https://api.nasdaq.com/api/calendar/earnings?date={day.isoformat()}",
              timeout=TIMEOUT)
    r.raise_for_status()
    return ((r.json().get("data") or {}).get("rows") or [])


def _mcap(row: dict) -> float:
    raw = str(row.get("marketCap") or "").replace("$", "").replace(",", "").strip()
    try:
        return float(raw)
    except ValueError:
        return 0.0


def fetch_earnings(today: dt.date) -> dict:
    rows = _nasdaq_earnings(today)
    pre: list[dict] = []
    post: list[dict] = []
    for r in rows:
        if _mcap(r) < EARN_MCAP_USD:
            continue
        t = (r.get("time") or "").lower()
        if "pre-market" in t or "before" in t:
            tgt = pre
        elif "after" in t:
            tgt = post
        else:
            continue
        tgt.append({"sym": (r.get("symbol") or "?").strip(),
                    "eps": (r.get("epsForecast") or "").strip(),
                    "mcap": _mcap(r)})
    pre.sort(key=lambda x: -x["mcap"])
    post.sort(key=lambda x: -x["mcap"])
    return {"pre": pre, "post": post}


FF_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
FF_CACHE = ROOT / "ff_cache.json"

# Nasdaq publishes each sub-index of a release as its own row (five separate
# MBA mortgage lines, four EIA crude lines). Members of a family sharing a
# release time collapse to one line so they cannot crowd out the 8:30 data.
ECON_FAMILIES = (
    (r"\b(mba|mortgage)\b", "MBA Mortgage Applications"),
    (r"\b(crude|eia|refinery|distillate|gasoline|cushing|heating oil|propane"
     r"|stockpiles?)\b", "Crude Oil Inventories"),
    (r"\bpending home sales\b", "Pending Home Sales"),
    (r"\bnatural gas\b", "Natural Gas Storage"),
)


def _is_tier1(name: str) -> bool:
    return any(re.search(k, name, re.I) for k in TIER1)


def _condense(events: list[dict]) -> list[dict]:
    """Collapse duplicate and same-family rows. A tier-1 release is never
    collapsed. The surviving row keeps its own name and figures -- renaming a
    sub-index to the family label would attach the wrong consensus to it."""
    best: dict[str, dict] = {}
    for e in events:
        k = " ".join(e["name"].split()).lower()
        cur = best.get(k)
        if cur is None or (not cur["est"] and e["est"]):
            best[k] = e
    rows = sorted(best.values(), key=lambda e: (e["t"], e["name"]))

    groups: dict[tuple, list[dict]] = {}
    out: list[dict] = []
    for e in rows:
        canon = None
        if not _is_tier1(e["name"]):
            canon = next((c for pat, c in ECON_FAMILIES
                          if re.search(pat, e["name"], re.I)), None)
        if canon is None:
            out.append(e)
        else:
            groups.setdefault((e["t"], canon), []).append(e)

    for (_, canon), members in groups.items():
        exact = [m for m in members if m["name"].strip().lower() == canon.lower()]
        withest = [m for m in members if m["est"]]
        pick = dict((exact or withest or members)[0])
        pick["folded"] = len(members) - 1
        out.append(pick)

    # The two feeds name the same event differently ("President Trump Speaks"
    # vs "U.S. President Trump Speaks"), which exact-name dedupe misses. At a
    # shared release time, a name containing another is the same event; keep
    # the shorter, but never drop a tier-1 row.
    out.sort(key=lambda e: (e["t"], len(e["name"])))
    kept: list[dict] = []
    for e in out:
        n = " ".join(e["name"].split()).lower()
        dup = any(e["t"] == k["t"] and not _is_tier1(e["name"])
                  and " ".join(k["name"].split()).lower() in n
                  for k in kept)
        if not dup:
            kept.append(e)
    return sorted(kept, key=lambda e: e["t"])


def _econ_rank(e: dict) -> int:
    """Tier-1 first, then anything carrying a consensus, then the rest --
    so truncation drops filler instead of the 8:30 print."""
    if _is_tier1(e["name"]):
        return 0
    return 1 if e["est"] else 2


def _ff_week(s: requests.Session) -> list[dict]:
    """ForexFactory weekly feed, cached to disk.

    It is the only free source carrying FOMC decisions/minutes and Fed
    speakers, and it rate-limits aggressively (HTTP 429 with an HTML body).
    A 429 on an FOMC morning would otherwise silently drop the single most
    important catalyst of the month, so the last good copy is reused."""
    try:
        r = s.get(FF_URL, timeout=TIMEOUT)
        r.raise_for_status()
        if "json" not in (r.headers.get("content-type") or ""):
            raise ValueError(f"non-JSON body (HTTP {r.status_code})")
        d = r.json()
        try:
            FF_CACHE.write_text(json.dumps(d), encoding="utf-8")
        except OSError as e:
            # a read-only container filesystem must not fail the fetch that
            # already succeeded -- the cache is an optimisation, not the data
            log.warning("econ: could not write ff cache (%s)", e)
        return d
    except Exception as e:                                   # noqa: BLE001
        if FF_CACHE.exists():
            age = dt.datetime.now().timestamp() - FF_CACHE.stat().st_mtime
            if age < 7 * 86400:
                log.warning("econ: forexfactory live fetch failed (%s); "
                            "using cache %.1fh old", e, age / 3600)
                return json.loads(FF_CACHE.read_text(encoding="utf-8"))
        raise


def fetch_econ(today: dt.date) -> list[dict]:
    """ForexFactory (correct tz + Fed events) merged with Nasdaq (consensus
    figures). Neither alone is complete: Nasdaq missed FOMC Minutes, and
    ForexFactory missed the 8:30 data dump."""
    s = _session()
    events: list[dict] = []
    try:
        d = _ff_week(s)
        for x in d:
            if x.get("country") != "USD":
                continue
            t = dt.datetime.fromisoformat(x["date"]).astimezone(ET)
            if t.date() != today:
                continue
            events.append({"t": t, "name": (x.get("title") or "").strip(),
                           "est": x.get("forecast") or "",
                           "prev": x.get("previous") or ""})
    except Exception as e:                                   # noqa: BLE001
        log.warning("econ: forexfactory failed: %s", e)
    try:
        r = s.get("https://api.nasdaq.com/api/calendar/economicevents"
                  f"?date={today.isoformat()}", timeout=TIMEOUT)
        for x in ((r.json().get("data") or {}).get("rows") or []):
            if x.get("country") != "United States":
                continue
            raw = (x.get("gmt") or "").strip()      # field is mislabeled; it is ET
            try:
                h, mi = (int(v) for v in raw.split(":"))
                t = dt.datetime.combine(today, dt.time(h, mi), ET)
            except (ValueError, TypeError):
                continue
            name = (x.get("eventName") or "").strip()
            if any(e["name"].lower() == name.lower() for e in events):
                continue
            events.append({"t": t, "name": name,
                           "est": (x.get("consensus") or "").strip(),
                           "prev": (x.get("previous") or "").strip()})
    except Exception as e:                                   # noqa: BLE001
        log.warning("econ: nasdaq failed: %s", e)
    if not events:
        raise ValueError("no econ events from any source")
    return _condense(events)


def fetch_movers(earn: dict, today: dt.date) -> dict:
    """Pre-market movers. Universe = today's reporters + yesterday's
    after-close reporters + Nasdaq most-actives, i.e. the names that
    actually gap. Yahoo reports zero volume on pre-market bars, so noise is
    filtered on market cap / price / 20d average volume instead."""
    s = _session()
    uni: set[str] = set()
    sizes: dict[str, int] = {}

    def _add(label: str, syms) -> None:
        before = len(uni)
        uni.update(x for x in syms if x)
        sizes[label] = len(uni) - before

    _add("earn_today", [r["sym"] for r in earn.get("pre", []) + earn.get("post", [])])
    try:
        # Nasdaq backfills `time` to "time-not-supplied" once a date is in the
        # past, so filtering yesterday's rows on "after" matches nothing and
        # drops the after-close reporters that gap hardest this morning. Take
        # every prior-day name above the cap floor; the price/liquidity
        # filters and the pre-market ranking sort out the ones that moved.
        _add("earn_prior",
             [(r.get("symbol") or "").strip()
              for r in _nasdaq_earnings(today - dt.timedelta(days=1))
              if _mcap(r) >= MOVER_MCAP_USD])
    except Exception as e:                                   # noqa: BLE001
        sizes["earn_prior"] = -1
        log.warning("movers: prior-day earnings failed: %s", e)
    try:
        mm = s.get("https://api.nasdaq.com/api/marketmovers", timeout=TIMEOUT).json()
        _add("most_actives",
             [(row.get("symbol") or "").strip()
              for grp in ((mm.get("data") or {}).get("STOCKS") or {}).values()
              for row in ((grp.get("table") or {}).get("rows") or [])])
    except Exception as e:                                   # noqa: BLE001
        sizes["most_actives"] = -1
        log.warning("movers: marketmovers failed: %s", e)
    uni = {u for u in uni if u and u.isalpha()}
    # -1 marks a source that failed outright, distinguishing it from one that
    # returned nothing; both produce a thin universe but need different fixes.
    src_mix = " ".join(f"{k}={v}" for k, v in sizes.items())
    if not uni:
        log.error("movers: empty universe (%s)", src_mix)
        raise ValueError("empty mover universe")

    def one(sym: str):
        """Returns (row_or_None, reason). The reason is tallied into run.log
        so a thin or empty movers section can be diagnosed after the fact --
        the pre-market path cannot be exercised outside the 4:00-9:30 window,
        so the evidence has to be collected while it runs."""
        try:
            r = _chart(s, sym, rng="1d", iv="1m", prepost=True)
            meta = r["meta"]
            ts = r.get("timestamp") or []
            q = r["indicators"]["quote"][0]
            prev = meta.get("chartPreviousClose")
            period = (meta.get("currentTradingPeriod") or {}).get("regular") or {}
            reg_start = period.get("start")
            if not prev or not reg_start:
                return None, "no_meta"
            pre = [c for t, c in zip(ts, q["close"]) if c and t < reg_start]
            if not pre:
                return None, "no_pre_bars"
            if pre[-1] < MOVER_MIN_PRICE:
                return None, "under_price_floor"
            d = _chart(s, sym, rng="1mo", iv="1d")
            dq = d["indicators"]["quote"][0]
            vols = [v for v in dq["volume"] if v]
            if not vols or sum(vols) / len(vols) < MOVER_MIN_ADV:
                return None, "under_adv_floor"
            # Nasdaq most-actives carry no market cap, so liquidity is the
            # proxy: dollar volume excludes low-priced high-share-count pumps
            # that a share-count floor alone lets through.
            dv = [c * v for c, v in zip(dq["close"], dq["volume"]) if c and v]
            if not dv or sorted(dv)[len(dv) // 2] < MOVER_MIN_DOLLAR_VOL:
                return None, "under_dollar_vol"
            return {"sym": sym, "chg": (pre[-1] / prev - 1) * 100}, "ok"
        except Exception as e:                               # noqa: BLE001
            return None, f"error:{type(e).__name__}"

    with ThreadPoolExecutor(max_workers=16) as ex:
        results = list(ex.map(one, sorted(uni)))
    rows = [r for r, _ in results if r]
    tally = Counter(reason for _, reason in results)
    log.info("movers: universe=%d (%s) | %s", len(uni), src_mix,
             " ".join(f"{k}={v}" for k, v in sorted(tally.items())))
    if not rows:
        raise ValueError("no pre-market prints in universe")
    log.info("movers: kept %d, range %+.1f%% to %+.1f%%",
             len(rows), max(r["chg"] for r in rows), min(r["chg"] for r in rows))
    rows.sort(key=lambda x: -x["chg"])
    return {"up": [r for r in rows if r["chg"] > 0][:4],
            "down": [r for r in rows if r["chg"] < 0][-4:][::-1]}


# --------------------------------------------------------------------------
# rule engine -- flags fire only from fetched data, never invented
# --------------------------------------------------------------------------
def build_catalysts(data: dict) -> list[str]:
    out: list[str] = []
    for e in data.get("econ") or []:
        if _is_tier1(e["name"]):
            est = f" (est. {e['est']})" if e["est"] else ""
            out.append(f"{hhmm(e['t'])} — {e['name']}{est}")
    earnings = data.get("earnings") or {}
    for r in earnings.get("pre", []):
        if r["mcap"] >= MEGACAP_USD:
            out.append(f"{r['sym']} reports before open")
    for r in earnings.get("post", []):
        if r["mcap"] >= MEGACAP_USD:
            out.append(f"{r['sym']} reports after close")
    fut = data.get("futures") or {}
    if "VIX" in fut and fut["VIX"]["last"] > VIX_ABS:
        out.append(f"VIX at {fut['VIX']['last']:.1f} — above {VIX_ABS:.0f}")
    v3 = data.get("vix3d")
    if v3 is not None and abs(v3) > VIX_3D_PCT:
        out.append(f"VIX {pct(v3)} over 3 sessions")
    for k in ("ES", "NQ", "YM", "RTY"):
        if k in fut and abs(fut[k]["chg"]) > FUT_MOVE_PCT:
            out.append(f"{k} {pct(fut[k]['chg'])} pre-market")
    y = data.get("yields")
    if y and abs(y["bps"]) > TEN_Y_BPS:
        out.append(f"10Y {y['bps']:+.0f}bps overnight to {y['last']:.2f}%")
    return out


# --------------------------------------------------------------------------
# render
# --------------------------------------------------------------------------
def render(data: dict, fails: dict) -> str:
    now = dt.datetime.now(ET)
    L = [f"<b>📊 Pre-Market Brief — {datestamp(now)}</b>", ""]

    L.append("<b>FUTURES</b>")
    if "futures" in fails:
        L.append(f"⚠️ futures unavailable ({esc(fails['futures'])})")
    else:
        f = data["futures"]
        L.append(" · ".join(f"{k} {pct(f[k]['chg'])}"
                            for k in ("ES", "NQ", "YM", "RTY") if k in f))
        bits = []
        if "VIX" in f:
            bits.append(f"VIX {f['VIX']['last']:.1f} ({pct(f['VIX']['chg'])})")
        if data.get("yields"):
            bits.append(f"10Y {data['yields']['last']:.2f}%")
        if "DXY" in f:
            bits.append(f"DXY {f['DXY']['last']:.1f}")
        if bits:
            L.append(" · ".join(bits))
        cm = []
        if "WTI" in f:
            cm.append(f"Crude ${f['WTI']['last']:.2f} {pct(f['WTI']['chg'])}")
        if "Gold" in f:
            cm.append(f"Gold ${f['Gold']['last']:,.0f} {pct(f['Gold']['chg'])}")
        if cm:
            L.append(" · ".join(cm))
    if "yields" in fails:
        L.append(f"⚠️ 10Y yield unavailable ({esc(fails['yields'])})")

    L += ["", "<b>⚡ WHAT COULD MOVE TODAY</b>"]
    cats = build_catalysts(data)
    L += [f"• {esc(c)}" for c in cats] or ["• Quiet setup — no major scheduled catalysts."]

    L += ["", "<b>📅 ECONOMIC CALENDAR</b>"]
    if "econ" in fails:
        L.append(f"⚠️ economic calendar unavailable ({esc(fails['econ'])})")
    else:
        rows = sorted(data["econ"], key=lambda e: (_econ_rank(e), e["t"]))[:8]
        for e in sorted(rows, key=lambda e: e["t"]):
            extra = []
            if e["est"]:
                extra.append(f"est {e['est']}")
            if e["prev"]:
                extra.append(f"prior {e['prev']}")
            tail = f"  <i>{esc(' · '.join(extra))}</i>" if extra else ""
            more = f" (+{e['folded']})" if e.get("folded") else ""
            L.append(f"{hhmm(e['t'])}  {esc(e['name'][:38])}{more}{tail}")

    L += ["", "<b>📈 EARNINGS</b>"]
    if "earnings" in fails:
        L.append(f"⚠️ earnings unavailable ({esc(fails['earnings'])})")
    else:
        for lbl, key in (("Before open", "pre"), ("After close", "post")):
            chunks = []
            for r in data["earnings"][key][:5]:
                tag = f" (est {esc(r['eps'])})" if r["eps"] else ""
                chunks.append(f"{esc(r['sym'])}{tag}")
            L.append(f"{lbl}: {' · '.join(chunks) if chunks else '—'}")

    L += ["", "<b>🔥 PRE-MARKET MOVERS</b>"]
    if "movers" in fails:
        L.append(f"⚠️ pre-market movers unavailable ({esc(fails['movers'])})")
    else:
        m = data["movers"]
        up = " · ".join(f"{esc(r['sym'])} {pct(r['chg'])}" for r in m["up"]) or "—"
        dn = " · ".join(f"{esc(r['sym'])} {pct(r['chg'])}" for r in m["down"]) or "—"
        L += [f"Up:   {up}", f"Down: {dn}"]

    L += ["", "<b>🔄 SECTORS (prior session)</b>"]
    if "sectors" in fails:
        L.append(f"⚠️ sectors unavailable ({esc(fails['sectors'])})")
    else:
        s = data["sectors"]
        L.append("Best:  " + " · ".join(f"{esc(n)} {pct(v)}" for n, v in s[:2]))
        L.append("Worst: " + " · ".join(f"{esc(n)} {pct(v)}" for n, v in s[-2:]))
    return "\n".join(L)


def split_msg(text: str, limit: int = 4000) -> list[str]:
    parts: list[str] = []
    cur = ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > limit and cur:
            parts.append(cur.rstrip("\n"))
            cur = ""
        cur += line + "\n"
    if cur.strip():
        parts.append(cur.rstrip("\n"))
    return parts


def send(token: str, chat_id: str, text: str) -> None:
    for i, part in enumerate(split_msg(text), 1):
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": part, "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=20)
        if not r.ok:
            # never log the token; Telegram echoes only a description
            raise RuntimeError(
                f"telegram {r.status_code}: {r.json().get('description')}")
        log.info("sent part %d (%d chars)", i, len(part))


def main() -> int:
    load_dotenv(ROOT / ".env")
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        log.error("missing TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID in .env")
        print("ERROR: fill in TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env",
              file=sys.stderr)
        return 2

    today = dt.datetime.now(ET).date()
    log.info("=== run start ===")
    data: dict = {}
    fails: dict = {}

    with ThreadPoolExecutor(max_workers=6) as ex:
        jobs = {"futures": ex.submit(fetch_futures),
                "vix3d": ex.submit(fetch_vix_3d),
                "yields": ex.submit(fetch_yields),
                "sectors": ex.submit(fetch_sectors),
                "econ": ex.submit(fetch_econ, today),
                "earnings": ex.submit(fetch_earnings, today)}
        for name, fut in jobs.items():
            try:
                data[name] = fut.result()
                log.info("%s: OK", name)
            except Exception as e:                           # noqa: BLE001
                fails[name] = type(e).__name__
                log.error("%s: FAIL %s: %s", name, type(e).__name__, e)

    try:
        data["movers"] = fetch_movers(data.get("earnings") or {}, today)
        log.info("movers: OK")
    except Exception as e:                                   # noqa: BLE001
        fails["movers"] = type(e).__name__
        log.error("movers: FAIL %s: %s", type(e).__name__, e)

    core = {"futures", "econ", "earnings", "sectors", "movers"}
    if core <= set(fails):
        log.error("=== total failure: every section dead, nothing sent ===")
        print("ERROR: all data sources failed; nothing sent", file=sys.stderr)
        return 1

    text = render(data, fails)
    print(text)
    try:
        send(token, chat_id, text)
    except Exception as e:                                   # noqa: BLE001
        log.error("telegram send FAILED: %s", e)
        print(f"ERROR: telegram send failed: {e}", file=sys.stderr)
        return 1
    log.info("=== run ok (%d section(s) failed) ===", len(fails))
    return 0


if __name__ == "__main__":
    sys.exit(main())
