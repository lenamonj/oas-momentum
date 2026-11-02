"""
OAS Relative Momentum - reference implementation (standard library only).

Inputs: the current index table in the repo (one row per constituent: attributes, peer-group ids, issuer) and
a fetch function that pulls par-equivalent OAS history from Bloomberg for a given set of ISINs. Output: for any
bond or issuer, four columns (Mom21, Conf21, Mom63x, Conf63x) and an issuer roll-up, computed at request time,
plus the display layer (Reading, Trend, display_row). Attributes and peer groups are those in the current table
and are held fixed across the window.

Sign convention: positive = relative tightening.
"""
import math
from collections import defaultdict
from datetime import date, datetime, timedelta

METHOD_VERSION = "6.0"

# ---------------- parameters (Appendix A) ----------------
HORIZONS = {"m21": (21, 0), "m63x": (42, 21)}   # name: (k nights, skip nights)
VOL_WINDOW = 126
MIN_FILL = 0.90
MIN_OAS_ABS, MAX_OAS_FLOOR, FLOOR_FRAC = 15.0, 40.0, 0.35
MAX_GAP_NIGHTS = 3
MIN_TTM_YEARS = 1.0
MIN_SPREAD_DUR = 1.0
CALL_EXCL_CAL_DAYS = 91
STALE_WINDOW, STALE_MIN_MOVES = 21, 10
JUMP_MAD_MULT, JUMP_MIN_ABS = 6.0, 0.10
CUM_WINDOW, CUM_MAD_MULT, CUM_MIN_ABS = 5, 6.0, 0.15
JUMP_CELL_MIN = 5
ROBUST_N = 8
N0_BENCH = 5
THIN_CELL_OUTER_ONLY = 4
MIN_OUTER = 6
N_MEDIAN_WINDOW = 63
RHO_FLOOR = -0.50
RHO_DEFAULT = -0.3            # pooled rho used when no bond in the loaded cells has an own rho
RHO_SE_NUM = lambda rho: 1 - 3 * rho ** 2 + 4 * rho ** 4
BRACKET_MIN_FRAC = 0.1        # a bracket k + 2(k-1) rho at or below 0.1 k blanks the window
Z_N0_INIT, Z_N0_MIN, Z_N0_MAX = 12, 5, 60
Z_SAMPLING_CONST = 1.3
Z_MIN_OTHER = 5
Z_CLIP = 3.0
Z_MEDIAN_VAR = 1.571          # pi/2: variance of the median of n unit-variance values is about 1.571 / n
MAD_SCALE = 1.4826
CONF_STEP_SHARE_C, CONF_STEP_SHARE_A = 0.80, 0.60
CONF_STEP_MIN_SCORE = 2.0     # step test applies only when max(|raw score|, |z|) is at least this
CONF_JUMP_EVENTS_C = 2        # C at this many jump events starting inside the window
CONF_REF_A = 8
TRADING_NIGHT_MIN_SHARE = 0.5 # a date is a trading night when at least this share of pulled bonds has a spread

# display layer (README "With the gap", spec 4)
READ_GAP_BP = 5.0             # cheap at Gap >= +5, rich at Gap <= -5, else fair
READ_MOVE = 1.0               # up at z >= +1.0, down at z <= -1.0, else flat (Reading and Trend)

REQUIRED = ("issuer_id", "oas", "spread_dur", "ttm_years", "amount_outstanding", "call_status",
            "rating_bucket", "cell_fine", "cell_wide", "cell_l3")
NUMERIC = ("oas", "spread_dur", "ttm_years", "amount_outstanding")
# call_status in {"bullet", "callable", "unknown"}; next_call_date optional (datetime.date)

# blank reasons, in order of precedence (the first that applies is displayed)
REASON_ORDER = ("not_in_table", "no_spread_tonight", "bad_row", "new_line",
                "multi_night_change", "not_contributor_today", "ttm", "call_unknown", "call_window", "spread_dur",
                "burn_in", "horizon_fill", "vol_fill", "zero_var", "variance_bracket",
                "too_few_reference_issuers", "no_scored_line")
REASON_TEXT = {
    "not_in_table": "not in the table",
    "no_spread_tonight": "no spread tonight",
    "bad_row": "bad data row tonight",
    "multi_night_change": "multi-night change tonight",
    "not_contributor_today": "stale marks (fewer than 10 changes in the last 21 nights)",
    "ttm": "maturity under 1 year",
    "call_unknown": "call status unknown",
    "call_window": "call within 91 days",
    "spread_dur": "spread duration under 1.0",
    "burn_in": "not enough pulled history",
    "new_line": "new line ({n} nights of history)",
    "horizon_fill": "window not covered",
    "vol_fill": "volatility period not covered",
    "zero_var": "zero variance",
    "variance_bracket": "volatility bracket too small",
    "too_few_reference_issuers": "too few reference issuers",
    "no_scored_line": "no line scored",
}


# ---------------- helpers ----------------

def _median(v):
    v = sorted(v); n = len(v)
    if n == 0:
        return None
    m = n // 2
    return v[m] if n % 2 else 0.5 * (v[m - 1] + v[m])


def _pvar(v):
    n = len(v)
    if n < 2:
        return None
    mu = sum(v) / n
    return sum((x - mu) ** 2 for x in v) / n


