# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "numpy==2.5.5",
#   "pandas==3.1.1",
#   "scikit-learn==1.9.1",
#   "statsmodels==0.15.0",
# ]
# ///
"""Reference fixtures for web/src/lib/analytics.

An independent implementation of the analytics specification written with
numpy, pandas, scikit-learn and statsmodels. The backtest here tracks share
counts and cash in currency units; the TypeScript code tracks weights, so the
two formulations check each other.

Run from the repository root:

    uv run tools/reference/generate_fixtures.py

It overwrites web/src/lib/analytics/__fixtures__/*.json. Output is
deterministic (fixed seeds).
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import statsmodels.api as sm
from sklearn.covariance import LedoitWolf
from statsmodels.regression.rolling import RollingOLS

ANN = 252
MAX_CHAIN = 5
MIN_INVVOL = 20
ROLL = 252
EWMA_LAMBDA = 0.94
FACTORS = ["Mkt-RF", "SMB", "HML", "RMW", "CMA", "Mom"]
OUT = Path(__file__).resolve().parents[2] / "web/src/lib/analytics/__fixtures__"

Json = dict[str, Any]


class SpecError(Exception):
    """A case the reference refuses (mirrors an AnalyticsError code)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# --------------------------------------------------------------------------
# Synthetic market data
# --------------------------------------------------------------------------


def day_after(d: str, n: int) -> str:
    return (dt.date.fromisoformat(d) + dt.timedelta(days=n)).isoformat()


