"""Tests for oasmom.py. Every test asserts. Run: python3 test_oasmom.py   (add --runtime for the 7,000 bond timing)"""
import json, math, os, random, subprocess, sys, time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import oasmom as m


def universe(n_days, cohorts, seed=1):
    """cohorts: [(sector, subsector, n_issuers[, bonds_per_issuer])]. log OAS = market RW + issuer RW + iid noise.
    Returns {date: {isin: row}} in the daily-file format."""
    r = random.Random(seed)
    dates, d = [], date(2025, 1, 2)
    while len(dates) < n_days:
        if d.weekday() < 5:
            dates.append(d)
        d += timedelta(days=1)
    mkt = [0.0]
    for _ in range(n_days - 1):
        mkt.append(mkt[-1] + r.gauss(0, 0.006))
    series, static = {}, {}
    for sec, sub, n_iss, *bpi in cohorts:
        bpi = bpi[0] if bpi else 1
        for i in range(n_iss):
            lvl = [0.0]
            for _ in range(n_days - 1):
                lvl.append(lvl[-1] + r.gauss(0, 0.004))
            for b in range(bpi):
                isin = f"{sub}_I{i}_B{b}"
                series[isin] = [100 * math.exp(mkt[t] + lvl[t] + r.gauss(0, 0.0045)) for t in range(n_days)]
                static[isin] = dict(issuer_id=f"{sub}_I{i}", spread_dur=6.0 + b, ttm_years=7.0, amount_outstanding=500 + 250 * b,
                                    call_status="bullet", rating_bucket="BBB",
                                    cell_fine=f"{sec}|{sub}|bullet|SR|BBB|5-7", cell_wide=f"{sec}|bullet|SR|BBB|5-7", cell_l3="bullet|SR|BBB|5-7")
    return dates, series, static


def files_from(dates, series, static, mutate_row=None):
    files = {}
    for i, t in enumerate(dates):
        rows = {}
        for isin, st in static.items():
            v = series[isin][i]
            if v is None:
                continue
            row = dict(st, oas=v)
            if mutate_row:
                row = mutate_row(i, isin, row)
                if row is None:
                    continue
            rows[isin] = row
        files[t] = rows
    return files


def load(n_days, cohorts, seed=1, mutate=None, mutate_row=None):
    dates, series, static = universe(n_days, cohorts, seed)
    if mutate:
        mutate(dates, series, static)
    return m.History(files_from(dates, series, static, mutate_row)), dates, series, static


def fetch_of(dates, series):
    return lambda isins: {i: {dates[t]: series[i][t] for t in range(len(dates)) if series[i][t] is not None}
                          for i in isins if i in series}


def scored(bonds, name):
    return sum(1 for r in bonds.values() if r.get(f"{name}_score") is not None)


COLS = ("m21_score", "m21_z", "m21_conf", "m63x_score", "m63x_z", "m63x_conf")


def same(a, b, cols=COLS, tol=0.0):
    for c in cols:
        x, y = a.get(c), b.get(c)
        if isinstance(x, float) or isinstance(y, float):
            assert x is not None and y is not None and abs(x - y) <= tol, (c, x, y)
        else:
            assert x == y, (c, x, y)


# ---------------- history, rows, calls ----------------

def t_history_boundaries():
    assert (m.earliest_score("m21"), m.earliest_score("m63x")) == (155, 197)
    assert (m.full_history("m21"), m.full_history("m63x")) == (167, 209)
    assert set(m.HORIZONS) == {"m21", "m63x"}
    for name in ("m21", "m63x"):
        e, f = m.earliest_score(name), m.full_history(name)
        h0, *_ = load(e - 1, [("IND", "CHEM", 8)]); b0, _, d0 = m.score(h0)
        h1, *_ = load(e, [("IND", "CHEM", 8)]); b1, _, d1 = m.score(h1)
        h2, *_ = load(f, [("IND", "CHEM", 8)]); b2, _, d2 = m.score(h2)
        assert scored(b0, name) == 0 and scored(b1, name) == 8 and scored(b2, name) == 8, name
        assert d1["CHEM_I0_B0"][name]["n_var"] == 114 and d2["CHEM_I0_B0"][name]["n_var"] == 126, name
        assert not any(k.startswith("m10") for r in b2.values() for k in r)
        assert d0["CHEM_I0_B0"].get(name, {}).get("n_var", 0) < 114 or d0["CHEM_I0_B0"]["blank"].get(name) == "burn_in"
    assert m.min_counts("m21") == {"covered": 19, "n_var": 114} and m.min_counts("m63x") == {"covered": 38, "n_var": 114}
    # T1: a bond first listed part-way through a 215-night pull scores once its counts are met: Mom21 at 145 nights
    # of its own history (144 shows new_line), Mom63x at 187 (186 shows new_line)
    first = {}
    for name in ("m21", "m63x"):
        for own in range(m.earliest_score(name) - 12, m.earliest_score(name)):
            def mut(dates, series, static, own=own):
                for t in range(len(dates) - own):
                    series["CHEM_I0_B0"][t] = None
            h, *_ = load(215, [("IND", "CHEM", 10)], seed=4, mutate=mut)
            b, _, dg = m.score(h, ["CHEM_I0_B0"])
            x = dg["CHEM_I0_B0"]
            if b.get("CHEM_I0_B0", {}).get(f"{name}_score") is not None:
                assert x[name]["n_var"] >= 114 and x[name]["covered"] >= m.min_counts(name)["covered"]
                first[name] = own; break
            assert x["display_reason"][name] == "new_line", (name, own, x["display_reason"])
    assert first == {"m21": 145, "m63x": 187}, first
    g = dg["_global"]["min_counts"]
    assert g["m21"]["n_var"] == 114 and g["m63x"]["covered"] == 38
    print("history boundaries: earliest", m.earliest_score("m21"), m.earliest_score("m63x"), "full", m.full_history("m21"), m.full_history("m63x"),
          "| listed part-way through a 215-night pull: first Mom21 at", first["m21"], "own nights, Mom63x at", first["m63x"])


def t_bad_rows_skipped():
    def mr(i, isin, row):
        if isin == "CHEM_I0_B0" and i == 100:
            row["oas"] = -3.0
        if isin == "CHEM_I1_B0" and i == 120:
            row["oas"] = float("nan")
        if isin == "CHEM_I2_B0" and i == 150:
            row["spread_dur"] = float("nan")
        if isin == "CHEM_I3_B0" and i == 160:
            del row["cell_fine"]
        return row
    h, *_ = load(230, [("IND", "CHEM", 8)], mutate_row=mr)
    reasons = [w for _, _, w in h.skipped]
    assert len(h.skipped) == 4 and any("implausible" in w for w in reasons) and sum("non-finite" in w for w in reasons) == 2 and any("missing" in w for w in reasons)
    b, _, dg = m.score(h)
    assert scored(b, "m21") == 8 and len(dg["_global"]["skipped"]) == 4
    print("bad rows skipped with reasons; everything else scores")


def t_missing_nights():
    def mut(dates, series, static):
        for k in series:
            series[k][-3] = None; series[k][-25] = None
    h, *_ = load(230, [("IND", "CHEM", 8)], mutate=mut)
    b, _, _ = m.score(h)
    assert scored(b, "m21") == 8
    print("two all-missing nights: full coverage")


def t_gap_netting_zero_on_market_night():
    n = 60
    dates = [date(2025, 1, 2) + timedelta(days=i) for i in range(n)]
    r = random.Random(1)
    series, static = {}, {}
    for i in range(10):
        isin = f"X_I{i}_B0"
        s = [(100.0 + i) * (1.05 if t >= 40 else 1.0) * (1 + r.gauss(0, 1e-6)) for t in range(n)]
        series[isin] = s
        static[isin] = dict(issuer_id=f"X_I{i}", spread_dur=6.0, ttm_years=7.0, amount_outstanding=500, call_status="bullet",
                            rating_bucket="BBB", cell_fine="F", cell_wide="W", cell_l3="L")
    series["X_I0_B0"][40] = None
    h = m.History(files_from(dates, series, static))
    d = m.derive(h, set(static), 0, n - 1)
    r41 = d[41]["rel1"]["X_I0_B0"]
    assert r41 is not None and abs(r41) < 1e-3 and d[41]["inputs"]["X_I0_B0"]["gap"] == 2
    print(f"gap netting on a +5% market night: catch-up rel_1 = {r41:+.1e}")