def _finite(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _is_date(x):
    return isinstance(x, date) and not isinstance(x, datetime)


def _centre_scale(vals):
    """Median of vals and 1.4826 x the median absolute deviation about it."""
    c = _median(vals)
    return c, _median([abs(v - c) for v in vals]) * MAD_SCALE


def _lag1(v):
    """Lag-1 autocorrelation of a list that may contain None: mean cross-product of demeaned pairs over the
    pair count, divided by the variance over n. Returns (rho, n_obs) or (None, n_obs). Called only on volatility
    periods that pass the 90% fill test."""
    vals = [x for x in v if x is not None]
    n = len(vals)
    if n < 2:
        return None, n
    mu = sum(vals) / n
    var = sum((x - mu) ** 2 for x in vals) / n
    pairs = [(a, b) for a, b in zip(v, v[1:]) if a is not None and b is not None]
    if var == 0 or not pairs:
        return None, n
    return sum((a - mu) * (b - mu) for a, b in pairs) / len(pairs) / var, n


# ---------------- display layer ----------------

def _move(z):
    return "up" if z >= READ_MOVE else ("down" if z <= -READ_MOVE else "flat")


def reading(gap, z21):
    """Reading label from the unrounded Gap (bp, positive = wider than peers) and the unrounded displayed Mom21.
    None when either is None (the caller shows Mom21's blank reason when Mom21 is blank)."""
    if gap is None or z21 is None:
        return None
    g = "cheap" if gap >= READ_GAP_BP else ("rich" if gap <= -READ_GAP_BP else "fair")
    word = {"up": "outperforming", "down": "underperforming", "flat": "flat"}[_move(z21)]
    return f"{g} and {word}"


def trend(z63, z21):
    """Trend label from the unrounded displayed Mom63x and Mom21. None when either is None."""
    if z63 is None or z21 is None:
        return None
    a, b = _move(z63), _move(z21)
    if a == "flat" and b == "flat":
        return "flat"
    if a == "flat":
        return "new"
    if b == "flat":
        return "faded"
    return "continuing" if a == b else "reversing"


def blank_code(dg, name):
    """The displayed blank reason code for window `name` from a diag entry: the bond-level reason blank["all"]
    if set, else the window's reason blank[name] (score stage or z stage; never both), else None."""
    if not dg:
        return "not_in_table"
    b = dg.get("blank", {})
    return b.get("all") or b.get(name)


def _set_display(dg):
    """display_reason: exactly one entry per window, the displayed code or None for a window with a z."""
    dg["display_reason"] = {name: blank_code(dg, name) for name in HORIZONS}
    return dg


def reason_text(code, nights=None):
    if code is None:
        return None
    t = REASON_TEXT[code]
    return t.format(n=nights) if "{n}" in t else t


def display_row(isin, rec, gap, dg=None):
    """Ordered row `ISIN | Issuer | Gap | Mom21 | Conf21 | Mom63x | Conf63x | Reading | Trend` for one bond.
    rec: bonds[isin], or None for a target with no score in either window (then dg = diag[isin] gives the reason).
    Values are unrounded; Reading carries Mom21's blank reason text when Mom21 is blank."""
    src = rec if rec is not None else {}
    z21, z63 = src.get("m21_z_disp"), src.get("m63x_z_disp")
    if z21 is None:
        if rec is not None:
            code, n = rec.get("m21_blank"), rec.get("history_nights")
        else:
            code, n = blank_code(dg, "m21"), (dg or {}).get("history_nights")
        read = reason_text(code, n)
    else:
        read = reading(gap, z21)
    return {"ISIN": isin, "Issuer": src.get("issuer_id") if rec is not None else (dg or {}).get("issuer_id"),
            "Gap": gap, "Mom21": z21, "Conf21": src.get("m21_conf"), "Mom63x": z63, "Conf63x": src.get("m63x_conf"),
            "Reading": read, "Trend": trend(z63, z21)}


def issuer_display_row(issuer_id, rec, gap):
    """Ordered issuer row `Issuer | Gap | Mom21 | Conf21 | Mom63x | Conf63x | Reading | Trend | n | max_w | pct_pos
    | sd | coverage` (roll-up fields for the Mom21 window). rec: issuers[issuer_id] or None."""
    src = rec or {}
    z21, z63 = src.get("m21_z"), src.get("m63x_z")
    read = reading(gap, z21) if z21 is not None else reason_text((src.get("m21_blank") or "no_scored_line") if rec is not None else "not_in_table")
    return {"Issuer": issuer_id, "Gap": gap, "Mom21": z21, "Conf21": src.get("m21_conf"), "Mom63x": z63,
            "Conf63x": src.get("m63x_conf"), "Reading": read, "Trend": trend(z63, z21), "n": src.get("m21_n"),
            "max_w": src.get("m21_max_w"), "pct_pos": src.get("m21_pct_pos"), "sd": src.get("m21_sd"),
            "coverage": src.get("m21_coverage")}


# ---------------- history ----------------

HISTORY_NIGHTS = 215          # trading nights of spread history a request needs (longest window + skip + vol + warm-up)


class History:
    """
    files: {date: {isin: row}}. A row is a dict with the REQUIRED fields (oas = par-equivalent OAS, bp)
    and optionally next_call_date. Rows that fail the field checks are skipped and listed in `skipped`.
    Built by build_history() from the current table plus pulled spread history.
    """

    def __init__(self, files, pull=None):
        self.dates = sorted(files)
        self.rows = {}
        self.skipped = []
        self.pull = pull or {}        # build_history: n_pulled, n_any and the per-date spread count
        for t in self.dates:
            kept = {}
            for isin in sorted(files[t]):
                r = files[t][isin]
                why = _check(r)
                if why:
                    self.skipped.append((t, isin, why))
                else:
                    kept[isin] = r
            self.rows[t] = kept

    def row(self, t, isin):
        return self.rows[t].get(isin)


def build_history(current, fetch, isins, n_nights=HISTORY_NIGHTS):
    """
    current: {isin: row} from the current index table (every field but oas; a current oas is ignored).
    fetch(isins) -> {isin: {datetime.date: par_equiv_oas}}: the Bloomberg pull composed with ParEquivOAS.
    A trading night is a date on which at least half of the pulled bonds that are in `current` and have any
    finite positive spread have a finite positive spread; other dates are dropped. A pulled ISIN that is not in
    `current` is ignored everywhere: it enters neither n_any nor any date's count, and gets no row. Only the last
    n_nights trading nights are used. Each night's row is the current attributes with that night's spread.
    hist.pull records n_pulled (ISINs passed to fetch), n_any and count {date: bonds with a spread that date}.
    """
    isins = sorted(isins)
    pulled = fetch(isins) or {}
    cnt, n_any = defaultdict(int), 0
    for isin in isins:
        if isin not in current:
            continue
        series = pulled.get(isin) or {}
        has = False
        for d, v in series.items():
            if not _is_date(d):
                raise TypeError(f"fetch returned a {type(d).__name__} key for {isin}; keys must be datetime.date")
            if _finite(v) and v > 0:
                cnt[d] += 1; has = True
        n_any += has
    dates = sorted(d for d, c in cnt.items() if c >= TRADING_NIGHT_MIN_SHARE * n_any)[-n_nights:]
    files = {}
    for t in dates:
        rows = {}
        for isin in isins:
            v = (pulled.get(isin) or {}).get(t)
            if v is not None and isin in current:
                rows[isin] = dict(current[isin], oas=v)
        files[t] = rows
    return History(files, {"n_pulled": len(isins), "n_any": n_any, "count": dict(cnt)})


def needed_isins(current, targets):
    """Bonds in the current table sharing a fine, wide or level-3 cell with any target."""
    cells = set()
    for isin in targets:
        r = current.get(isin)
        if r:
            cells.update({("f", r["cell_fine"]), ("w", r["cell_wide"]), ("l", r["cell_l3"])})
    out = {i for i in targets if i in current}
    for isin, r in current.items():
        if ("f", r["cell_fine"]) in cells or ("w", r["cell_wide"]) in cells or ("l", r["cell_l3"]) in cells:
            out.add(isin)
    return out


def request(current, fetch, query=None, tables=None):
    """
    The entry point. query: None (every bond), a list of ISINs, or {"issuer": issuer_id}.
    Resolves the bonds whose history is needed from the current table, pulls it, scores as of the latest trading
    night. Never raises for an unknown ISIN, an unknown issuer, an empty query or an empty pull.
    """
    if query is None:
        targets = set(current)
    elif isinstance(query, dict):
        targets = {i for i, r in current.items() if r.get("issuer_id") == query.get("issuer")}
    else:
        targets = set(query)
    if not targets:
        return {}, {}, {"_query": _set_display({"blank": {"all": "not_in_table"}}),
                        "_global": {"as_of": None, "request_night": {"as_of": None, "n_pulled": 0, "n_any": 0, "n_spread": 0}}}
    isins = needed_isins(current, targets) if query is not None else set(current)
    hist = build_history(current, fetch, isins) if isins else History({})
    if hist.dates:
        bonds, issuers, diag = score(hist, query if query is None or isinstance(query, dict) else sorted(targets), None, tables)
    else:
        bonds, issuers, diag = {}, {}, {}
    diag.pop("_query", None)
    # one taxonomy for every target: not_in_table only for an ISIN not in the current table; a bond in the table
    # with no valid row on the request night is no_spread_tonight unless score() found a bad row (bad_row)
    for i in sorted(targets):
        dg = diag.get(i)
        if i not in current:
            diag[i] = _set_display({"blank": {"all": "not_in_table"}})
        elif dg is None or dg.get("blank", {}).get("all") == "not_in_table":
            diag[i] = _set_display({"blank": {"all": "no_spread_tonight"}, "issuer_id": current[i].get("issuer_id")})
        else:
            _set_display(dg)
    g = diag.setdefault("_global", {})
    as_of = hist.dates[-1] if hist.dates else None
    g["as_of"] = as_of
    g["request_night"] = {"as_of": as_of, "n_pulled": hist.pull.get("n_pulled", 0), "n_any": hist.pull.get("n_any", 0),
                          "n_spread": hist.pull.get("count", {}).get(as_of, 0)}
    if query is None or isinstance(query, dict):
        lines = defaultdict(list)
        for i in sorted(targets):
            if i in current:
                lines[current[i].get("issuer_id")].append(i)
        for iss, ls in lines.items():
            if iss not in issuers:
                issuers[iss] = _empty_issuer(as_of)
            issuers[iss]["n_lines"] = len(ls)
    return bonds, issuers, diag


def _empty_issuer(as_of):
    """Issuer row with no scored line in either window: every window field None, n 0, reason no_scored_line."""
    out = {"as_of": as_of, "n_bonds": 0, "n_cells": 0, "n_lines": 0}
    for name in HORIZONS:
        out[f"{name}_n_dropped_no_weight"] = 0
        _blank_issuer_window(out, name)
    return out


def _blank_issuer_window(out, name):
    for f in ("z", "raw", "conf", "max_w", "pct_pos", "sd", "coverage"):
        out[f"{name}_{f}"] = None
    out[f"{name}_n"] = 0
    out[f"{name}_blank"] = "no_scored_line"


def _check(r):
    missing = [k for k in REQUIRED if k not in r or r[k] is None]
    if missing:
        return f"missing {missing}"
    bad = [k for k in NUMERIC if not _finite(r[k])]
    if bad:
        return f"non-finite {bad}"
    if r["oas"] <= 0 or r["amount_outstanding"] <= 0 or r["spread_dur"] < 0 or r["ttm_years"] < 0:
        return "implausible value"
    if r["call_status"] not in ("bullet", "callable", "unknown"):
        return "bad call_status"
    nc = r.get("next_call_date")
    if nc is not None and not _is_date(nc):
        return "next_call_date not a datetime.date"
    return None


# ---------------- derivation ----------------

def _needed_isins(hist, targets, start, end):
    """Bonds sharing a fine, wide or level-3 cell with any target on any night in [start, end]."""
    cells = set()
    for i in range(start, end + 1):
        rows = hist.rows[hist.dates[i]]
        for isin in targets:
            r = rows.get(isin)
            if r:
                cells.update({("f", r["cell_fine"]), ("w", r["cell_wide"]), ("l", r["cell_l3"])})
    out = set(targets)
    for i in range(start, end + 1):
        for isin, r in hist.rows[hist.dates[i]].items():
            if ("f", r["cell_fine"]) in cells or ("w", r["cell_wide"]) in cells or ("l", r["cell_l3"]) in cells:
                out.add(isin)
    return out


def derive(hist, isins, start, end):
    """Per night in start..end: daily change with relative floor (gap tolerant), stale mask, contributor flag,
    jump flags, leave-own-issuer-out benchmarks with shrinkage, rel_1. Each night uses its own file's attributes.
    Bonds are processed in sorted ISIN order so every float sum is reproducible."""
    isins = sorted(isins)
    out = {}
    last_valid = {}
    for i in range(max(0, start - MAX_GAP_NIGHTS), start):
        for isin in isins:
            r = hist.row(hist.dates[i], isin)
            if r:
                last_valid[isin] = (i, r["oas"])
    for i in range(start, end + 1):
        t = hist.dates[i]
        rows = {isin: hist.rows[t][isin] for isin in isins if isin in hist.rows[t]}
        cell_oas = defaultdict(list)
        for r in rows.values():
            cell_oas[r["cell_fine"]].append(r["oas"])
        floor_of = {c: min(MAX_OAS_FLOOR, max(MIN_OAS_ABS, FLOOR_FRAC * _median(v))) for c, v in cell_oas.items()}
        prev_nights = [out[j] for j in range(max(start, i - (STALE_WINDOW - 1)), i)]
        inputs = {}
        for isin, r in rows.items():
            prev = last_valid.get(isin)
            if prev is not None and i - prev[0] > MAX_GAP_NIGHTS:
                prev = None
            d1, gap = None, 0
            if prev is not None:
                d1 = (r["oas"] - prev[1]) / max(prev[1], floor_of[r["cell_fine"]])
                gap = i - prev[0]
            last_valid[isin] = (i, r["oas"])
            moved = d1 is not None and r["oas"] != prev[1]
            moves = sum(1 for p in prev_nights if isin in p["inputs"] and p["inputs"][isin]["moved"]) + (1 if moved else 0)
            stale = not (i - start >= STALE_WINDOW - 1 and moves >= STALE_MIN_MOVES)
            nc = r.get("next_call_date")
            if r["call_status"] == "bullet":
                call_ok, call_reason = True, None
            elif r["call_status"] == "callable" and nc is not None and nc > t:
                call_ok = nc > t + timedelta(days=CALL_EXCL_CAL_DAYS)
                call_reason = None if call_ok else "call_window"
            else:
                call_ok, call_reason = False, "call_unknown"
            contrib = d1 is not None and gap == 1 and not stale and r["spread_dur"] >= MIN_SPREAD_DUR
            inputs[isin] = {"d1": d1, "gap": gap, "moved": moved, "moves": moves, "stale": stale, "contrib": contrib,
                            "jump": False, "cum_jump": False, "issuer": r["issuer_id"],
                            "fine": r["cell_fine"], "wide": r["cell_wide"], "l3": r["cell_l3"], "oas": r["oas"],
                            "spread_dur": r["spread_dur"], "amt": r["amount_outstanding"], "ttm": r["ttm_years"],
                            "call_ok": call_ok, "call_reason": call_reason, "rating": r["rating_bucket"]}
        _flag_jumps(inputs, out, i, start)
        night = {"inputs": inputs}
        _benchmarks(inputs, night, out, i, start)
        out[i] = night
    return out


def _flag_jumps(inputs, out, i, start):
    by_cell = defaultdict(list)                     # keyed (level, id)
    for r in inputs.values():
        if r["contrib"]:
            by_cell[("fine", r["fine"])].append(r["d1"]); by_cell[("wide", r["wide"])].append(r["d1"])
    stats = {}
    for c, v in by_cell.items():
        if len(v) >= JUMP_CELL_MIN:
            med = _median(v); mad = _median([abs(x - med) for x in v]) * MAD_SCALE
            stats[c] = (med, max(JUMP_MAD_MULT * mad, JUMP_MIN_ABS))
    cum_prev = {}
    for isin in inputs:
        s = 0.0
        for j in range(max(start, i - CUM_WINDOW + 1), i):
            v = out[j]["rel1"].get(isin)
            if v is not None:
                s += v
        cum_prev[isin] = s

    def cell_of(r):
        return ("fine", r["fine"]) if ("fine", r["fine"]) in stats else ("wide", r["wide"])

    cum_cell = defaultdict(list)
    for isin, r in inputs.items():
        if not r["contrib"]:
            continue
        c = cell_of(r)
        st = stats.get(c)
        if st is not None:
            r["jump_threshold"] = st[1]
            if abs(r["d1"] - st[0]) > st[1]:
                r["jump"] = True
            cum_cell[c].append(cum_prev[isin] + (r["d1"] - st[0]))
    cum_stats = {}
    for c, v in cum_cell.items():
        if len(v) >= JUMP_CELL_MIN:
            med = _median(v); mad = _median([abs(x - med) for x in v]) * MAD_SCALE
            cum_stats[c] = (med, max(CUM_MAD_MULT * mad, CUM_MIN_ABS))
    for isin, r in inputs.items():
        if not r["contrib"] or r["jump"]:
            continue
        c = cell_of(r)
        st, cs = stats.get(c), cum_stats.get(c)
        if st is not None and cs is not None and abs(cum_prev[isin] + (r["d1"] - st[0]) - cs[0]) > cs[1]:
            r["cum_jump"] = True


def _benchmarks(inputs, night, out, i, start):
    iss_num, iss_den, outer_of = defaultdict(float), defaultdict(float), {}
    for r in inputs.values():
        if r["contrib"] and not r["jump"] and not r["cum_jump"]:
            w = r["amt"] * r["spread_dur"] * r["oas"]
            for lvl in ("fine", "wide", "l3"):
                key = (lvl, r[lvl], r["issuer"])
                iss_num[key] += w * r["d1"]; iss_den[key] += w
            outer_of[("fine", r["fine"], r["issuer"])] = r["wide"]
            outer_of[("wide", r["wide"], r["issuer"])] = r["l3"]
    members = defaultdict(dict)
    for (lvl, cell, iss), num in iss_num.items():
        members[(lvl, cell)][iss] = num / iss_den[(lvl, cell, iss)]
    wins = {}
    for key, m in members.items():
        vals = sorted(m.values()); n = len(vals)
        if n >= 3:
            lo, hi = vals[max(1, int(0.1 * (n - 1)))], vals[min(n - 2, int(math.ceil(0.9 * (n - 1))))]
        else:
            lo, hi = vals[0], vals[-1]
        wins[key] = {iss: min(hi, max(lo, v)) for iss, v in m.items()}
    inner_sum, inner_n = defaultdict(float), defaultdict(int)
    for (lvl, cell), m in members.items():
        if lvl == "l3":
            continue
        outer_lvl = "wide" if lvl == "fine" else "l3"
        for iss in m:
            oc = outer_of[(lvl, cell, iss)]
            ov = wins.get((outer_lvl, oc), {}).get(iss)
            if ov is not None:
                inner_sum[(lvl, cell, outer_lvl, oc)] += ov; inner_n[(lvl, cell, outer_lvl, oc)] += 1

    def loo(lvl, cell, own, excl_lvl=None, excl_cell=None):
        m = members.get((lvl, cell), {}); w = wins.get((lvl, cell), {})
        excl = members.get((excl_lvl, excl_cell), {}) if excl_lvl else {}
        own_counted = own in m and own not in excl
        n_rem = len(m) - (inner_n[(excl_lvl, excl_cell, lvl, cell)] if excl_lvl else 0) - (1 if own_counted else 0)
        if n_rem <= 0:
            return None, 0
        if n_rem <= ROBUST_N:
            return _median([v for iss, v in m.items() if iss != own and iss not in excl]), n_rem
        s = sum(w.values()) - (inner_sum[(excl_lvl, excl_cell, lvl, cell)] if excl_lvl else 0.0)
        if own_counted:
            s -= w[own]
        return s / n_rem, n_rem

    # one count per fine cell: issuers with a benchmark-contributing line in the cell tonight, less one
    n_fine = {r["fine"]: max(0, len(members.get(("fine", r["fine"]), {})) - 1) for r in inputs.values()}
    n_hist = defaultdict(list)
    for j in range(max(start, i - N_MEDIAN_WINDOW), i):
        for c, k in out[j]["n_fine"].items():
            n_hist[c].append(k)

    def bench_for(fine, wide, l3, own):
        """Shrinkage benchmark for a bond in (fine, wide, l3) whose issuer is `own` (None: not a member)."""
        b_f, nf = loo("fine", fine, own)
        b_w, nw = loo("wide", wide, own, "fine", fine)
        b_3, n3 = loo("l3", l3, own, "wide", wide)
        outer, outer_lvl = (b_w, "wide") if nw >= MIN_OUTER else ((b_3, "l3") if n3 >= MIN_OUTER else (None, None))
        n_eff = _median(n_hist[fine] + [n_fine[fine]])     # tonight and the previous 63 nights
        w = 0.0 if n_eff < THIN_CELL_OUTER_ONLY else n_eff / (n_eff + N0_BENCH)
        if outer is not None and b_f is not None and w > 0:
            bench = w * b_f + (1 - w) * outer
        elif outer is not None:
            bench = outer
        elif b_f is not None and nf >= MIN_OUTER:
            bench = b_f
        else:
            bench = None
        return {"bench": bench, "bench_fine": b_f, "bench_outer": outer, "outer_level": outer_lvl, "w_fine": w,
                "n_fine_other": nf, "n_eff": n_eff}

    rel1 = {}
    for isin, r in inputs.items():
        r.update(bench_for(r["fine"], r["wide"], r["l3"], r["issuer"]))
        rel1[isin] = (r["d1"] - r["bench"]) if (r["contrib"] and r["bench"] is not None) else None
    # a bond absent from a night's file still needs that night's benchmark when its next change spans the gap:
    # its issuer was not a member that night, so the benchmark is the cell aggregate with nothing excluded
    night["bench_key"] = {(r["fine"], r["wide"], r["l3"], r["issuer"]): r["bench"] for r in inputs.values()}
    trips = sorted({(r["fine"], r["wide"], r["l3"]) for r in inputs.values()}, key=repr)
    night["bench_cell"] = {trip: bench_for(*trip, None)["bench"] for trip in trips}
    for isin, r in inputs.items():
        if r["d1"] is None or r["gap"] <= 1 or r["bench"] is None:
            continue
        prod, ok = 1.0 + r["bench"], True
        key = (r["fine"], r["wide"], r["l3"], r["issuer"])
        for j in range(1, r["gap"]):
            pn = out.get(i - j, {})
            bj = pn.get("bench_key", {}).get(key)
            if bj is None:
                bj = pn.get("bench_cell", {}).get(key[:3])
            if bj is None:
                ok = False; break
            prod *= 1.0 + bj
        if ok and r["spread_dur"] >= MIN_SPREAD_DUR and r["moves"] >= STALE_MIN_MOVES:
            rel1[isin] = (1.0 + r["d1"]) / prod - 1.0
    night["rel1"] = rel1; night["n_fine"] = n_fine


# ---------------- scoring ----------------

def min_counts(name):
    """The two counts a window needs to score: covered nights (19 of 21, 38 of 42) and single-night changes in
    the volatility period (114 of 126). A bond scores a window when both are met (and the later tests pass);
    diag[isin][W] carries the bond's covered and n_var for every window that reaches the fill test."""
    k, _ = HORIZONS[name]
    return {"covered": math.ceil(MIN_FILL * k - 1e-9), "n_var": math.ceil(MIN_FILL * VOL_WINDOW - 1e-9)}


def earliest_score(name):
    """Pulled nights (counting tonight) at which a bond with a spread on every night of the pull, listed on the
    pull's first night, first scores: the first 20 nights are the stale-mask warm-up, then 114 volatility nights,
    then the window."""
    k, skip = HORIZONS[name]
    return (STALE_WINDOW - 1) + VOL_WINDOW - int((1 - MIN_FILL) * VOL_WINDOW) + k + skip


def full_history(name):
    """Pulled nights at which the volatility period of a bond listed on the pull's first night with a spread on
    every night is fully populated."""
    k, skip = HORIZONS[name]
    return (STALE_WINDOW - 1) + VOL_WINDOW + k + skip


def estimate_z_n0(cell_scales):
    rows = [(math.log(s), n) for s, n in cell_scales if s and s > 0 and n >= Z_MIN_OTHER]
    if len(rows) < 20:
        return Z_N0_INIT
    v = _pvar([x for x, _ in rows]) or 0.0
    tau2 = max(v - sum(Z_SAMPLING_CONST / n for _, n in rows) / len(rows), 1e-4)
    return max(Z_N0_MIN, min(Z_N0_MAX, 2.0 / tau2))


def _tkey(cell, tc):
    return f"{cell}|{tc}"


def spread_terciles(today):
    """{isin: (wide cell, tercile)} for the bonds with a row tonight. Within each wide cell the N bonds are sorted
    by tonight's spread ascending, ties by ISIN ascending; sorted position p (from 0) is tercile 0 when
    p < floor(N/3), 1 when floor(N/3) <= p < floor(2N/3), else 2."""
    terc = {}
    for cell in sorted({r["wide"] for r in today.values()}, key=repr):
        vals = sorted((r["oas"], str(i), i) for i, r in today.items() if r["wide"] == cell)
        n = len(vals)
        for p, (_, _, i) in enumerate(vals):
            terc[i] = (cell, 0 if p < n // 3 else (1 if p < (2 * n) // 3 else 2))
    return terc


def score(hist, query=None, as_of=None, tables=None):
    """
    query: None (every bond), a list of ISINs, or {"issuer": issuer_id}. as_of: a date in hist.dates (default last).
    tables: output of batch() for the month-end before as_of; without them the universe statistics are taken from
    the loaded cells and diag["_global"]["tables"] says "local". Returns (bonds, issuers, diag). Issuer rows are
    returned only for query None and issuer queries, one per issuer with a target line. Every bond record, issuer
    record and diag["_global"] carries "as_of", the scored night (datetime.date).
    """
    tables = tables or {}
    nothing = {"_query": _set_display({"blank": {"all": "not_in_table"}}), "_global": {"as_of": None}}
    if isinstance(query, list) and not query:
        return {}, {}, nothing
    if not hist.dates:
        tg = query if isinstance(query, list) else []
        if not tg:
            return {}, {}, nothing
        return {}, {}, dict({i: _set_display({"blank": {"all": "not_in_table"}}) for i in tg}, _global={"as_of": None})
    as_of = as_of or hist.dates[-1]
    pos = hist.dates.index(as_of)
    need = max(k + s for k, s in HORIZONS.values()) + VOL_WINDOW + STALE_WINDOW + CUM_WINDOW
    start = max(0, pos - need)
    loaded_skips = [(t_, i_, w) for t_, i_, w in hist.skipped if hist.dates[start] <= t_ <= as_of]
    if query is None:
        # every bond with a row (valid or skipped) on some loaded night is a target
        isins = set().union(*(hist.rows[hist.dates[i]].keys() for i in range(start, pos + 1)))
        targets = isins | {i_ for _, i_, _ in loaded_skips}
    else:
        if isinstance(query, dict):
            targets = {i for j in range(start, pos + 1) for i, r in hist.rows[hist.dates[j]].items()
                       if r["issuer_id"] == query.get("issuer")}
            if not targets:
                return {}, {}, nothing
        else:
            targets = set(query)
        isins = _needed_isins(hist, targets, start, pos)
    isins = sorted(isins)
    d = derive(hist, isins, start, pos)
    n_days = pos - start + 1
    series = {isin: [d[start + j]["rel1"].get(isin) for j in range(n_days)] for isin in isins}
    gaps = {isin: [d[start + j]["inputs"].get(isin, {}).get("gap", 1) for j in range(n_days)] for isin in isins}
    first_row = {}
    for j in range(start, pos + 1):
        for isin in d[j]["inputs"]:
            first_row.setdefault(isin, j)
    today = d[pos]["inputs"]

    # autocorrelation per horizon from the variance window, on bonds whose variance window passes the fill test;
    # pooled at issuer level by (wide cell, spread tercile)
    rho_own, rho_n = {h: {} for h in HORIZONS}, {h: {} for h in HORIZONS}
    for name, (k, skip) in HORIZONS.items():
        v_end = n_days - skip - k; v_start = v_end - VOL_WINDOW
        if v_start < 0:
            continue
        for isin in isins:
            if isin in today:
                w = [x if g == 1 else None for x, g in zip(series[isin][v_start:v_end], gaps[isin][v_start:v_end])]
                if sum(1 for x in w if x is not None) < MIN_FILL * VOL_WINDOW:
                    continue
                rho, n = _lag1(w)
                if rho is not None:
                    rho_own[name][isin] = rho; rho_n[name][isin] = n
    terc = spread_terciles(today)
    pool, w_own = {}, {}
    for name in HORIZONS:
        own = rho_own[name]
        if not own:
            # no loaded bond has an own rho in this window: the default is stored, not left empty
            pool[name] = {"tercile": {}, "wide": {}, "index": RHO_DEFAULT}
            w_own[name] = 0.0
            continue
        by, by_wide, allv = defaultdict(lambda: defaultdict(list)), defaultdict(lambda: defaultdict(list)), defaultdict(list)
        for isin, rho in own.items():
            cell, tc = terc.get(isin, (today[isin]["wide"], 1))
            by[_tkey(cell, tc)][today[isin]["issuer"]].append(rho); by_wide[str(cell)][today[isin]["issuer"]].append(rho)
            allv[today[isin]["issuer"]].append(rho)
        iss_obs = [_median(v) for v in allv.values()]
        table = {"tercile": {}, "wide": {}, "index": _median(iss_obs)}
        for part, src in (("tercile", by), ("wide", by_wide)):
            for key, dct in src.items():
                vals = [_median(v) for v in dct.values()]
                if len(vals) >= 5:
                    table[part][key] = _median(vals)
        mt = tables.get("pooled_rho", {}).get(name)
        if mt:
            table["tercile"].update(mt.get("tercile", {})); table["wide"].update(mt.get("wide", {}))
            if mt.get("index") is not None:
                table["index"] = mt["index"]
        pool[name] = table
        se2 = sum(RHO_SE_NUM(table["index"]) / rho_n[name][i] for i in own) / len(own)
        tau2 = max((_pvar(iss_obs) or 0.0) - se2, 0.0)
        wo = tables.get("w_own", {}).get(name)
        w_own[name] = wo if wo is not None else tau2 / (tau2 + se2)

    skipped_tonight = {i: w for t_, i, w in hist.skipped if t_ == as_of}
    allbonds, diag = {}, {}
    for isin in isins:
        r = today.get(isin)
        if r is None:
            continue
        hist_n = pos - first_row[isin] + 1
        dg = {"blank": {}, "reasons": [], "issuer_id": r["issuer"], "history_nights": hist_n,
              "bench_fine": r.get("bench_fine"), "bench_outer": r.get("bench_outer"),
              "outer_level": r.get("outer_level"), "w_fine": r.get("w_fine"), "n_fine_other": r.get("n_fine_other"),
              "moves_21": r["moves"], "jump_today": r["jump"] or r["cum_jump"]}
        # structural conditions; a bond with fewer than 21 loaded nights of its own is a new line, not stale
        if hist_n < STALE_WINDOW:
            dg["reasons"].append("new_line")
        if r["gap"] > 1:
            dg["reasons"].append("multi_night_change")
        elif (r["d1"] is None or r["stale"]) and hist_n >= STALE_WINDOW:
            dg["reasons"].append("not_contributor_today")
        if r["ttm"] < MIN_TTM_YEARS:
            dg["reasons"].append("ttm")
        if not r["call_ok"]:
            dg["reasons"].append(r["call_reason"])
        if r["spread_dur"] < MIN_SPREAD_DUR:
            dg["reasons"].append("spread_dur")
        dg["reasons"].sort(key=REASON_ORDER.index)
        if dg["reasons"]:
            dg["blank"]["all"] = dg["reasons"][0]          # the one bond-level reason; `reasons` lists every condition
        rec = {"issuer_id": r["issuer"], "cell": r["fine"], "wide": r["wide"], "rating": r["rating"],
               "oas": r["oas"], "outer_level": r.get("outer_level"), "history_nights": hist_n, "as_of": as_of}
        s, g = series[isin], gaps[isin]
        for name, (k, skip) in HORIZONS.items():
            rec[f"{name}_score"] = None
            if dg["reasons"]:
                continue
            h_end = n_days - skip; h_start = h_end - k; v_end = h_start; v_start = v_end - VOL_WINDOW
            if v_start < 0:
                dg["blank"][name] = "burn_in"; continue
            # a change spanning g nights counts only when all g nights lie inside the window; it then covers g
            inw = [j for j in range(k) if s[h_start + j] is not None and g[h_start + j] <= j + 1]
            covered = sum(g[h_start + j] for j in inw)
            vv = [x for x, gg in zip(s[v_start:v_end], g[v_start:v_end]) if x is not None and gg == 1]
            dg[name] = {"covered": covered, "n_var": len(vv)}
            fill = "horizon_fill" if covered < MIN_FILL * k else ("vol_fill" if len(vv) < MIN_FILL * VOL_WINDOW else None)
            if fill:
                dg["blank"][name] = "new_line" if hist_n < earliest_score(name) else fill; continue
            var1 = _pvar(vv)
            if not var1:
                dg["blank"][name] = "zero_var"; continue
            own = rho_own[name].get(isin)
            table = pool.get(name, {"tercile": {}, "wide": {}, "index": RHO_DEFAULT})
            cell, tc = terc.get(isin, (r["wide"], 1))
            p = table["tercile"].get(_tkey(cell, tc), table["wide"].get(str(cell), table["index"]))
            rho = max(RHO_FLOOR, w_own[name] * own + (1 - w_own[name]) * p) if own is not None else max(RHO_FLOOR, p)
            bracket = k + 2 * (k - 1) * rho
            if bracket <= BRACKET_MIN_FRAC * k:
                dg["blank"][name] = "variance_bracket"; continue
            sd_k = math.sqrt(var1 * bracket)
            vals = [0.0] * k
            for j in inw:
                vals[j] = s[h_start + j]
            rel_k = sum(vals[j] for j in inw)
            rec[f"{name}_rel"] = -rel_k
            rec[f"{name}_score"] = -rel_k / sd_k
            best = max((abs(sum(vals[j:j + 3])) for j in range(max(1, len(vals) - 2))), default=0.0)
            rec[f"{name}_step_share"] = best / abs(rel_k) if rel_k else None

            def flagged(j):
                st = d[start + j]["inputs"].get(isin) if j >= 0 else None
                return bool(st and (st["jump"] or st["cum_jump"]))
            rec[f"{name}_jump_nights"] = sum(1 for j in range(h_start, h_end) if flagged(j))
            # a jump event is a run of consecutive flagged nights; it counts toward the window in which it starts
            rec[f"{name}_jump_events"] = sum(1 for j in range(h_start, h_end) if flagged(j) and not flagged(j - 1))
            wgt = None
            for j in range(h_start, max(-1, h_start - MAX_GAP_NIGHTS - 1), -1):
                st = d[start + j]["inputs"].get(isin)
                if st:
                    wgt = st["amt"] * st["spread_dur"] * st["oas"]; rec[f"{name}_w_night"] = start + j; break
            rec[f"{name}_w"] = wgt
            dg[name].update({"sd_k": sd_k, "var1": var1, "rho": rho, "rho_own": own, "rho_pool": p,
                             "w_own": w_own.get(name)})
        if any(rec[f"{n}_score"] is not None for n in HORIZONS):
            allbonds[isin] = rec
        if isin in targets:
            diag[isin] = dg
    # targets without a valid row tonight: bad_row (tonight's row failed the checks), no_spread_tonight (a row,
    # valid or not, on some loaded night), not_in_table (no row on any loaded night)
    seen = set(first_row) | {i_ for _, i_, _ in loaded_skips}
    last_iss = {}
    for j in range(start, pos + 1):
        for i_, st in d[j]["inputs"].items():
            last_iss[i_] = st["issuer"]
    for isin in sorted(targets):
        if isin in diag:
            continue
        if isin in skipped_tonight:
            diag[isin] = {"blank": {"all": "bad_row"}, "bad_row_detail": skipped_tonight[isin]}
        else:
            diag[isin] = {"blank": {"all": "no_spread_tonight" if isin in seen else "not_in_table"}}
        if isin in last_iss:
            diag[isin]["issuer_id"] = last_iss[isin]
    bonds = {i: allbonds[i] for i in sorted(targets) if i in allbonds}
    want_issuers = query is None or isinstance(query, dict)
    if want_issuers:
        target_lines = defaultdict(set)
        for i in targets:
            if i in last_iss:
                target_lines[last_iss[i]].add(i)
        zset = {i: r for i, r in allbonds.items() if r["issuer_id"] in target_lines}
    else:
        target_lines, zset = {}, bonds

    # z: centred on the reference cell's median issuer score; scale from the median absolute deviation of the
    # reference issuers' scores about that median, shrunk (variance form) toward the rating-bucket scale
    g = diag.setdefault("_global", {})
    for name in HORIZONS:
        col = f"{name}_score"
        iss_sc, glob = defaultdict(lambda: defaultdict(list)), defaultdict(lambda: defaultdict(list))
        for rec in allbonds.values():
            if rec[col] is not None:
                iss_sc[("fine", rec["cell"])][rec["issuer_id"]].append(rec[col])
                iss_sc[("wide", rec["wide"])][rec["issuer_id"]].append(rec[col])
                glob[str(rec["rating"])][rec["issuer_id"]].append(rec[col])
        # m_rating (informational): the bucket median of issuer values
        m_glob = {rt: _median([_median(v) for v in dd.values()]) for rt, dd in glob.items()}
        # s_rating from within-cell deviations: each issuer of the bucket (first scored line in ISIN order fixes
        # its cell) is compared with its reference cell, the fine cell if it has at least 5 other issuers, else the
        # wide cell if it has at least 5, else the issuer is left out. Deviation = (issuer's median in that cell -
        # median of the other issuers there) / sqrt(1 + 1.571 / n_other), the centre's sampling variance divided
        # out; s_rating = 1.4826 x median |deviation|; n_rating = the number of issuers that entered.
        ref_cell = {}
        for rec in allbonds.values():
            if rec[col] is None or (str(rec["rating"]), rec["issuer_id"]) in ref_cell:
                continue
            ref_cell[(str(rec["rating"]), rec["issuer_id"])] = next(
                (key for key in (("fine", rec["cell"]), ("wide", rec["wide"])) if len(iss_sc[key]) - 1 >= Z_MIN_OTHER), None)
        dev = defaultdict(list)
        for (rt, iss), key in ref_cell.items():
            if key is None:
                continue
            oth = [_median(v) for i2, v in iss_sc[key].items() if i2 != iss]
            dev[rt].append((_median(iss_sc[key][iss]) - _median(oth)) / math.sqrt(1.0 + Z_MEDIAN_VAR / len(oth)))
        s_glob = {rt: _median([abs(x) for x in v]) * MAD_SCALE for rt, v in dev.items()}
        n_glob = {rt: len(v) for rt, v in dev.items()}
        src = {rt: "local" for rt in s_glob}
        if tables.get("rating_scale", {}).get(name):
            s_glob.update(tables["rating_scale"][name])
            m_glob.update(tables.get("rating_centre", {}).get(name, {}))
            n_glob.update(tables.get("rating_n", {}).get(name, {}))
            src.update({rt: "batch" for rt in tables["rating_scale"][name]})
        cell_scale = [(_centre_scale([_median(v) for v in dd.values()])[1], len(dd))
                      for dd in iss_sc.values() if len(dd) >= Z_MIN_OTHER + 1]
        n0 = tables.get("z_n0", {}).get(name) or estimate_z_n0(cell_scale)
        for isin, rec in zset.items():
            z, ref, n_ref, centre, why = None, "none", 0, None, None
            s_g = s_glob.get(str(rec["rating"]))
            if s_g is not None:
                # median-centre correction on the rating-level term, n = issuers that entered s_rating
                s_g = s_g * math.sqrt(1.0 + Z_MEDIAN_VAR / max(1, n_glob.get(str(rec["rating"]), 1)))
            if rec[col] is not None:
                why = "too_few_reference_issuers"
                for key, label in ((("fine", rec["cell"]), "fine"), (("wide", rec["wide"]), "wide")):
                    others = [_median(v) for iss, v in iss_sc.get(key, {}).items() if iss != rec["issuer_id"]]
                    if len(others) >= Z_MIN_OTHER:
                        m_c, s_c = _centre_scale(others)
                        scale = math.sqrt((len(others) * s_c ** 2 + n0 * s_g ** 2) / (len(others) + n0)) if s_g is not None else 0.0
                        # m_cell is a median of len(others) issuer values: its sampling variance, about
                        # Z_MEDIAN_VAR / n in scale units, is added so that z keeps unit variance
                        scale *= math.sqrt(1.0 + Z_MEDIAN_VAR / len(others))
                        if scale > 0:
                            z, ref, n_ref, centre, why = (rec[col] - m_c) / scale, label, len(others), m_c, None
                        else:
                            why = "zero_var"
                        break
                if why and isin in diag:
                    diag[isin]["blank"][name] = why
            rec[f"{name}_z"] = z; rec[f"{name}_z_ref"] = ref; rec[f"{name}_z_n_ref"] = n_ref
            rec[f"{name}_z_centre"] = centre
            rec[f"{name}_z_disp"] = None if z is None else max(-Z_CLIP, min(Z_CLIP, z))
            rec[f"{name}_conf"] = _confidence(rec, name) if z is not None else None
        g[f"{name}_z_n0"] = n0
        g.setdefault("rating_scale", {})[name] = s_glob
        g.setdefault("rating_centre", {})[name] = m_glob
        g.setdefault("rating_n", {})[name] = n_glob
        g.setdefault("rating_scale_source", {})[name] = src
        g.setdefault("pooled_rho", {})[name] = pool.get(name, {})
        g.setdefault("w_own", {})[name] = w_own.get(name)
        g.setdefault("min_counts", {})[name] = min_counts(name)
    g["as_of"] = as_of
    g["tables"] = "batch" if tables else "local"
    g["skipped"] = list(hist.skipped)
    for isin in sorted(targets):
        dg = diag.get(isin)
        if dg is None:
            continue
        _set_display(dg)
        if isin in bonds:
            for name in HORIZONS:
                bonds[isin][f"{name}_blank"] = dg["display_reason"][name]

    # issuer roll-up (issuer queries and the batch only): one row per issuer with a target line, scored or not
    issuers, by_iss = {}, defaultdict(list)
    for rec in zset.values():
        by_iss[rec["issuer_id"]].append(rec)
    for iss in sorted(target_lines, key=repr):
        rows = by_iss.get(iss, [])
        out = {"as_of": as_of, "n_bonds": len(rows), "n_cells": len({r["cell"] for r in rows}),
               "n_lines": len(target_lines[iss])}
        for name, (k, skip) in HORIZONS.items():
            pairs = [(r[f"{name}_z_disp"], r[f"{name}_score"], r[f"{name}_w"], r[f"{name}_conf"])
                     for r in rows if r.get(f"{name}_z_disp") is not None and r.get(f"{name}_w")]
            out[f"{name}_n_dropped_no_weight"] = sum(1 for r in rows if r.get(f"{name}_score") is not None and not r.get(f"{name}_w"))
            wsum = sum(w for _, _, w, _ in pairs)
            if wsum <= 0:
                _blank_issuer_window(out, name); continue
            out[f"{name}_blank"] = None
            out[f"{name}_z"] = sum(z * w for z, _, w, _ in pairs) / wsum
            out[f"{name}_raw"] = sum(sc * w for _, sc, w, _ in pairs) / wsum
            out[f"{name}_n"] = len(pairs)
            out[f"{name}_max_w"] = max(w for _, _, w, _ in pairs) / wsum
            raws = [sc for _, sc, _, _ in pairs]
            out[f"{name}_pct_pos"] = sum(1 for x in raws if x > 0) / len(raws)
            out[f"{name}_sd"] = math.sqrt(_pvar(raws)) if len(raws) >= 2 else None
            out[f"{name}_conf"] = max((c for _, _, _, c in pairs), key=lambda c: "ABC".index(c))   # worst line
            # coverage denominator: every line of the issuer with a row on the window-start night or up to 3 nights
            # before, each weighted at its nearest such night (the numerator's rule)
            h0 = pos + 1 - skip - k
            lo = max(start, h0 - MAX_GAP_NIGHTS)
            tot = 0.0
            for x in sorted({x for j in range(lo, h0 + 1) for x, st in d[j]["inputs"].items() if st["issuer"] == iss}):
                for j in range(h0, lo - 1, -1):
                    st = d[j]["inputs"].get(x)
                    if st:
                        tot += st["amt"] * st["spread_dur"] * st["oas"]; break
            out[f"{name}_coverage"] = wsum / tot if tot > 0 else None
        issuers[iss] = out
    return bonds, issuers, diag


def _confidence(rec, name):
    """C: 2 or more jump events starting in the window, or the largest 3-night move is 80% or more of the window
    move. A: no jump event, largest 3-night move under 60%, fine-cell reference with at least 8 other issuers.
    B otherwise. The step test applies only when max(|raw score|, |z|) >= CONF_STEP_MIN_SCORE; below that the
    window move is too small for its concentration to mean anything and the test is treated as passed."""
    events = rec.get(f"{name}_jump_events") or 0
    step = rec.get(f"{name}_step_share") or 0.0
    if max(abs(rec.get(f"{name}_score") or 0.0), abs(rec.get(f"{name}_z") or 0.0)) < CONF_STEP_MIN_SCORE:
        step = 0.0
    ref, n_ref = rec.get(f"{name}_z_ref"), rec.get(f"{name}_z_n_ref") or 0
    if events >= CONF_JUMP_EVENTS_C or step >= CONF_STEP_SHARE_C:
        return "C"
    if events == 0 and step < CONF_STEP_SHARE_A and ref == "fine" and n_ref >= CONF_REF_A:
        return "A"
    return "B"


def batch(current, fetch):
    """request(current, fetch, None): every bond in the current table, with the same diag taxonomy and issuer
    rows, returning also the tables single requests use."""
    bonds, issuers, diag = request(current, fetch, None)
    return bonds, issuers, diag, _tables(diag)


def batch_from_history(hist, as_of=None):
    """score(hist, None, as_of) plus the tables. Returns (bonds, issuers, diag, tables)."""
    bonds, issuers, diag = score(hist, None, as_of)
    return bonds, issuers, diag, _tables(diag)


def _tables(diag):
    """tables is JSON-serialisable as it stands: pooled rho tercile keys are the strings "<wide cell>|<tercile>",
    wide-cell and rating keys are str(id), as_of is an ISO date string."""
    g = diag.get("_global", {})
    a = g.get("as_of")
    return {"pooled_rho": g.get("pooled_rho", {}), "rating_scale": g.get("rating_scale", {}),
            "rating_centre": g.get("rating_centre", {}), "rating_n": g.get("rating_n", {}), "w_own": g.get("w_own", {}),
            "z_n0": {h: g.get(f"{h}_z_n0") for h in HORIZONS},
            "as_of": a.isoformat() if a else None, "method_version": METHOD_VERSION}