def make_market() -> Json:
    rng = np.random.default_rng(20261120)
    days = pd.bdate_range("2015-01-02", periods=840)
    holidays = {"01-01", "07-04", "12-25"}
    cal = [d.strftime("%Y-%m-%d") for d in days if d.strftime("%m-%d") not in holidays]
    n = len(cal)

    vols = np.array([0.010, 0.005, 0.005, 0.004, 0.004, 0.007])
    corr = np.eye(6)
    corr[0, 1] = corr[1, 0] = 0.3
    corr[0, 2] = corr[2, 0] = -0.2
    corr[2, 4] = corr[4, 2] = 0.5
    corr[0, 5] = corr[5, 0] = -0.3
    cov = np.outer(vols, vols) * corr
    f = rng.multivariate_normal(np.full(6, 0.0002), cov, size=n)
    french_rf = np.round(rng.uniform(0.00002, 0.00012, size=n), 6)

    def equity(betas: list[float], alpha: float, idio: float) -> np.ndarray:
        r = alpha + f @ np.array(betas) + french_rf + rng.normal(0, idio, n)
        return np.asarray(100 * np.cumprod(1 + r))

    series: Json = {}

    def add(
        sid: str,
        lo: int,
        hi: int,
        prices: np.ndarray,
        successor: Json | None = None,
        ended: bool = False,
    ) -> None:
        dates = cal[lo : hi + 1]
        close = [float(x) for x in prices[lo : hi + 1]]
        series[sid] = {
            "dates": dates,
            "close": close,
            "status": "ended" if ended or successor is not None else "active",
            "lastDate": dates[-1],
            "successor": successor,
        }

    spy = np.asarray(100 * np.cumprod(1 + f[:, 0] + french_rf + rng.normal(0, 0.001, n)))
    add("SPY", 0, n - 1, spy)
    add("AAA", 0, n - 1, equity([1.1, 0.2, -0.3, 0.1, 0.0, 0.2], 0.0001, 0.008))
    add("BBB", 0, n - 1, equity([0.8, -0.1, 0.4, 0.3, 0.2, -0.1], 0.0, 0.010))
    add("CCC", 0, n - 1, equity([1.3, 0.6, 0.0, -0.2, -0.3, 0.4], -0.0001, 0.012))
    add("DDD", 0, n - 1, equity([0.6, 0.0, 0.1, 0.5, 0.4, 0.0], 0.0002, 0.006))
    add("EEE", 0, n - 1, equity([1.0, 0.3, 0.3, 0.0, 0.0, -0.3], 0.0, 0.009))
    add("NEW", 300, n - 1, equity([1.2, 0.5, -0.2, 0.0, 0.1, 0.0], 0.0, 0.015))
    add("LATE1", 400, n - 1, equity([0.9, 0.0, 0.0, 0.0, 0.0, 0.0], 0.0, 0.01))
    add("LATE2", 400, n - 1, equity([0.7, 0.2, 0.0, 0.0, 0.0, 0.0], 0.0, 0.02))
    add("CONST", 0, n - 1, np.full(n, 50.0))

    # Crypto trades every day; its weekend moves must fold into Monday.
    first, last = dt.date.fromisoformat(cal[0]), dt.date.fromisoformat(cal[-1])
    btc_dates = [(first + dt.timedelta(days=i)).isoformat() for i in range((last - first).days + 1)]
    btc = 300 * np.cumprod(1 + rng.normal(0.001, 0.03, len(btc_dates)))
    series["BTC-USD"] = {
        "dates": btc_dates,
        "close": [float(x) for x in btc],
        "status": "active",
        "lastDate": btc_dates[-1],
        "successor": None,
    }

    zero = equity([1.0, 0, 0, 0, 0, 0], -0.002, 0.02)
    zero[300:350] = 0.0
    zero[350:] = 5.0 * np.cumprod(1 + rng.normal(0, 0.02, n - 350))
    add("ZERO", 0, n - 1, zero)

    def succ(sid: str, ratio: float, cash: float, eff: str) -> Json:
        return {
            "id": sid,
            "ratio": ratio,
            "cashPerShare": cash,
            "effectiveDate": eff,
            "source": "corporate_actions",
        }

    # Rename three calendar days after the last bar (may fall on a weekend).
    add("RENNEW", 200, n - 1, equity([1.0, 0.1, 0.1, 0, 0, 0], 0.0, 0.01))
    ren = {"id": "RENNEW", "ratio": 1, "cashPerShare": 0, "effectiveDate": day_after(cal[500], 3), "source": "edgar_rename"}
    add("REN", 0, 500, equity([0.9, 0.1, 0.1, 0, 0, 0], 0.0, 0.01), ren)
    # Acquired by AAA (half a share plus 10 cash) at the close of its last day.
    add("ACQ", 0, 450, equity([1.0, 0.4, 0.0, 0, 0, 0], 0.0, 0.012), succ("AAA", 0.5, 10.0, cal[450]))
    add("DEL", 0, 380, equity([1.4, 0.5, 0.0, 0, 0, 0], -0.001, 0.02), ended=True)
    # Sequential chain CH1 -> CH2 -> CH3.
    add("CH3", 0, n - 1, equity([0.8, 0, 0, 0, 0, 0], 0.0, 0.01))
    add("CH2", 100, 650, equity([0.9, 0, 0, 0, 0, 0], 0.0, 0.01), succ("CH3", 1.5, 0.0, cal[655]))
    add("CH1", 0, 550, equity([1.0, 0, 0, 0, 0, 0], 0.0, 0.01), succ("CH2", 0.8, 2.0, cal[552]))
    # DB2 has already converted into DB3 when DB1 converts into DB2: both happen at one close.
    add("DB3", 0, n - 1, equity([0.7, 0, 0, 0, 0, 0], 0.0, 0.01))
    add("DB2", 0, 590, equity([0.75, 0, 0, 0, 0, 0], 0.0, 0.01), succ("DB3", 2.0, 1.0, cal[595]))
    add("DB1", 0, 600, equity([0.8, 0, 0, 0, 0, 0], 0.0, 0.01), succ("DB2", 1.0, 0.0, cal[602]))
    # Polymarket contract resolving YES.
    pm = np.clip(0.5 + np.cumsum(rng.normal(0, 0.02, n)), 0.02, 0.98)
    pm[480] = 1.0
    add("pm:RES", 100, 480, pm, ended=True)
    # Ended before the backtest start used in the pre-start scenario.
    add("NEWCO", 0, n - 1, equity([1.0, 0, 0, 0, 0, 0], 0.0, 0.01))
    add("OLD", 0, 150, equity([1.0, 0, 0, 0, 0, 0], 0.0, 0.01), succ("NEWCO", 2.0, 1.5, cal[152]))
    add("GONE", 0, 120, equity([1.0, 0, 0, 0, 0, 0], 0.0, 0.01), ended=True)
    # Cash-out at a deal price (successor id null) three days after the last bar.
    add("DEAL", 0, 420, equity([1.0, 0.2, 0, 0, 0, 0], 0.0, 0.01), {**succ("", 0.0, 77.0, cal[423]), "id": None})

    rf_days = pd.bdate_range("2014-12-01", cal[-1])
    keep = rng.uniform(size=len(rf_days)) > 0.1
    rf_dates = [d.strftime("%Y-%m-%d") for d, k in zip(rf_days, keep, strict=True) if k]
    rf_rate = np.clip(0.02 + np.cumsum(rng.normal(0, 0.0005, len(rf_dates))), 0.0, 0.06)

    # Factor data misses a few calendar dates, so the regression uses the intersection.
    drop = set(rng.choice(np.arange(5, n), size=15, replace=False).tolist())
    fdates = [d for i, d in enumerate(cal) if i not in drop]
    fvalues = {name: [float(f[i, j]) for i in range(n) if i not in drop] for j, name in enumerate(FACTORS)}
    fvalues["RF"] = [float(french_rf[i]) for i in range(n) if i not in drop]

    shares = {
        "AAA": {"source": "edgar", "knownDates": [cal[50], cal[200], cal[400]], "shares": [1.0e9, 1.1e9, 0.9e9]},
        "BBB": {"source": "edgar", "knownDates": ["2014-06-30", cal[300]], "shares": [3.0e9, 3.2e9]},
        "CCC": {"source": "current", "knownDates": ["2026-11-20"], "shares": [5.0e8]},
        "DDD": {"source": "edgar", "knownDates": ["2014-01-15"], "shares": [2.0e9]},
    }

    def meta(asset_class: str, sector: str | None, country: str | None, ccy: str, fund: Json | None = None) -> Json:
        return {"assetClass": asset_class, "sector": sector, "country": country, "currency": ccy, "fundSectorWeights": fund}

    metas = {
        "AAA": meta("equity", "Technology", "United States", "USD"),
        "BBB": meta("equity", None, "United States", "USD", {"Technology": 0.5, "Energy": 0.3, "Healthcare": 0.18}),
        "CCC": meta("equity", None, "Japan", "JPY"),
        "DDD": meta("equity", "Healthcare", None, "USD"),
    }

    return {
        "calendar": cal,
        "series": series,
        "rf": {"dates": rf_dates, "annualRate": [float(x) for x in rf_rate]},
        "factors": {"model": "ff5mom", "region": "us", "factors": FACTORS, "dates": fdates, "values": fvalues},
        "shares": shares,
        "meta": metas,
    }