def t_call_window():
    def mr(i, isin, row):
        if isin == "CHEM_I0_B0":
            row.update(call_status="callable", next_call_date=date(2025, 6, 7))
        if isin == "CHEM_I1_B0":
            row.update(call_status="callable", next_call_date=date(2024, 1, 1))
        if isin == "CHEM_I2_B0":
            row.update(call_status="callable", next_call_date=date(2030, 1, 1))
        if isin == "CHEM_I3_B0":
            row.update(call_status="unknown")
        return row
    h, dates, *_ = load(200, [("IND", "CHEM", 8)], mutate_row=mr)
    as_of = [t for t in dates if t < date(2025, 6, 7)][-10]
    _, _, dg = m.score(h, as_of=as_of)
    assert "call_window" in dg["CHEM_I0_B0"]["reasons"]
    assert "call_unknown" in dg["CHEM_I1_B0"]["reasons"] and "call_unknown" in dg["CHEM_I3_B0"]["reasons"]
    assert not dg["CHEM_I2_B0"]["reasons"]
    # S-32 edges, measured from t: no date and a past date are call_unknown; t+91 is call_window; t+92 passes;
    # a bullet's date is ignored
    def mut2(dates, series, static):
        t = dates[-1]
        static["CHEM_I1_B0"].update(call_status="callable")
        static["CHEM_I2_B0"].update(call_status="callable", next_call_date=t - timedelta(days=5))
        static["CHEM_I3_B0"].update(call_status="callable", next_call_date=t + timedelta(days=91))
        static["CHEM_I4_B0"].update(call_status="callable", next_call_date=t + timedelta(days=92))
        static["CHEM_I5_B0"].update(call_status="bullet", next_call_date=t + timedelta(days=10))
    h2, *_ = load(230, [("IND", "CHEM", 8), ("IND", "PAPER", 12)], seed=5, mutate=mut2)
    b2, _, d2 = m.score(h2, [f"CHEM_I{i}_B0" for i in range(1, 6)])
    assert d2["CHEM_I1_B0"]["reasons"] == ["call_unknown"] and d2["CHEM_I2_B0"]["reasons"] == ["call_unknown"]
    assert d2["CHEM_I3_B0"]["reasons"] == ["call_window"] and "CHEM_I4_B0" in b2 and "CHEM_I5_B0" in b2
    print("call window: inside excluded, past and unknown excluded, day 91 excluded, day 92 and far call scored")


def t_datetime_validation():
    dates, series, static = universe(60, [("IND", "CHEM", 6)], seed=1)
    current = {k: dict(v) for k, v in static.items()}
    bad = lambda isins: {i: {datetime(t.year, t.month, t.day): series[i][j] for j, t in enumerate(dates)} for i in isins}
    try:
        m.request(current, bad, ["CHEM_I0_B0"]); raise AssertionError("datetime keys accepted")
    except TypeError as e:
        assert "datetime.date" in str(e)
    current["CHEM_I1_B0"].update(call_status="callable", next_call_date=datetime(2030, 1, 1))
    _, _, dg = m.request(current, fetch_of(dates, series), ["CHEM_I1_B0"])
    assert dg["CHEM_I1_B0"]["blank"]["all"] == "bad_row" and "datetime.date" in dg["CHEM_I1_B0"]["bad_row_detail"]
    print("datetime fetch keys raise TypeError; a datetime next_call_date is a bad_row")


# ---------------- benchmarks, flags, floor ----------------

def t_floor():
    def mut(dates, series, static):
        series["CHEM_I0_B0"] = [0.05 * v for v in series["CHEM_I0_B0"]]          # a 5bp line in a 100bp cell
    h, dates, series, _ = load(60, [("IND", "CHEM", 10)], mutate=mut)
    d = m.derive(h, set(h.rows[dates[-1]]), 0, 59)
    med = m._median([h.rows[dates[59]][k]["oas"] for k in h.rows[dates[59]]])
    floor = min(m.MAX_OAS_FLOOR, max(m.MIN_OAS_ABS, m.FLOOR_FRAC * med))
    s = series["CHEM_I0_B0"]
    assert 30 < floor <= 40 and abs(d[59]["inputs"]["CHEM_I0_B0"]["d1"] - (s[59] - s[58]) / floor) < 1e-15
    print(f"floor: a 5bp line's change is divided by the {floor:.1f}bp floor")


def t_stale_mask():
    def mut(dates, series, static):
        v = series["CHEM_I0_B0"][-16]
        for t in range(-15, 0):
            series["CHEM_I0_B0"][t] = v                                          # 15 frozen nights: 6 moves of 21
    h, *_ = load(230, [("IND", "CHEM", 10)], seed=3, mutate=mut)
    b, _, dg = m.score(h)
    x = dg["CHEM_I0_B0"]
    assert x["reasons"] == ["not_contributor_today"] and x["moves_21"] == 6 and "CHEM_I0_B0" not in b
    assert m.display_row("CHEM_I0_B0", None, 3.0, x)["Reading"] == "stale marks (fewer than 10 changes in the last 21 nights)"
    # CHOICE 3: jump flags are tested only for contributors; a stale line that re-marks +40% tonight is not flagged
    def mut2(dates, series, static):
        mut(dates, series, static); series["CHEM_I0_B0"][-1] *= 1.40
    h2, dates2, *_ = load(230, [("IND", "CHEM", 10)], seed=3, mutate=mut2)
    d2 = m.derive(h2, set(h2.rows[dates2[-1]]), 200, 229)
    x2 = d2[229]["inputs"]["CHEM_I0_B0"]
    assert not x2["contrib"] and not x2["jump"] and not x2["cum_jump"] and abs(x2["d1"]) > 0.3
    print("stale mask: 15 frozen nights blank the line with the stale-marks text; a stale line is never jump-tested")


def t_jump_one_night():
    base, *_ = load(230, [("IND", "CHEM", 5), ("IND", "PAPER", 12)], seed=31); b0, _, _ = m.score(base)
    def mut(dates, series, static):
        for t in range(-15, 0):
            series["CHEM_I0_B0"][t] *= 1.40
    h, dates, *_ = load(230, [("IND", "CHEM", 5), ("IND", "PAPER", 12)], seed=31, mutate=mut); b1, _, _ = m.score(h)
    worst = max(abs(b0[k]["m21_score"] - b1[k]["m21_score"]) for k in b0 if k.startswith("CHEM") and k != "CHEM_I0_B0")
    x = b1["CHEM_I0_B0"]
    d = m.derive(h, sorted(h.rows[dates[-1]]), len(dates) - 60, len(dates) - 1)
    assert d[len(dates) - 15]["inputs"]["CHEM_I0_B0"]["jump"]
    assert worst < 0.5 and x["m21_score"] < -5 and x["m21_conf"] == "C" and x["m21_jump_events"] == 1 and x["m21_jump_nights"] >= 2
    print(f"one-night +40% re-mark: max peer shift {worst:.2f}; one jump event ({x['m21_jump_nights']} flagged nights); graded C by the step test")


def t_cum_jump():
    def mut(dates, series, static):
        f = 1.0
        for t in range(-5, 0):
            f *= 1.40 ** 0.2; series["CHEM_I0_B0"][t] *= f
    h, dates, *_ = load(230, [("IND", "CHEM", 12)], seed=11, mutate=mut)
    n = len(dates)
    d = m.derive(h, sorted(h.rows[dates[-1]]), n - 30, n - 1)
    flags = [(d[i]["inputs"]["CHEM_I0_B0"]["jump"], d[i]["inputs"]["CHEM_I0_B0"]["cum_jump"]) for i in range(n - 5, n)]
    assert not any(j for j, _ in flags) and any(c for _, c in flags), flags
    b, _, _ = m.score(h)
    assert b["CHEM_I0_B0"]["m21_jump_events"] == 1
    print(f"phased +40% re-mark over 5 nights: no one-night flag, cumulative flag {[int(c) for _, c in flags]}, one event")


def t_jump_phased_thin_cell():
    base, *_ = load(230, [("IND", "CHEM", 3), ("IND", "PAPER", 12)], seed=11); b0, _, _ = m.score(base)
    def mut(dates, series, static):
        f = 1.0
        for t in range(-5, 0):
            f *= 1.40 ** 0.2; series["CHEM_I0_B0"][t] *= f
    h, *_ = load(230, [("IND", "CHEM", 3), ("IND", "PAPER", 12)], seed=11, mutate=mut); b1, _, _ = m.score(h)
    worst = max(abs(b0[k]["m21_score"] - b1[k]["m21_score"]) for k in b0 if k.startswith("CHEM") and k != "CHEM_I0_B0")
    assert worst < 0.5
    print(f"phased +40% re-mark in 3-issuer cell: max peer shift {worst:.2f}")


def t_jump_tonight_scored():
    def mut(dates, series, static):
        series["CHEM_I0_B0"][-1] *= 1.40
    h, *_ = load(230, [("IND", "CHEM", 8), ("IND", "PAPER", 12)], seed=5, mutate=mut)
    b, _, dg = m.score(h, ["CHEM_I0_B0"])
    x = b["CHEM_I0_B0"]
    assert dg["CHEM_I0_B0"]["jump_today"] and dg["CHEM_I0_B0"]["reasons"] == [] and x["m21_score"] is not None
    assert x["m21_jump_events"] == 1 and x["m21_conf"] == "C"
    print(f"jump tonight: scored ({x['m21_z_disp']:+.1f}), one event, grade {x['m21_conf']}")