# --------------------------------------------------------------------------
# Reference backtest (share counts and cash in currency units)
# --------------------------------------------------------------------------


def as_of(cal: list[str], dates: list[str], values: list[float]) -> pd.Series:
    s = pd.Series(values, index=pd.Index(dates), dtype=float)
    return s.reindex(s.index.union(pd.Index(cal))).ffill().reindex(cal)


def returns_of(p: pd.Series) -> pd.Series:
    prev = p.shift(1)
    r = p / prev - 1
    r[prev == 0] = 0.0
    return r


def first_index_on_or_after(cal: list[str], d: str) -> int | None:
    for i, c in enumerate(cal):
        if c >= d:
            return i
    return None


def period(d: str, freq: str) -> str:
    if freq == "monthly":
        return d[:7]
    if freq == "quarterly":
        return f"{d[:4]}Q{(int(d[5:7]) - 1) // 3}"
    return d[:4]


@dataclass
class Slot:
    holdings: list[str]
    id: str
    sign: int
    custom: float
    dead: bool = False


@dataclass
class State:
    shares: dict[str, float] = field(default_factory=dict)
    cash: float = 1.0


def run_backtest(basket: Json, market: Json) -> Json:
    cal: list[str] = market["calendar"]
    series: Json = market["series"]
    holdings = [h["id"] for h in basket["holdings"]]

    ids: list[str] = []
    for h in holdings:
        cur, hops = h, 0
        while True:
            if cur not in ids:
                ids.append(cur)
            s = series[cur]["successor"]
            if s is None or s["id"] is None:
                break
            hops += 1
            if hops > MAX_CHAIN:
                raise SpecError("successor_chain_too_long")
            cur = s["id"]

    P = {i: as_of(cal, series[i]["dates"], series[i]["close"]) for i in set(ids) | {basket["benchmark"]}}
    R = {i: returns_of(p) for i, p in P.items()}
    rf = as_of(cal, market["rf"]["dates"], [(1 + a) ** (1 / ANN) - 1 for a in market["rf"]["annualRate"]])

    need = holdings + [basket["benchmark"]]
    if basket["start"] is None:
        start = next(i for i in range(len(cal)) if all(not np.isnan(P[x].iloc[i]) for x in need))
    else:
        found = first_index_on_or_after(cal, basket["start"])
        assert found is not None
        start = found

    due: dict[str, float] = {}
    for i in ids:
        s = series[i]
        if s["status"] != "ended":
            due[i] = float("inf")
            continue
        when = s["successor"]["effectiveDate"] if s["successor"] else s["lastDate"]
        k = first_index_on_or_after(cal, when)
        due[i] = float("inf") if k is None else k

    slots = [Slot([h["id"]], h["id"], -1 if h["weight"] < 0 else 1, h["weight"]) for h in basket["holdings"]]
    st = State()
    events: list[Json] = []
    warnings: list[Json] = []
    rebalances: list[Json] = []

    def price(i: str, t: int) -> float:
        v = float(P[i].iloc[t])
        assert not np.isnan(v)
        return v

    def value(t: int) -> float:
        return sum(n * price(i, t) for i, n in st.shares.items()) + st.cash

    def process_events(t: int) -> None:
        nonlocal slots
        v_pre = value(t)
        while True:
            slot = next((s for s in slots if not s.dead and due[s.id] <= t), None)
            if slot is None:
                return
            old = slot.id
            succ = series[old]["successor"]
            n = st.shares.pop(old, 0.0)
            p = price(old, t)
            if succ is None or succ["id"] is None:
                deal = None if succ is None else succ["cashPerShare"]
                received = n * (p if deal is None else deal)
                st.cash += received
                slot.dead = True
                events.append(
                    {
                        "id": old,
                        "date": cal[t],
                        "kind": "cash_out",
                        "weight": n * p / v_pre,
                        "cashPerShare": deal,
                        "cashValue": received / v_pre,
                    }
                )
                continue
            ps = price(succ["id"], t)
            st.shares[succ["id"]] = st.shares.get(succ["id"], 0.0) + n * succ["ratio"]
            st.cash += n * succ["cashPerShare"]
            host = next((s for s in slots if s is not slot and not s.dead and s.id == succ["id"]), None)
            if host is None:
                slot.id = succ["id"]
            else:
                if basket["scheme"] != "custom" and host.sign != slot.sign:
                    raise SpecError("slot_sign_conflict")
                host.holdings += slot.holdings
                host.custom += slot.custom
                slots = [s for s in slots if s is not slot]
            events.append(
                {
                    "id": old,
                    "date": cal[t],
                    "kind": "conversion",
                    "weight": n * p / v_pre,
                    "successorValue": n * succ["ratio"] * ps / v_pre,
                    "cashValue": n * succ["cashPerShare"] / v_pre,
                    "merged": host is not None,
                }
            )

    def rebalance(t: int) -> None:
        nonlocal slots
        dead = [s for s in slots if s.dead]
        live = [s for s in slots if not s.dead]
        for s in dead:
            events.append(
                {
                    "id": s.id,
                    "date": cal[t],
                    "kind": "slot_removed",
                    "holdings": s.holdings,
                    "customWeight": s.custom if basket["scheme"] == "custom" else None,
                    "redistributedTo": [x.id for x in live if x.custom * s.custom > 0] if basket["scheme"] == "custom" else [],
                }
            )
        if basket["scheme"] == "custom":
            # Sequential redistribution; proportional scaling makes the order irrelevant.
            for s in dead:
                pool = [x for x in live if x.custom * s.custom > 0]
                tot = sum(abs(x.custom) for x in pool)
                for x in pool:
                    x.custom += s.custom * abs(x.custom) / tot
        slots = live
        lev = basket["leverage"]
        signs = np.array([s.sign for s in slots], dtype=float)
        look: list[str] = []
        if not slots:
            w = np.zeros(0)
        elif basket["scheme"] == "custom":
            w = np.array([s.custom for s in slots])
        elif basket["scheme"] == "equal":
            w = signs / len(slots) * lev
        elif basket["scheme"] == "invvol":
            sig: list[float | None] = []
            for s in slots:
                win = R[s.id].iloc[max(0, t - basket["invvolLookback"] + 1) : t + 1].dropna().to_numpy()
                if len(win) < MIN_INVVOL:
                    sig.append(None)
                else:
                    sd = float(np.std(win, ddof=1))
                    if sd == 0:
                        raise SpecError("zero_volatility")
                    sig.append(sd)
            known = [x for x in sig if x is not None]
            if not known:
                w = signs / len(slots) * lev
                warnings.append({"date": cal[t], "code": "invvol_equal_weight", "ids": [s.id for s in slots]})
            else:
                med = float(np.median(known))
                short = [s.id for s, x in zip(slots, sig, strict=True) if x is None]
                if short:
                    warnings.append({"date": cal[t], "code": "invvol_median_sigma", "ids": short})
                raw = np.array([sg / (x if x is not None else med) for sg, x in zip(signs, sig, strict=True)])
                w = raw / np.abs(raw).sum() * lev
        else:
            caps = []
            for s in slots:
                e = market["shares"][s.id]
                known_idx = [k for k, d in enumerate(e["knownDates"]) if d <= cal[t]]
                if e["source"] == "current" or not known_idx:
                    look.append(s.id)
                sh = e["shares"][known_idx[-1]] if known_idx else e["shares"][0]
                caps.append(price(s.id, t) * sh)
            raw = signs * np.array(caps)
            w = raw / np.abs(raw).sum() * lev
        v = value(t)
        st.shares = {}
        for s, wi in zip(slots, w, strict=True):
            p = price(s.id, t)
            if p == 0:
                if wi != 0:
                    raise SpecError("zero_price_allocation")
                st.shares[s.id] = 0.0
            else:
                st.shares[s.id] = float(wi) * v / p
        st.cash = (1 - float(np.sum(w))) * v
        rebalances.append(
            {"date": cal[t], "weights": {s.id: float(wi) for s, wi in zip(slots, w, strict=True)}, "cash": st.cash / v, "lookAhead": look}
        )

    nav = [1.0]
    rets: list[float] = []
    rfs: list[float] = []
    bench: list[float] = []
    cash = []
    process_events(start)
    rebalance(start)
    cash.append(st.cash / value(start))
    v_prev = value(start)
    for t in range(start + 1, len(cal)):
        r_f = float(rf.iloc[t])
        assert not np.isnan(r_f)
        st.cash *= 1 + r_f
        for i in list(st.shares):
            if price(i, t) == 0:
                st.shares[i] = 0.0  # a worthless position stays worthless
        process_events(t)
        v = value(t)
        if v <= 0:
            raise SpecError("nav_nonpositive")
        rets.append(v / v_prev - 1)
        nav.append(nav[-1] * v / v_prev)
        rfs.append(r_f)
        bench.append(float(R[basket["benchmark"]].iloc[t]))
        if period(cal[t - 1], basket["rebalance"]) != period(cal[t], basket["rebalance"]) and basket["rebalance"] != "none":
            rebalance(t)
        cash.append(st.cash / v)
        v_prev = v
    vt = value(len(cal) - 1)
    latest = {i: n * price(i, len(cal) - 1) / vt for i, n in st.shares.items()}
    return {
        "dates": cal[start:],
        "nav": nav,
        "returns": rets,
        "rf": rfs,
        "benchmarkReturns": bench,
        "cash": cash,
        "rebalances": rebalances,
        "latest": {"date": cal[-1], "weights": latest, "cash": st.cash / vt},
        "events": events,
        "warnings": warnings,
    }


# --------------------------------------------------------------------------
# Performance statistics
# --------------------------------------------------------------------------


def none_if_nan(x: float) -> float | None:
    return None if x is None or not np.isfinite(x) else float(x)


def stats(bt: Json) -> Json:
    dates = bt["dates"]
    nav = pd.Series(np.asarray(bt["nav"], dtype=float), index=pd.Index(dates))
    r = np.array(bt["returns"])
    rf = np.array(bt["rf"])
    rb = np.array(bt["benchmarkReturns"])
    n = len(r)
    e = r - rf

    # The share-count backtest reproduces a weight-1 position's return only to
    # rounding, so "constant" allows 1e-14 of noise; the TypeScript weight
    # formulation is exact in those cases.
    def constant(x: np.ndarray) -> bool:
        return bool(np.ptp(x) <= 1e-14)

    dd = nav / nav.cummax() - 1
    mdd = float(dd.min())
    if mdd < 0:
        trough = str(dd.idxmin())
        peak = str(nav.loc[:trough].idxmax())
        after = nav.loc[trough:].iloc[1:]
        rec = after[after >= nav.loc[peak]]
        dd_info = {"max": mdd, "peakDate": peak, "troughDate": trough, "recoveryDate": rec.index[0] if len(rec) else None}
    else:
        dd_info = {"max": 0.0, "peakDate": None, "troughDate": None, "recoveryDate": None}
    dd_info["series"] = [float(x) for x in dd]

    cagr = (nav.iloc[-1] / nav.iloc[0]) ** (ANN / n) - 1 if n > 0 else None
    downside = np.sqrt(np.mean(np.minimum(e, 0) ** 2)) if n > 0 else 0.0
    rr = pd.Series(r)
    roll_ret = (nav / nav.shift(ROLL) - 1).iloc[ROLL:]
    roll_vol = [float(np.std(r[k - ROLL : k], ddof=1) * np.sqrt(ANN)) for k in range(ROLL, len(nav))]
    years = []
    base = nav.iloc[0]
    for y, grp in nav.groupby(nav.index.str[:4], sort=True):
        years.append({"year": int(y), "startDate": grp.index[0], "endDate": grp.index[-1], "return": float(grp.iloc[-1] / base - 1)})
        base = grp.iloc[-1]

    if n >= 2:
        x = rb - rf
        if constant(x):
            beta = alpha = None
        else:
            fit = sm.OLS(e, sm.add_constant(x)).fit()
            beta = float(fit.params[1])
            alpha = float(fit.params[0] * ANN)
        active = r - rb
        te = float(np.std(active, ddof=1) * np.sqrt(ANN)) if not constant(active) else 0.0
        bstats = {
            "beta": beta,
            "alpha": alpha,
            "trackingError": te,
            "informationRatio": None if te == 0 else float(np.mean(active) * ANN / te),
            "correlation": None if constant(r) or constant(rb) else float(np.corrcoef(r, rb)[0, 1]),
        }
        vol = 0.0 if constant(r) else float(rr.std(ddof=1) * np.sqrt(ANN))
        sharpe = None if constant(e) else float(np.mean(e) / np.std(e, ddof=1) * np.sqrt(ANN))
    else:
        bstats = {"beta": None, "alpha": None, "trackingError": None, "informationRatio": None, "correlation": None}
        vol = None
        sharpe = None
    return {
        "cumulativeReturn": float(nav.iloc[-1] / nav.iloc[0] - 1),
        "cagr": none_if_nan(cagr) if cagr is not None else None,
        "annualVol": vol,
        "sharpe": sharpe,
        "sortino": None if n == 0 or downside == 0 else float(np.mean(e) / downside * np.sqrt(ANN)),
        "calmar": None if cagr is None or mdd == 0 else float(cagr / -mdd),
        "drawdown": dd_info,
        "rolling": {"dates": list(roll_ret.index), "return": [float(x) for x in roll_ret], "vol": roll_vol},
        "calendarYears": years,
        "benchmark": bstats,
    }