def t_jump_events_by_window():
    def run(j, mult):
        def mut(dates, series, static):
            for t in range(-(j + 1), 0):
                series["CHEM_I0_B0"][t] *= mult
        h, *_ = load(230, [("IND", "CHEM", 12)], seed=4, mutate=mut)
        return m.score(h)[0]["CHEM_I0_B0"]
    x15, x22, x0 = run(15, 1.25), run(22, 1.25), run(0, 1.0)
    assert x15["m21_jump_events"] == 1 and x15["m21_jump_nights"] > 1 and x15["m63x_jump_events"] == 0
    assert x22["m21_jump_events"] == 0 and x22["m21_jump_nights"] > 0 and x22["m63x_jump_events"] == 1
    assert x22["m21_conf"] == x0["m21_conf"] == "A" and x22["m63x_conf"] == "C"
    print(f"jump events: re-mark at t-15 is one Mom21 event; at t-22 one Mom63x event, Conf21 {x22['m21_conf']} (flagged echo nights in Mom21: {x22['m21_jump_nights']})")


def t_subsector_move_not_flagged():
    def mut(dates, series, static):
        for k in series:
            if k.startswith("CHEM"):
                series[k][-1] *= 1.15
    h, dates, *_ = load(60, [("IND", "CHEM", 8), ("IND", "PAPER", 8)], mutate=mut)
    d = m.derive(h, set(h.rows[dates[-1]]), 0, 59)
    assert sum(1 for k, r in d[59]["inputs"].items() if k.startswith("CHEM") and (r["jump"] or r["cum_jump"])) == 0
    print("whole subsector +15%: no jump flags")


def t_scale_invariance():
    h1, *_ = load(230, [("IND", "CHEM", 8)]); b1, _, _ = m.score(h1)
    h2, *_ = load(230, [("IND", "CHEM", 8)], mutate=lambda d, s, st: [s.__setitem__(k, [v * 0.5 for v in s[k]]) for k in s]); b2, _, _ = m.score(h2)
    assert max(abs(b1[k]["m21_score"] - b2[k]["m21_score"]) for k in b1) < 1e-12
    print("scale invariance holds")


def t_shrinkage_weight_and_outer():
    h, *_ = load(230, [("IND", "CHEM", 10), ("IND", "RAIL", 3), ("FIN", "BANK", 12)], seed=4)
    b, _, dg = m.score(h)
    assert abs(dg["CHEM_I0_B0"]["w_fine"] - 9 / 14) < 1e-15 and dg["BANK_I0_B0"]["w_fine"] == 11 / 16
    assert dg["RAIL_I0_B0"]["w_fine"] == 0.0                                     # 2 others: below 4, outer only
    assert dg["CHEM_I0_B0"]["outer_level"] == "l3" and dg["RAIL_I0_B0"]["outer_level"] == "wide"
    # O-1: one count per fine cell. A frozen line in a 5-issuer cell leaves 4 contributing issuers, so c = 3 for
    # every bond of the cell, including the frozen one, and n = 3 < 4 gives w = 0
    def frozen(dates, series, static):
        series["CHEM_I4_B0"] = [series["CHEM_I4_B0"][0]] * len(dates)
    h2, dates2, *_ = load(230, [("IND", "CHEM", 5), ("IND", "PAPER", 12)], seed=2, mutate=frozen)
    d2 = m.derive(h2, set(h2.rows[dates2[-1]]), 14, 229)
    assert d2[229]["n_fine"]["IND|CHEM|bullet|SR|BBB|5-7"] == 3
    _, _, dg2 = m.score(h2)
    assert dg2["CHEM_I0_B0"]["w_fine"] == 0.0 and dg2["CHEM_I4_B0"]["w_fine"] == 0.0
    print("shrinkage weight n/(n+5) = 9/14; thin cell outer only; outer level falls to level 3 below 6 others; frozen line: c = 3, w = 0")


def t_slow_reference_benchmark():
    worst = 0.0
    for seed in range(5):
        h, dates, *_ = load(40, [("IND", "CHEM", 3 + seed, 2), ("IND", "PAPER", 10), ("IND", "RAIL", 2, 3), ("FIN", "BANK", 12, 2), ("FIN", "INS", 4)], seed=seed)
        d = m.derive(h, set(h.rows[dates[-1]]), 0, 39)
        inputs = d[39]["inputs"]
        iss = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0]))
        for r in inputs.values():
            if r["contrib"] and not r["jump"] and not r["cum_jump"]:
                w = r["amt"] * r["spread_dur"] * r["oas"]
                for lvl in ("fine", "wide", "l3"):
                    iss[(lvl, r[lvl])][r["issuer"]][0] += w * r["d1"]; iss[(lvl, r[lvl])][r["issuer"]][1] += w
        chg = {key: {i: a / b for i, (a, b) in dd.items()} for key, dd in iss.items()}
        def agg(vals, full):
            if len(vals) <= m.ROBUST_N:
                return m._median(vals)
            srt = sorted(full); n = len(srt)
            lo, hi = srt[max(1, int(0.1 * (n - 1)))], srt[min(n - 2, int(math.ceil(0.9 * (n - 1))))]
            return sum(min(hi, max(lo, v)) for v in vals) / len(vals)
        for isin, r in inputs.items():
            own = r["issuer"]
            f = chg.get(("fine", r["fine"]), {}); wd = chg.get(("wide", r["wide"]), {}); l3 = chg.get(("l3", r["l3"]), {})
            bf = [v for i, v in f.items() if i != own]; bw = [v for i, v in wd.items() if i != own and i not in f]; b3 = [v for i, v in l3.items() if i != own and i not in wd]
            if bf and r["bench_fine"] is not None:
                worst = max(worst, abs(agg(bf, list(f.values())) - r["bench_fine"]))
            exp_w = agg(bw, list(wd.values())) if bw else None; exp_3 = agg(b3, list(l3.values())) if b3 else None
            outer = exp_w if len(bw) >= m.MIN_OUTER else (exp_3 if len(b3) >= m.MIN_OUTER else None)
            if outer is not None and r["bench_outer"] is not None:
                worst = max(worst, abs(outer - r["bench_outer"]))
    assert worst < 1e-12, worst
    print(f"fast vs slow benchmark: max |diff| {worst:.1e}")


def t_rho_alignment():
    h, *_ = load(230, [("IND", "CHEM", 8)]); _, _, dg = m.score(h)
    x = dg["CHEM_I0_B0"]
    assert x["m21"]["n_var"] == 126 and x["m63x"]["n_var"] == 126 and x["m21"]["rho"] != x["m63x"]["rho"]
    print("rho per horizon from the matching window")


# ---------------- windows, blanks, display ----------------

def t_window_coverage_rule():
    T_ = lambda j: -(j + 1)                                                      # index of night t-j
    def run(missing):
        def mut(dates, series, static):
            for t in missing:
                series["CHEM_I0_B0"][t] = None
        h, dates, *_ = load(230, [("IND", "CHEM", 10)], seed=4, mutate=mut)
        b, _, dg = m.score(h)
        return h, dates, b, dg["CHEM_I0_B0"]
    _, _, _, x = run([T_(21), T_(20), T_(19)])                                   # t-18 has no change (4 nights)
    assert x["blank"]["m21"] == "horizon_fill"
    h, dates, b, x = run([T_(22), T_(21)])                                       # t-20 spans t-22..t-20: dropped
    assert x["m21"]["covered"] == 20 and b["CHEM_I0_B0"]["m21_score"] is not None
    pos = len(dates) - 1
    d = m.derive(h, sorted(set().union(*(h.rows[t].keys() for t in dates))), pos - 215, pos)
    rk = sum(d[i]["rel1"]["CHEM_I0_B0"] for i in range(pos - 19, pos + 1))
    assert abs(-b["CHEM_I0_B0"]["m21_rel"] - rk) < 1e-15
    _, _, _, x = run([T_(22), T_(21), T_(10), T_(9), T_(8)])                     # 17 in-window nights covered
    assert x["blank"]["m21"] == "horizon_fill"
    print("window coverage: a change spanning nights before the window is dropped; 17 of 21 blanks")