# --------------------------------------------------------------------------
# Risk and concentration
# --------------------------------------------------------------------------


def risk(bt: Json, market: Json, estimator: str, window: int = 252) -> Json | None:
    cal = market["calendar"]
    w_map: dict[str, float] = bt["latest"]["weights"]
    ids = list(w_map)
    if not ids:
        return None
    rets = pd.DataFrame({i: returns_of(as_of(cal, market["series"][i]["dates"], market["series"][i]["close"])) for i in ids})
    ok = rets.notna().all(axis=1).to_numpy()
    first = len(cal)
    while first - 1 >= 0 and len(cal) - first < window and ok[first - 1]:
        first -= 1
    X = rets.iloc[first:].to_numpy()
    if estimator == "sample":
        cov = np.cov(X, rowvar=False, ddof=1)
        shrink = None
    elif estimator == "ledoit_wolf":
        lw = LedoitWolf().fit(X)
        cov, shrink = lw.covariance_, float(lw.shrinkage_)
    else:
        t = len(X)
        wts = (1 - EWMA_LAMBDA) * EWMA_LAMBDA ** np.arange(t - 1, -1, -1) / (1 - EWMA_LAMBDA**t)
        cov = (X * wts[:, None]).T @ X
        shrink = None
    cov = np.atleast_2d(cov) * ANN
    w = np.array([w_map[i] for i in ids])
    var = float(w @ cov @ w)
    vols = np.sqrt(np.diag(cov))
    out: Json = {
        "ids": ids,
        "observations": len(X),
        "startDate": cal[first],
        "covariance": cov.tolist(),
        "vols": vols.tolist(),
        "shrinkage": shrink,
    }
    with np.errstate(divide="ignore", invalid="ignore"):
        corr = cov / np.outer(vols, vols)
    out["correlation"] = [
        [None if vols[i] == 0 or vols[j] == 0 else (1.0 if i == j else float(corr[i, j])) for j in range(len(ids))] for i in range(len(ids))
    ]
    if var <= 0:
        out.update({"sigmaP": 0.0, "contributions": None, "diversificationRatio": None, "riskEffectiveN": None})
        return out
    sp = np.sqrt(var)
    mctr = cov @ w / sp
    ctr = w * mctr
    pct = ctr / sp
    out.update(
        {
            "sigmaP": float(sp),
            "contributions": {"mctr": mctr.tolist(), "ctr": ctr.tolist(), "pctCtr": pct.tolist()},
            "diversificationRatio": float(np.abs(w) @ vols / sp),
            "riskEffectiveN": float(1 / np.sum(pct**2)),
        }
    )
    return out


def concentration(weights: dict[str, float]) -> Json | None:
    a = np.abs(np.array(list(weights.values())))
    g = a.sum()
    if g == 0:
        return None
    s = np.sort(a / g)[::-1]
    hhi = float(np.sum(s**2))
    return {"gross": float(g), "hhi": hhi, "effectiveN": 1 / hhi, "top5": float(s[:5].sum()), "top10": float(s[:10].sum())}


def exposures(weights: dict[str, float], metas: Json) -> Json:
    out: dict[str, dict[str, list[float]]] = {k: {} for k in ("assetClass", "sector", "country", "currency")}

    def add(dim: str, key: str, w: float) -> None:
        cur = out[dim].setdefault(key, [0.0, 0.0])
        cur[0] += w
        cur[1] += abs(w)

    for i, w in weights.items():
        m = metas[i]
        add("assetClass", m["assetClass"], w)
        add("country", m["country"] or "Unclassified", w)
        add("currency", m["currency"], w)
        if m["fundSectorWeights"] is None:
            add("sector", m["sector"] or "Unclassified", w)
        else:
            tot = sum(m["fundSectorWeights"].values())
            for k, f in m["fundSectorWeights"].items():
                add("sector", k, w * f / tot)
    return {dim: {k: {"net": v[0], "gross": v[1]} for k, v in d.items()} for dim, d in out.items()}