def t_blank_reasons():
    def mut(dates, series, static):
        static["CHEM_I1_B0"].update(ttm_years=0.999, spread_dur=0.999)
        for t in range(len(dates) - 100):
            series["CHEM_I2_B0"][t] = None                                       # a new line: 100 nights
        for t in (-11, -10, -9, -8, -7):
            series["CHEM_I3_B0"][t] = None                                       # Mom21 window under 90%
        for t in range(len(dates) - 5):
            series["CHEM_I4_B0"][t] = None                                       # 5 nights old
        series["CHEM_I5_B0"][-2] = None                                          # 2-night change tonight
        for t in range(len(dates) - 15):
            series["CHEM_I6_B0"][t] = None                                       # 15 nights old, 2-night change tonight
        series["CHEM_I6_B0"][-2] = None
    h, dates, *_ = load(230, [("IND", "CHEM", 14)], seed=4, mutate=mut)
    b, _, dg = m.score(h, ["CHEM_I1_B0", "CHEM_I2_B0", "CHEM_I3_B0", "CHEM_I4_B0", "CHEM_I5_B0", "CHEM_I6_B0", "NOPE"])
    # G-5: under 21 loaded nights of its own a bond is a new line, not stale; new_line outranks multi_night_change
    x4 = dg["CHEM_I4_B0"]
    assert x4["blank"]["all"] == "new_line" and x4["history_nights"] == 5 and "not_contributor_today" not in x4["reasons"]
    assert m.display_row("CHEM_I4_B0", None, 0.0, x4)["Reading"] == "new line (5 nights of history)"
    assert dg["CHEM_I5_B0"]["blank"]["all"] == "multi_night_change"
    assert dg["CHEM_I6_B0"]["reasons"] == ["new_line", "multi_night_change"] and dg["CHEM_I6_B0"]["display_reason"] == {"m21": "new_line", "m63x": "new_line"}
    assert m.REASON_ORDER[:6] == ("not_in_table", "no_spread_tonight", "bad_row", "new_line", "multi_night_change", "not_contributor_today")
    # G-6: one bond-level reason (blank["all"]) and one reason per window; display_reason has every window
    for k, x in dg.items():
        if k.startswith("_"):
            continue
        assert set(x["display_reason"]) == set(m.HORIZONS) and not any(key.endswith("_z") for key in x["blank"]), (k, x["blank"])
        for name in m.HORIZONS:
            assert x["display_reason"][name] == (x["blank"].get("all") or x["blank"].get(name)), (k, name)
        if x.get("reasons"):
            assert x["blank"]["all"] == x["reasons"][0]
    assert dg["CHEM_I1_B0"]["reasons"] == ["ttm", "spread_dur"]
    assert m.display_row("CHEM_I1_B0", None, 0.0, dg["CHEM_I1_B0"])["Reading"] == "maturity under 1 year"
    x2 = dg["CHEM_I2_B0"]
    assert x2["blank"] == {"m21": "new_line", "m63x": "new_line"} and x2["history_nights"] == 100
    assert m.display_row("CHEM_I2_B0", None, 0.0, x2)["Reading"] == "new line (100 nights of history)"
    r3 = b["CHEM_I3_B0"]
    assert r3["m21_score"] is None and r3["m63x_z"] is not None and r3["m21_blank"] == "horizon_fill" and r3["m63x_blank"] is None
    row = m.display_row("CHEM_I3_B0", r3, 12.0)
    assert row["Reading"] == "window not covered" and row["Trend"] is None and row["Mom63x"] == r3["m63x_z_disp"]
    assert m.display_row("NOPE", None, None, dg["NOPE"])["Reading"] == "not in the table"
    assert set(m.REASON_ORDER) == set(m.REASON_TEXT)
    print("blank reasons: precedence, new_line with its night count, display text in Reading")


def t_reading_trend_grid():
    gaps = ((5.0, "cheap"), (12.0, "cheap"), (4.999999, "fair"), (0.0, "fair"), (-4.999999, "fair"), (-5.0, "rich"), (-30.0, "rich"))
    moves = ((1.0, "outperforming", "up"), (2.7, "outperforming", "up"), (0.999999, "flat", "flat"), (0.0, "flat", "flat"),
             (-0.999999, "flat", "flat"), (-1.0, "underperforming", "down"), (-3.0, "underperforming", "down"))
    labels = set()
    for g, gc in gaps:
        for z, word, _ in moves:
            assert m.reading(g, z) == f"{gc} and {word}", (g, z)
            labels.add(m.reading(g, z))
    assert labels == {f"{a} and {b}" for a in ("cheap", "rich", "fair") for b in ("outperforming", "underperforming", "flat")}
    assert m.reading(None, 2.0) is None and m.reading(10.0, None) is None
    table = {("up", "up"): "continuing", ("down", "down"): "continuing", ("up", "down"): "reversing", ("down", "up"): "reversing",
             ("flat", "up"): "new", ("flat", "down"): "new", ("up", "flat"): "faded", ("down", "flat"): "faded", ("flat", "flat"): "flat"}
    for z63, _, c63 in moves:
        for z21, _, c21 in moves:
            assert m.trend(z63, z21) == table[(c63, c21)], (z63, z21)
        assert m.trend(z63, None) is None and m.trend(None, z63) is None
    assert m.READ_GAP_BP == 5.0 and m.READ_MOVE == 1.0
    print("reading and trend: 49 x 2 grid points and boundaries at +-5.0 and +-1.0")


def t_display_rows():
    h, *_ = load(230, [("IND", "CHEM", 10, 2)], seed=6)
    b, iss, _ = m.score(h)
    k = "CHEM_I0_B0"; r = b[k]
    row = m.display_row(k, r, 7.5)
    assert list(row) == ["ISIN", "Issuer", "Gap", "Mom21", "Conf21", "Mom63x", "Conf63x", "Reading", "Trend"]
    assert row["Mom21"] == r["m21_z_disp"] and row["Reading"] == m.reading(7.5, r["m21_z_disp"]) and row["Trend"] == m.trend(r["m63x_z_disp"], r["m21_z_disp"])
    assert m.display_row(k, r, None)["Reading"] is None
    irow = m.issuer_display_row("CHEM_I0", iss["CHEM_I0"], -6.0)
    assert list(irow)[:8] == ["Issuer", "Gap", "Mom21", "Conf21", "Mom63x", "Conf63x", "Reading", "Trend"]
    assert irow["Reading"] == m.reading(-6.0, iss["CHEM_I0"]["m21_z"]) and irow["n"] == 2
    print("display rows: column order, Reading and Trend from unrounded values")


def t_issuer_rows_unscored():
    """G-2: an issuer in the table with no scored line gets a row with the window fields None and no_scored_line,
    on the batch, an issuer request and score(); its display reads "no line scored"."""
    dates, series, static = universe(230, [("IND", "CHEM", 10, 2), ("IND", "PAPER", 12)], seed=6)
    for t in range(len(dates) - 100):
        series["CHEM_I2_B0"][t] = series["CHEM_I2_B1"][t] = None                 # new issuer: both lines 100 nights
    series["PAPER_I1_B0"][-1] = None                                             # only line: no spread tonight
    static["CHEM_I3_B1"]["spread_dur"] = 0.5                                     # one line scored, one not
    current = {k: dict(v) for k, v in static.items()}
    current["GHOST_B0"] = dict(current["PAPER_I2_B0"], issuer_id="GHOST")      # in the table, nothing pulled
    f = fetch_of(dates, series)
    bb, ii, dd, tables = m.batch(current, f)
    _, iis, _ = m.score(m.History(files_from(dates, series, static)))
    for iss, n_lines in (("CHEM_I2", 2), ("PAPER_I1", 1)):
        _, ir, _ = m.request(current, f, {"issuer": iss}, tables=tables)
        for rows in (ii, ir, iis):
            rec = rows[iss]
            assert rec["n_bonds"] == 0 and rec["n_lines"] == n_lines, (iss, rec)
            for name in m.HORIZONS:
                assert rec[f"{name}_n"] == 0 and rec[f"{name}_blank"] == "no_scored_line"
                assert all(rec[f"{name}_{fld}"] is None for fld in ("z", "raw", "conf", "max_w", "pct_pos", "sd", "coverage"))
            assert m.issuer_display_row(iss, rec, 0.0)["Reading"] == "no line scored"
    assert ii["CHEM_I3"]["m21_n"] == 1 and ii["CHEM_I3"]["m21_blank"] is None and ii["CHEM_I3"]["n_lines"] == 2
    assert set(ii) == {r["issuer_id"] for r in current.values()}
    assert ii["GHOST"]["n_lines"] == 1 and ii["GHOST"]["m21_blank"] == "no_scored_line" and dd["GHOST_B0"]["blank"]["all"] == "no_spread_tonight"
    print("issuer rows: an issuer with no scored line has a row with blank fields and reads 'no line scored'")


# ---------------- standardisation and grade ----------------

def t_fallback_z():
    h, *_ = load(230, [("IND", "CHEM", 8), ("IND", "RAIL", 2)], seed=5)
    b, _, _ = m.score(h)
    for k in b:
        if k.startswith("RAIL"):
            assert b[k]["m21_z"] is not None and b[k]["m21_z_ref"] == "wide"
    # G-6: a window with a score and no z carries its z reason under blank[W], the window's one reason
    h2, *_ = load(230, [("IND", "CHEM", 4), ("FIN", "BANK", 8)], seed=5)               # CHEM outer: level 3
    b2, _, d2 = m.score(h2)
    r, x = b2["CHEM_I0_B0"], d2["CHEM_I0_B0"]
    assert r["m21_score"] is not None and r["m21_z"] is None
    assert x["blank"] == {"m21": "too_few_reference_issuers", "m63x": "too_few_reference_issuers"}
    assert x["display_reason"] == x["blank"] and r["m21_blank"] == "too_few_reference_issuers"
    print("thin subsector: z referenced to wide cell; a 4-issuer sector has scores and no z, reason under blank[W]")


def t_centring_hand():
    # CHOICE 1: every median is the middle value, or the mean of the two middle values for an even count
    assert m._median([4.0, 1.0, 3.0, 2.0]) == 2.5 and m._median([3, 1, 2]) == 2 and m._median([]) is None
    # RAIL (3 issuers) enters s_rating through its wide cell; BANK (4 issuers, own wide cell) does not enter
    h, *_ = load(230, [("IND", "CHEM", 9, 2), ("IND", "PAPER", 12), ("IND", "RAIL", 3), ("FIN", "BANK", 4)], seed=8)
    b, _, dg = m.score(h)
    g = dg["_global"]
    sec = lambda i: "FIN" if i.startswith("BANK") else "IND"
    for name in ("m21", "m63x"):
        per_iss = defaultdict(list)
        for r in b.values():
            per_iss[r["issuer_id"]].append(r[f"{name}_score"])
        meds = {i: m._median(v) for i, v in per_iss.items()}
        allm = list(meds.values()); mr = m._median(allm)
        # G-4: s_rating from each issuer's deviation from its own fine cell's leave-own-out centre, the centre's
        # sampling variance divided out (both cells have at least 5 other issuers here)
        devs = []
        for i, v in meds.items():
            oth = [w for j, w in meds.items() if j != i and j.split("_")[0] == i.split("_")[0]]
            if len(oth) < 5:
                oth = [w for j, w in meds.items() if j != i and sec(j) == sec(i)]
            if len(oth) >= 5:
                devs.append((v - m._median(oth)) / math.sqrt(1 + 1.571 / len(oth)))
        s_g = m._median([abs(x) for x in devs]) * m.MAD_SCALE
        assert abs(g["rating_scale"][name]["BBB"] - s_g) < 1e-15 and abs(g["rating_centre"][name]["BBB"] - mr) < 1e-15
        assert g["rating_n"][name]["BBB"] == len(devs) == 24 and len(allm) == 28 and m.Z_MEDIAN_VAR == 1.571
        n0 = g[f"{name}_z_n0"]
        for k in ("CHEM_I0_B1", "PAPER_I3_B0"):
            r = b[k]
            others = [v for i, v in meds.items() if i != r["issuer_id"] and i.split("_")[0] == k.split("_")[0]]
            assert r[f"{name}_z_ref"] == "fine"
            mc = m._median(others); sc = m._median([abs(v - mc) for v in others]) * m.MAD_SCALE; n = len(others)
            sg = s_g * math.sqrt(1 + 1.571 / len(devs))                         # rating term, issuers that entered
            z = (r[f"{name}_score"] - mc) / (math.sqrt((n * sc ** 2 + n0 * sg ** 2) / (n + n0)) * math.sqrt(1 + 1.571 / n))
            assert abs(r[f"{name}_z"] - z) < 1e-12 and abs(r[f"{name}_z_centre"] - mc) < 1e-15 and r[f"{name}_z_n_ref"] == n
    assert g["m21_z_n0"] == m.Z_N0_INIT                                          # fewer than 20 cells
    print("z = (score - m_cell) / scale reproduced by hand; m_cell stored as _z_centre; n0_z 12 below 20 cells")


def t_z_n0_estimate():
    r = random.Random(3)
    cells = [(math.exp(r.gauss(0, 0.3)), 6 + i % 7) for i in range(25)]
    v = m._pvar([math.log(s) for s, _ in cells]); tau2 = max(v - sum(1.3 / n for _, n in cells) / 25, 1e-4)
    assert abs(m.estimate_z_n0(cells) - max(5, min(60, 2 / tau2))) < 1e-12 and m.estimate_z_n0(cells[:19]) == 12
    print(f"n0_z estimate {m.estimate_z_n0(cells):.2f} matches the formula; 12 below 20 cells")


def t_z_clip():
    h, *_ = load(230, [("IND", "CHEM", 10, 2), ("IND", "PAPER", 12)], seed=2)
    b, _, _ = m.score(h)
    zs = [(r["m21_z"], r["m21_z_disp"]) for r in b.values()] + [(r["m63x_z"], r["m63x_z_disp"]) for r in b.values()]
    assert all(zd == max(-3.0, min(3.0, z)) for z, zd in zs)
    assert sum(1 for z, _ in zs if 1.0 < abs(z) < 3.0) >= 3
    print("z_disp = clip(z, -3, 3)")


def t_rotation_centred():
    """F-2: a whole fine cell widening 15% over the window shows near zero within the cell; neighbours stay near zero."""
    def mut(dates, series, static):
        for k in series:
            if k.startswith("CHEM"):
                for j, t in enumerate(range(-21, 0)):
                    series[k][t] *= (1 + 0.15 * (j + 1) / 21)
    out = []
    for n_cell in (6, 20):
        cohorts = [("IND", "CHEM", n_cell, 2), ("IND", "PAPER", 15, 2), ("IND", "RAIL", 10, 2)]
        h, *_ = load(230, cohorts, seed=3, mutate=mut)
        b, _, _ = m.score(h)
        zc = [b[k]["m21_z_disp"] for k in b if k.startswith("CHEM")]
        zp = [b[k]["m21_z_disp"] for k in b if k.startswith("PAPER")]
        raw = m._median([b[k]["m21_score"] for k in b if k.startswith("CHEM")])
        under = sum(1 for z in zc if m.reading(0.0, z) == "fair and underperforming") / len(zc)
        assert raw < -1.5 and abs(m._median(zc)) < 0.25 and abs(m._median(zp)) < 0.25 and under <= 0.5, (n_cell, m._median(zc), under)
        out.append(f"{n_cell}-issuer cell: raw median {raw:+.2f}, z median {m._median(zc):+.2f}, Gap-neutral lines read underperforming {under:.0%}")
    # G-4: 8 cells of 10 issuers in one bucket, 0 or 4 of them widening: the rating scale stays put and the
    # unshocked lines keep their move share
    res = {}
    for n_shock in (0, 4):
        def mut8(dates, series, static, n_shock=n_shock):
            for k in series:
                if any(k.startswith(f"SUB{c}_") for c in range(n_shock)):
                    for j, t in enumerate(range(-21, 0)):
                        series[k][t] *= (1 + 0.15 * (j + 1) / 21)
        h, *_ = load(230, [("IND", f"SUB{c}", 10, 2) for c in range(8)], seed=3, mutate=mut8)
        b, _, dg = m.score(h)
        calm = [r["m21_z_disp"] for k, r in b.items() if int(k[3]) >= n_shock and r.get("m21_z_disp") is not None]
        res[n_shock] = (dg["_global"]["rating_scale"]["m21"]["BBB"], sum(abs(z) >= 1.0 for z in calm) / len(calm))
    assert abs(res[4][0] / res[0][0] - 1) < 0.10 and abs(res[4][1] - res[0][1]) < 0.05, res
    out.append(f"rating scale 0 vs 4 of 8 cells shocked {res[0][0]:.3f} vs {res[4][0]:.3f}, unshocked move share {res[0][1]:.3f} vs {res[4][1]:.3f}")
    print("rotation: " + "; ".join(out))


def t_confidence_rules():
    base = {"x_score": 2.0, "x_z": 2.0, "x_step_share": 0.5, "x_jump_events": 0, "x_z_ref": "fine", "x_z_n_ref": 8}
    c = lambda **kw: m._confidence({**base, **{f"x_{k}": v for k, v in kw.items()}}, "x")
    assert c() == "A" and c(z_n_ref=7) == "B" and c(z_ref="wide", z_n_ref=30) == "B"
    assert c(step_share=0.6) == "B" and c(step_share=0.79) == "B" and c(step_share=0.8) == "C"
    assert c(jump_events=1) == "B" and c(jump_events=2) == "C"
    assert c(score=1.5, z=1.9, step_share=0.95) == "A"                         # gate: both below 2.0
    assert c(score=1.0, z=2.0, step_share=0.95) == "C" and c(score=-2.0, z=0.4, step_share=0.95) == "C"
    assert c(score=1.0, z=-2.1, step_share=0.7) == "B"
    print("grade rules: step gate on max(|raw|, |z|) >= 2.0, events, reference depth")


def t_step_gate_on_display():
    """O-7 / F-3: one 9% night under the jump floor, calm last month after a volatile half year."""
    r = random.Random(2)
    n_days, calm_last, vm = 230, 21, 2.5
    dates, dd = [], date(2025, 1, 2)
    while len(dates) < n_days:
        if dd.weekday() < 5:
            dates.append(dd)
        dd += timedelta(days=1)
    mkt = [0.0]
    for _ in range(n_days - 1):
        mkt.append(mkt[-1] + r.gauss(0, 0.006))
    series, static = {}, {}
    for sub, n in (("CHEM", 12), ("PAPER", 12)):
        for i in range(n):
            lvl = [0.0]
            for t in range(n_days - 1):
                lvl.append(lvl[-1] + r.gauss(0, 0.004 * (vm if t < n_days - 1 - calm_last else 1.0)))
            isin = f"{sub}_I{i}_B0"
            series[isin] = [100 * math.exp(mkt[t] + lvl[t] + r.gauss(0, 0.0045 * (vm if t < n_days - calm_last else 1.0))) for t in range(n_days)]
            static[isin] = dict(issuer_id=f"{sub}_I{i}", spread_dur=6.0, ttm_years=7.0, amount_outstanding=500, call_status="bullet",
                                rating_bucket="BBB", cell_fine=f"IND|{sub}", cell_wide="IND", cell_l3="L3")
    for t in range(-5, 0):
        series["CHEM_I0_B0"][t] *= 1.09
    b, _, _ = m.score(m.History(files_from(dates, series, static)))
    x = b["CHEM_I0_B0"]
    assert abs(x["m21_score"]) < 2.0 and abs(x["m21_z"]) >= 2.0 and x["m21_step_share"] >= 0.8 and x["m21_jump_events"] == 0
    assert x["m21_conf"] in ("B", "C")
    print(f"step gate on the displayed value: raw {x['m21_score']:+.2f}, z {x['m21_z']:+.2f}, step share {x['m21_step_share']:.2f}, grade {x['m21_conf']}")