# --------------------------------------------------------------------------
# Factor regression
# --------------------------------------------------------------------------


def factor_regression(bt: Json, market: Json) -> Json:
    fac = market["factors"]
    fdf = pd.DataFrame(fac["values"], index=fac["dates"])
    r = pd.Series(bt["returns"], index=bt["dates"][1:])
    common = r.index.intersection(fdf.index)
    y = (r[common] - fdf.loc[common, "RF"]).to_numpy()
    X = sm.add_constant(fdf.loc[common, FACTORS].to_numpy())
    fit = sm.OLS(y, X).fit()
    roll = RollingOLS(y, X, window=ROLL).fit().params[ROLL - 1 :]
    n = len(y)
    b = fit.params[1:]
    sys_var = float(b @ np.cov(X[:, 1:], rowvar=False, ddof=1) @ b)
    idio = float(fit.ssr / (n - 1))
    total = float(np.var(y, ddof=1))
    return {
        "observations": n,
        "startDate": common[0],
        "endDate": common[-1],
        "intercept": {"estimate": float(fit.params[0]), "stdError": float(fit.bse[0]), "tStat": float(fit.tvalues[0])},
        "alphaAnnual": float(fit.params[0] * ANN),
        "betas": {
            f: {"estimate": float(fit.params[j + 1]), "stdError": float(fit.bse[j + 1]), "tStat": float(fit.tvalues[j + 1])}
            for j, f in enumerate(FACTORS)
        },
        "r2": float(fit.rsquared),
        "rolling": {"dates": list(common[ROLL - 1 :]), "betas": {f: roll[:, j + 1].tolist() for j, f in enumerate(FACTORS)}},
        "risk": {
            "total": total * ANN,
            "systematic": sys_var * ANN,
            "idiosyncratic": idio * ANN,
            "systematicShare": sys_var / total,
            "idiosyncraticShare": idio / total,
        },
    }


# --------------------------------------------------------------------------
# Scenarios
# --------------------------------------------------------------------------