# ---------------- requests, determinism ----------------

def t_query_equals_batch():
    h, *_ = load(230, [("IND", "CHEM", 10, 2), ("IND", "PAPER", 12), ("FIN", "BANK", 12, 2)], seed=4)
    bb, ii, _, tables = m.batch_from_history(h)
    b1, i1, _ = m.score(h, ["CHEM_I0_B0"], tables=tables)
    b2, i2, _ = m.score(h, {"issuer": "BANK_I3"}, tables=tables)
    bl, _, dl = m.score(h, ["CHEM_I0_B0"])
    same(b1["CHEM_I0_B0"], bb["CHEM_I0_B0"])
    for k in b2:
        same(b2[k], bb[k])
    same(i2["BANK_I3"], ii["BANK_I3"], ("m21_z", "m21_raw", "m21_coverage", "m21_conf", "m63x_z", "m63x_coverage"))
    assert i1 == {} and dl["_global"]["tables"] == "local" and bl["CHEM_I0_B0"]["m21_score"] == bb["CHEM_I0_B0"]["m21_score"]
    print("bond and issuer requests with month-end tables equal the batch; a bond request returns no issuer row")


def t_request_from_current_and_pull():
    """request(current, fetch) with a fake Bloomberg pull reproduces scoring from assembled files."""
    dates, series, static = universe(230, [("IND", "CHEM", 10, 2), ("IND", "PAPER", 12), ("FIN", "BANK", 12, 2)], seed=4)
    for k, st in static.items():
        if k.startswith("BANK"):
            st.update(cell_fine="FIN|BANK|bullet|SUB|BBB|5-7", cell_wide="FIN|bullet|SUB|BBB|5-7", cell_l3="bullet|SUB|BBB|5-7")
    current = {isin: dict(st) for isin, st in static.items()}
    pulled_for = []
    def fetch(isins):
        pulled_for.append(set(isins))
        return fetch_of(dates, series)(isins)
    bb, ii, _, tables = m.batch(current, fetch)
    b1, i1, d1 = m.request(current, fetch, ["CHEM_I0_B0"], tables=tables)
    b2, i2, _ = m.request(current, fetch, {"issuer": "BANK_I3"}, tables=tables)
    same(b1["CHEM_I0_B0"], bb["CHEM_I0_B0"])
    same(i2["BANK_I3"], ii["BANK_I3"], ("m21_z", "m21_coverage", "m63x_z"))
    assert pulled_for[1] == {k for k in current if not k.startswith("BANK")}
    print(f"request from current table plus pull equals batch; one-bond request pulled {len(pulled_for[1])} of {len(current)} bonds (its three cells)")


def t_request_equals_batch_hard_cases():
    """O-1, O-2, O-3: frozen line in a 5-issuer cell, peer missing tonight, sibling needing level 3, outer
    contributors dipping below 6."""
    cases = []
    d, s, st = universe(230, [("IND", "CHEM", 5), ("IND", "PAPER", 12), ("FIN", "BANK", 12)], seed=2)
    s["CHEM_I4_B0"] = [s["CHEM_I4_B0"][0]] * len(d)
    cases.append(("frozen line", d, s, st, "CHEM_I0_B0"))
    d, s, st = universe(230, [("IND", "CHEM", 10), ("IND", "PAPER", 12)], seed=4)
    s["CHEM_I5_B0"][-1] = None
    cases.append(("peer missing tonight", d, s, st, "CHEM_I0_B0"))
    d, s, st = universe(230, [("IND", "RAIL", 3), ("IND", "CHEM", 12), ("FIN", "BANK", 12)], seed=7)
    cases.append(("sibling needs level 3", d, s, st, "RAIL_I0_B0"))
    d, s, st = universe(230, [("IND", "CHEM", 10), ("IND", "PAPER", 8), ("FIN", "BANK", 12)], seed=3)
    for i in range(3):
        st[f"PAPER_I{i}_B0"]["spread_dur"] = 0.8
    cases.append(("outer contributors below 6", d, s, st, "CHEM_I0_B0"))
    for lab, dates, series, static, tgt in cases:
        current = {k: dict(v) for k, v in static.items()}
        f = fetch_of(dates, series)
        bb, ii, _, tables = m.batch(current, f)
        b1, _, _ = m.request(current, f, [tgt], tables=tables)
        same(b1[tgt], bb[tgt], COLS + ("outer_level",))
        b2, i2, _ = m.request(current, f, {"issuer": static[tgt]["issuer_id"]}, tables=tables)
        same(i2[static[tgt]["issuer_id"]], ii[static[tgt]["issuer_id"]], ("m21_z", "m21_coverage", "m63x_z"))
    print("request equals batch: frozen line, peer missing tonight, sibling cell on level 3, outer dip below 6")


HASH_SNIPPET = r'''
import sys, json
sys.path.insert(0, %r)
import oasmom as m, test_oasmom as T
dates, series, static = T.universe(230, [("IND", "CHEM", 5), ("IND", "PAPER", 12), ("FIN", "BANK", 12)], seed=2)
series["CHEM_I4_B0"] = [series["CHEM_I4_B0"][0]] * len(dates)
series["PAPER_I3_B0"][-1] = None
static["CHEM_I2_B0"]["spread_dur"] = 0.9
current = {k: dict(v) for k, v in static.items()}
f = T.fetch_of(dates, series)
bb, ii, _, tables = m.batch(current, f)
b1, _, _ = m.request(current, f, ["CHEM_I0_B0", "CHEM_I1_B0"], tables=tables)
b2, i2, _ = m.request(current, f, {"issuer": "CHEM_I3"}, tables=tables)
dump = lambda d: repr(sorted((k, sorted((a, repr(b)) for a, b in v.items())) for k, v in d.items()))
for x in (bb, ii, b1, b2, i2):
    print(dump(x))
print(json.dumps(tables, sort_keys=True))
'''


def t_hash_seed_determinism():
    outs = []
    for seed in ("0", "1", "3"):
        env = dict(os.environ, PYTHONHASHSEED=seed)
        p = subprocess.run([sys.executable, "-c", HASH_SNIPPET % HERE], env=env, capture_output=True, text=True, timeout=600)
        assert p.returncode == 0, p.stderr
        outs.append(p.stdout)
    assert outs[0] == outs[1] == outs[2] and len(outs[0]) > 1000
    print(f"PYTHONHASHSEED 0, 1, 3: identical batch, request and tables output ({len(outs[0])} chars)")


def t_stray_date():
    dates, series, static = universe(230, [("IND", "CHEM", 10), ("IND", "PAPER", 12)], seed=4)
    current = {k: dict(v) for k, v in static.items()}
    base = fetch_of(dates, series)
    bb, _, _, tables = m.batch(current, base)
    extra = dates[-1] + timedelta(days=1)
    def partial(isins):
        out = base(isins); out["PAPER_I3_B0"][extra] = series["PAPER_I3_B0"][-1] * 1.001; return out
    def nan(isins):
        out = base(isins)
        for i in out:
            out[i][extra] = float("nan")
        return out
    sat = next(t + timedelta(days=1) for t in dates[100:] if t.weekday() == 4)
    def weekend(isins):
        out = base(isins); out["CHEM_I4_B0"][sat] = 99.0; return out
    for f in (partial, nan, weekend):
        b, _, _ = m.request(current, f, ["CHEM_I0_B0", "CHEM_I1_B0"], tables=tables)
        for k in ("CHEM_I0_B0", "CHEM_I1_B0"):
            same(b[k], bb[k])
    b, _, _, _ = m.batch(current, partial)
    assert len(b) == len(current)
    # the majority rule is inclusive: a date printed by exactly half of the pulled bonds is a trading night
    half = sorted(current)[:len(current) // 2]
    def halfp(isins):
        out = base(isins)
        for k in half:
            if k in out:
                out[k][extra] = series[k][-1] * 1.001
        return out
    _, _, dh = m.request(current, halfp, ["CHEM_I0_B0"])
    rn = dh["_global"]["request_night"]
    assert dh["_global"]["as_of"] == extra and rn == {"as_of": extra, "n_pulled": 22, "n_any": 22, "n_spread": 11}, rn
    # a pulled ISIN that is not in the current table counts nowhere (n_any stays 22, the half date stays)
    def withx(isins):
        out = halfp(isins); out["XNOTCUR"] = {t: 100.0 for t in dates}; return out
    hx = m.build_history(current, withx, set(current) | {"XNOTCUR"})
    assert hx.dates[-1] == extra and hx.pull["n_any"] == 22 and hx.pull["n_pulled"] == 23
    print("stray dates (one partial print, a NaN night for all, a weekend value): request night unchanged, results equal; "
          "exactly half printed is a trading night; a pulled ISIN outside the table counts nowhere")


def t_unknown_and_empty():
    dates, series, static = universe(230, [("IND", "CHEM", 10)], seed=4)
    series["CHEM_I1_B0"][-1] = None
    static["CHEM_I2_B0"]["call_status"] = "make_whole"
    current = {k: dict(v) for k, v in static.items()}
    f = fetch_of(dates, series)
    for q, key in ((["US0000NOPE00"], "US0000NOPE00"), ({"issuer": "NO_SUCH_PARENT"}, "_query"), ([], "_query")):
        b, i, dg = m.request(current, f, q)
        tg = {k: v for k, v in dg.items() if k != "_global"}
        assert b == {} and i == {} and list(tg) == [key] and tg[key]["blank"]["all"] == "not_in_table", (q, dg)
        assert tg[key]["display_reason"] == {"m21": "not_in_table", "m63x": "not_in_table"}
    b, i, dg = m.request(current, lambda isins: {}, ["CHEM_I0_B0"])
    assert b == {} and dg["CHEM_I0_B0"]["blank"]["all"] == "no_spread_tonight" and dg["_global"]["as_of"] is None
    b, _, dg = m.request(current, f, ["CHEM_I1_B0", "CHEM_I2_B0", "NOT_A_BOND", "CHEM_I0_B0"])
    assert dg["CHEM_I1_B0"]["blank"]["all"] == "no_spread_tonight" and dg["CHEM_I2_B0"]["blank"]["all"] == "bad_row"
    assert dg["NOT_A_BOND"]["blank"]["all"] == "not_in_table" and "CHEM_I0_B0" in b
    assert m.score(m.History({}), ["X"])[2]["X"]["blank"]["all"] == "not_in_table"
    # G-1, O-13: the same taxonomy on every path. CHEM_X_B0 is in the table but its only spreads fall on
    # Saturdays (not trading nights); CHEM_I1_B0 has no spread tonight; CHEM_I2_B0's rows all fail the checks
    cur = dict(current, CHEM_X_B0=dict(current["CHEM_I3_B0"], issuer_id="CHEM_X"))
    sats = [t + timedelta(days=1) for t in dates if t.weekday() == 4][-30:]
    def fx(isins):
        out = f(isins)
        if "CHEM_X_B0" in isins:
            out["CHEM_X_B0"] = {d: 100.0 + j for j, d in enumerate(sats)}
        return out
    want = {"CHEM_X_B0": "no_spread_tonight", "CHEM_I1_B0": "no_spread_tonight", "CHEM_I2_B0": "bad_row"}
    _, _, d_req = m.request(cur, fx, sorted(want))
    _, _, d_iss = m.request(cur, fx, {"issuer": "CHEM_X"})
    bb, ib, d_bat, _ = m.batch(cur, fx)
    _, _, d_all = m.request(cur, fx, None)
    for lab, dd in (("list", d_req), ("batch", d_bat), ("request None", d_all)):
        for k, code in want.items():
            assert dd[k]["blank"]["all"] == code and dd[k]["display_reason"] == {"m21": code, "m63x": code}, (lab, k, dd[k])
    assert d_iss["CHEM_X_B0"]["blank"]["all"] == "no_spread_tonight" and "_query" not in d_iss
    assert m.display_row("CHEM_I1_B0", bb.get("CHEM_I1_B0"), 0.0, d_bat["CHEM_I1_B0"])["Reading"] == "no spread tonight"
    h_fx = m.History(files_from(dates, series, static))                                  # score() on a bare history
    _, _, d_h = m.score(h_fx)
    assert d_h["CHEM_I1_B0"]["blank"]["all"] == "no_spread_tonight" and d_h["CHEM_I2_B0"]["blank"]["all"] == "bad_row"
    print("unknown ISIN, unknown issuer, empty query, empty pull: empty results with reasons, no exception; "
          "no_spread_tonight and bad_row on the list, issuer, whole-index request and batch paths")


def t_as_of():
    """G-3: every record and diag["_global"] name the scored night; request() reports the pull behind it."""
    dates, series, static = universe(230, [("IND", "CHEM", 10, 2), ("IND", "PAPER", 12)], seed=4)
    current = {k: dict(v) for k, v in static.items()}
    f = fetch_of(dates, series)
    series2 = {k: list(v) for k, v in series.items()}
    for k in sorted(series2)[:5]:
        series2[k][-1] = None
    f2 = fetch_of(dates, series2)
    for q in (None, ["CHEM_I0_B0"], {"issuer": "CHEM_I1"}):
        b, i, dg = m.request(current, f2, q)
        assert all(r["as_of"] == dates[-1] for r in b.values()) and all(r["as_of"] == dates[-1] for r in i.values())
        assert dg["_global"]["as_of"] == dates[-1] and type(dg["_global"]["as_of"]) is date
        assert dg["_global"]["request_night"] == {"as_of": dates[-1], "n_pulled": 32, "n_any": 32, "n_spread": 27}, dg["_global"]["request_night"]
    bb, ii, dd, tables = m.batch(current, f)
    assert dd["_global"]["as_of"] == dates[-1] and tables["as_of"] == dates[-1].isoformat()
    b1, _, d1 = m.score(m.History(files_from(dates, series, static)), as_of=dates[-30])
    assert d1["_global"]["as_of"] == dates[-30] and all(r["as_of"] == dates[-30] for r in b1.values())
    print(f"as_of: every bond, issuer and _global carries the scored night; request_night counts {dg['_global']['request_night']['n_spread']} of 32 with a spread")


def t_terciles_and_default_rho():
    """CHOICE 4: spread ascending, ties by ISIN ascending, boundaries at floor(N/3) and floor(2N/3).
    D1: a window with no own rho stores the default pooled rho and w_own 0.0."""
    mk = lambda oas: {"wide": "W", "oas": oas}
    today = {f"B{j:02d}": mk(100.0 + (j // 2)) for j in range(7)}                 # ties in pairs
    t7 = m.spread_terciles(today)
    assert [t7[f"B{j:02d}"][1] for j in range(7)] == [0, 0, 1, 1, 2, 2, 2]
    rev = {f"B{j:02d}": mk(200.0 - j) for j in range(20)}                        # descending spread by name
    t20 = m.spread_terciles(rev)
    order = sorted(rev, key=lambda k: rev[k]["oas"])
    assert [t20[k][1] for k in order] == [0] * 6 + [1] * 7 + [2] * 7
    tie = m.spread_terciles({"Z1": mk(50.0), "A1": mk(50.0), "M1": mk(50.0)})
    assert (tie["A1"][1], tie["M1"][1], tie["Z1"][1]) == (0, 1, 2)
    # CHOICE 5: an issuer's median for a tercile key covers only its lines in that wide cell and tercile
    def wide_apart(dates, series, static):
        for k in series:
            if k.endswith("_B1"):
                series[k] = [1.6 * v for v in series[k]]
    hp, dp, *_ = load(230, [("IND", "CHEM", 12, 2), ("IND", "PAPER", 12, 2)], seed=3, mutate=wide_apart)
    bp, _, dgp = m.score(hp)
    tonight = {k: {"wide": r["cell_wide"], "oas": r["oas"]} for k, r in hp.rows[dp[-1]].items()}
    tp = m.spread_terciles(tonight)
    by = defaultdict(lambda: defaultdict(list))
    for k in bp:
        ro = dgp[k]["m21"]["rho_own"]
        if ro is not None:
            by[f"{tp[k][0]}|{tp[k][1]}"][bp[k]["issuer_id"]].append(ro)
    split = [i for i in {r["issuer_id"] for r in bp.values()} if len({tp[k][1] for k in bp if bp[k]["issuer_id"] == i}) > 1]
    hand = {key: m._median([m._median(v) for v in dd.values()]) for key, dd in by.items() if len(dd) >= 5}
    got = dgp["_global"]["pooled_rho"]["m21"]["tercile"]
    assert split and set(hand) == set(got) and all(abs(hand[k] - got[k]) < 1e-15 for k in hand), (split, hand, got)
    h, *_ = load(170, [("IND", "CHEM", 8)], seed=2)                             # Mom63x volatility period not loaded
    b, _, dg = m.score(h)
    g = dg["_global"]
    assert g["pooled_rho"]["m63x"] == {"tercile": {}, "wide": {}, "index": -0.3} and g["w_own"]["m63x"] == 0.0
    assert g["w_own"]["m21"] is not None and scored(b, "m21") == 8
    print("terciles: 7 bonds split 2/2/3, 20 split 6/7/7, ties by ISIN; no own rho: pooled rho -0.3 and w_own 0.0 stored")


def t_rollup():
    h, *_ = load(230, [("IND", "CHEM", 8, 3)], seed=2); b, i, _ = m.score(h)
    for iss, rec in i.items():
        assert rec["m21_n"] == 3 and abs(rec["m21_coverage"] - 1.0) < 1e-9 and rec["m21_n_dropped_no_weight"] == 0
        assert rec["m21_conf"] in "ABC" and rec["m63x_conf"] in "ABC"
    assert all(r["m21_conf"] in "ABC" and r["m63x_conf"] in "ABC" for r in b.values())
    print("roll-up: coverage 1.0, no silent drops, grades per window")


def t_coverage_at_most_one():
    def mut(dates, series, static):
        series["CHEM_I0_B2"][-21] = None                                         # no mark on the Mom21 start night
    h, *_ = load(230, [("IND", "CHEM", 10, 3)], seed=2, mutate=mut)
    _, iss, _ = m.score(h)
    assert abs(iss["CHEM_I0"]["m21_coverage"] - 1.0) < 1e-12 and all(r["m21_coverage"] <= 1 + 1e-12 for r in iss.values())
    def mut2(dates, series, static):
        series["CHEM_I0_B1"][-21] = None
        static["CHEM_I0_B1"]["spread_dur"] = 0.5                                 # in the table, never scored
    h2, dates, series, static = load(230, [("IND", "CHEM", 10, 2)], seed=2, mutate=mut2)
    _, iss2, _ = m.score(h2)
    w0 = 500 * 6.0 * series["CHEM_I0_B0"][-21]; w1 = 750 * 0.5 * series["CHEM_I0_B1"][-22]
    assert iss2["CHEM_I0"]["m21_n"] == 1 and abs(iss2["CHEM_I0"]["m21_coverage"] - w0 / (w0 + w1)) < 1e-12
    print(f"coverage: 1.0 with a line missing on the start night; unscored line counted at its 3-night weight ({iss2['CHEM_I0']['m21_coverage']:.3f})")


def t_tables_json_and_ids():
    dates, series, static = universe(230, [("IND", "CHEM", 10), ("IND", "PAPER", 12), ("FIN", "BANK", 12)], seed=4)
    current = {k: dict(v) for k, v in static.items()}
    f = fetch_of(dates, series)
    bb, _, _, tables = m.batch(current, f)
    assert "cell_depth" not in tables
    t2 = json.loads(json.dumps(tables))
    assert t2 == tables
    b1, _, _ = m.request(current, f, ["CHEM_I0_B0"], tables=tables)
    b2, _, _ = m.request(current, f, ["CHEM_I0_B0"], tables=t2)
    same(b1["CHEM_I0_B0"], b2["CHEM_I0_B0"]); same(b1["CHEM_I0_B0"], bb["CHEM_I0_B0"])
    # O-14: a rating bucket absent from the month-end tables uses the local scale and says so
    cur2 = {k: dict(v) for k, v in current.items()}; cur2["CHEM_I0_B0"]["rating_bucket"] = "A"
    b3, _, d3 = m.request(cur2, f, ["CHEM_I0_B0"], tables=tables)
    src = d3["_global"]["rating_scale_source"]["m21"]
    assert b3["CHEM_I0_B0"]["m21_z"] is not None and src["A"] == "local" and src["BBB"] == "batch"
    # O-12: integer ids numbered per level give the same numbers as string ids
    fine_id = {"CHEM": 1, "PAPER": 2, "BANK": 3}; wide_id = {"IND": 2, "FIN": 1}
    st2 = {k: dict(v, cell_fine=fine_id[k.split("_")[0]], cell_wide=wide_id["FIN" if k.startswith("BANK") else "IND"], cell_l3=0) for k, v in static.items()}
    b4, _, _ = m.score(m.History(files_from(dates, series, static)))
    b5, _, _ = m.score(m.History(files_from(dates, series, st2)))
    for k in b4:
        same(b4[k], b5[k])
    print("tables round-trip through json; local rating scale recorded; per-level integer cell ids safe")


def t_null_calibration():
    s, z, c21, c63, agree, zd, n0s = [], [], Counter(), Counter(), [], [], []
    for seed in range(2):
        h, *_ = load(230, [("S%d" % a, "S%dSUB%d" % (a, c), 10, 2) for a in range(5) for c in range(35)], seed=seed)
        b, _, dg = m.score(h)
        s += [r["m21_score"] for r in b.values() if r["m21_score"] is not None]
        z += [r["m21_z"] for r in b.values() if r.get("m21_z") is not None]
        if seed == 0:
            c21.update(r["m21_conf"] for r in b.values() if r.get("m21_conf"))
            c63.update(r["m63x_conf"] for r in b.values() if r.get("m63x_conf"))
            agree = [r["m21_conf"] == r["m63x_conf"] for r in b.values() if r.get("m21_conf") and r.get("m63x_conf")]
            zd = [r["m21_z_disp"] for r in b.values() if r.get("m21_z_disp") is not None]
            n0s = (dg["_global"]["m21_z_n0"], dg["_global"]["m63x_z_n0"])
    stats = {}
    for nm, v in (("raw", s), ("z", z)):
        n = len(v); sd = math.sqrt(m._pvar(v)); p2 = sum(abs(x) > 2 for x in v) / n; p3 = sum(abs(x) > 3 for x in v) / n
        stats[nm] = (sd, p2, p3)
        print(f"null {nm}: n={n} sd={sd:.3f} P>2={p2:.4f} P>3={p3:.4f}")
    n21, n63 = sum(c21.values()), sum(c63.values())
    move = sum(abs(x) >= m.READ_MOVE for x in zd) / len(zd)
    print("null seed 0: Conf21 " + " ".join(f"{k} {c21[k] / n21:.3f}" for k in "ABC") + "; Conf63x " + " ".join(f"{k} {c63[k] / n63:.3f}" for k in "ABC")
          + f"; Conf21 = Conf63x {sum(agree) / len(agree):.3f}; move share |Mom21| >= 1.0 {move:.3f}; n0_z {n0s[0]:.1f} {n0s[1]:.1f}")
    sd, p2, p3 = stats["raw"]
    assert 0.95 <= sd <= 1.05 and 0.035 <= p2 <= 0.065 and p3 <= 0.008, ("raw", stats["raw"])
    sd, p2, p3 = stats["z"]
    assert 1.00 <= sd <= 1.20 and 0.04 <= p2 <= 0.09 and p3 <= 0.015, ("z", stats["z"])
    assert c21["A"] / n21 >= 0.95 and c21["C"] / n21 <= 0.01 and 0.30 <= move <= 0.45


def t_runtime():
    cohorts = [("S%d" % a, "S%dSUB%d" % (a, c), 10, 2) for a in range(7) for c in range(50)]
    dates, series, static = universe(230, cohorts)
    current = {k: dict(v) for k, v in static.items()}
    f = fetch_of(dates, series)
    t0 = time.time(); bb, _, _, tables = m.batch(current, f); full = time.time() - t0
    t0 = time.time(); b1, _, _ = m.request(current, f, ["S0SUB0_I0_B0"], tables=tables); q1 = time.time() - t0
    t0 = time.time(); b2, i2, _ = m.request(current, f, {"issuer": "S0SUB0_I1"}, tables=tables); q2 = time.time() - t0
    same(b1["S0SUB0_I0_B0"], bb["S0SUB0_I0_B0"])
    for st in current.values():
        st["cell_l3"] = st["cell_wide"].split("|")[0] + "|bullet|SR|BBB|5-7"           # level 3 = sector, 1,000 bonds
    pulled = []
    t0 = time.time(); m.request(current, lambda isins: (pulled.append(len(isins)), f(isins))[1], ["S0SUB0_I0_B0"]); q3 = time.time() - t0
    print(f"7,000 bonds (one level-3 cell): one-bond request {q1:.0f}s, one-issuer request {q2:.0f}s, full batch {full:.0f}s, scored {len(bb)}; "
          f"with level 3 = sector: one-bond request {q3:.1f}s pulling {pulled[0]} bonds")


if __name__ == "__main__":
    tests = [t_history_boundaries, t_bad_rows_skipped, t_missing_nights, t_gap_netting_zero_on_market_night, t_call_window,
             t_datetime_validation, t_floor, t_stale_mask, t_jump_one_night, t_cum_jump, t_jump_phased_thin_cell,
             t_jump_tonight_scored, t_jump_events_by_window, t_subsector_move_not_flagged, t_scale_invariance,
             t_shrinkage_weight_and_outer, t_slow_reference_benchmark, t_rho_alignment, t_window_coverage_rule,
             t_blank_reasons, t_reading_trend_grid, t_display_rows, t_fallback_z, t_centring_hand, t_z_n0_estimate,
             t_z_clip, t_rotation_centred, t_confidence_rules, t_step_gate_on_display, t_query_equals_batch,
             t_request_from_current_and_pull, t_request_equals_batch_hard_cases, t_hash_seed_determinism, t_stray_date,
             t_unknown_and_empty, t_as_of, t_terciles_and_default_rho, t_issuer_rows_unscored, t_rollup,
             t_coverage_at_most_one, t_tables_json_and_ids, t_null_calibration]
    if "--runtime" in sys.argv:
        tests.append(t_runtime)
    t_start = time.time()
    for t in tests:
        t()
    print(f"ALL PASS ({len(tests)} tests, {time.time() - t_start:.0f}s)")