def basket(name: str, holdings: list[tuple[str, float]], **kw: Any) -> Json:
    b: Json = {
        "v": 1,
        "name": name,
        "holdings": [{"id": i, "weight": w} for i, w in holdings],
        "scheme": "custom",
        "invvolLookback": 63,
        "rebalance": "monthly",
        "leverage": 1,
        "benchmark": "SPY",
        "overlays": [],
        "start": None,
    }
    b.update(kw)
    return b


def scenarios(cal: list[str]) -> list[tuple[str, Json, bool]]:
    saturday = next(d for d in (day_after(c, 1) for c in cal[100:]) if dt.date.fromisoformat(d).weekday() == 5)
    return [
        ("custom_monthly", basket("Custom monthly", [("AAA", 0.4), ("BBB", 0.3), ("CCC", 0.2), ("DDD", 0.1)]), True),
        (
            "custom_long_short_quarterly",
            basket("Long short", [("AAA", 0.9), ("BBB", 0.7), ("CCC", -0.3), ("DDD", 0.0)], rebalance="quarterly", start=saturday),
            False,
        ),
        (
            "equal_levered_annual",
            basket("Equal 2x", [("AAA", 1), ("BBB", 0), ("CCC", -1), ("BTC-USD", 1)], scheme="equal", leverage=2, rebalance="annual"),
            False,
        ),
        (
            "invvol_median_fallback",
            basket("Invvol", [("AAA", 1), ("BBB", -1), ("NEW", 1)], scheme="invvol", leverage=1.5),
            False,
        ),
        (
            "invvol_equal_fallback",
            basket("Invvol late", [("LATE1", 1), ("LATE2", 1)], scheme="invvol", invvolLookback=40),
            False,
        ),
        (
            "mcap_lookahead",
            basket("Mcap", [("AAA", 1), ("BBB", 1), ("CCC", -1), ("DDD", 1)], scheme="mcap", start=cal[10]),
            False,
        ),
        (
            "events_custom_monthly",
            basket(
                "Events custom",
                [
                    ("AAA", 0.25),
                    ("REN", 0.15),
                    ("ACQ", 0.1),
                    ("DEL", -0.1),
                    ("EEE", -0.15),
                    ("CH1", 0.1),
                    ("DB1", 0.1),
                    ("pm:RES", 0.05),
                    ("BTC-USD", 0.1),
                    ("DEAL", 0.05),
                ],
                start=cal[150],
            ),
            False,
        ),
        (
            "events_buy_and_hold",
            basket(
                "Events none", [("AAA", 0.3), ("ZERO", 0.2), ("DEL", 0.15), ("REN", 0.2), ("ACQ", 0.1), ("DEAL", 0.05)], rebalance="none"
            ),
            False,
        ),
        (
            "events_invvol_merge",
            basket("Events invvol", [("AAA", 1), ("ACQ", 1), ("REN", 1), ("DEL", -1)], scheme="invvol", rebalance="quarterly"),
            False,
        ),
        (
            "events_equal_prestart",
            basket("Pre-start", [("OLD", 1), ("GONE", 1), ("AAA", 1), ("BBB", -1)], scheme="equal", start=cal[200]),
            False,
        ),
        ("single_asset", basket("Single", [("AAA", 1.0)], rebalance="none", benchmark="AAA"), False),
        ("constant_series", basket("Constant", [("CONST", 1.0)], rebalance="none"), False),
    ]


def main() -> None:
    market = make_market()
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "market.json").write_text(json.dumps(market))
    names = []
    for name, b, with_factors in scenarios(market["calendar"]):
        bt = run_backtest(b, market)
        expected: Json = {"backtest": bt, "stats": stats(bt)}
        expected["risk"] = {e: risk(bt, market, e) for e in ("sample", "ledoit_wolf", "ewma")}
        expected["concentration"] = concentration(bt["latest"]["weights"])
        if with_factors:
            expected["factors"] = factor_regression(bt, market)
            expected["exposures"] = exposures(bt["latest"]["weights"], market["meta"])
        (OUT / f"{name}.json").write_text(json.dumps({"name": name, "basket": b, "expected": expected}))
        names.append(name)
    (OUT / "index.json").write_text(json.dumps(names, indent=1) + "\n")
    print(f"wrote {len(names)} scenarios to {OUT}")


if __name__ == "__main__":
    main()
