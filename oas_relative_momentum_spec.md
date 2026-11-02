# OAS Relative Momentum - Implementation Guide

This document specifies the OAS relative momentum module for the IG relative value repository. The module takes the **current index table** in the repo (one row per constituent, refreshed overnight by a process outside the repo) for constituents, attributes, issuer and peer-group ids, pulls each needed bond's **spread history from Bloomberg** through `blpapi` at request time, and computes four columns for any bond or issuer, plus the display labels `Reading` and `Trend` that sit beside the RV engine's `Gap`. It consumes two things the repo already has: the **peer groups** (the RV engine's cells, three nesting levels) and **`ParEquivOAS`** (the par-equivalent spread).

`oasmom.py` is reproduced as Appendix B. The Bloomberg pull and `ParEquivOAS` already exist in the repo; the module takes them as a single callable. The module uses the Python standard library only.

Sign convention: **positive = relative tightening**.

---

## Part I: Integration

### 1. What the module computes

For a bond: the percentage change in its par-equivalent OAS over a window, less the change in a leave-own-issuer-out benchmark of its peer group, divided by the bond's own volatility of that relative change (the raw score, sign flipped so that tightening is positive), then centred on the median raw score of the other issuers in the group and divided by their scale (the standardized score, clipped at plus or minus 3, which is the displayed value), with a confidence grade per window. Two windows, with `t` the request night (the latest trading night): `Mom21` (the 21 trading nights t-20 to t) and `Mom63x` (the 42 trading nights t-62 to t-21). Four columns: `Mom21`, `Conf21`, `Mom63x`, `Conf63x`. Bond scores roll up to one score and one grade per window per issuer. The display layer adds `Reading` and `Trend`.

This text and Appendix B specify the same computation; a disagreement between them is a defect in whichever departs from the other's stated rule, and Appendix A is the list of record for every constant.

Every median in this document is the usual one: the middle value for an odd count, the mean of the two middle values for an even count.

### 2. Inputs

| Needed | Contract | Source |
|---|---|---|
| Current index table | `current = {isin: row}`, one row per constituent with the fields in section 3 (no history, no `oas`; a `oas` in a row is overwritten by the pulled spread) | the index table already in the repo |
| Spread history | `fetch(isins) -> {isin: {datetime.date: par_equiv_oas}}`: the repo's existing `blpapi` pull composed with `ParEquivOAS`, returning daily par-equivalent spread in bp for the bonds asked for. Keys are `datetime.date`; a `datetime.datetime` key raises `TypeError`. A series may be longer than 215 trading nights (the module keeps the latest 215) | existing pull plus `ParEquivOAS` |
| Peer groups | three nested ids per row: `cell_fine`, `cell_wide`, `cell_l3`; every fine cell lies inside one wide cell and every wide cell inside one level-3 cell. The module relies on the nesting. Ids may be any hashable value; within a level their `str()` forms must be distinct (table keys use `str(id)`) | RV engine peer-group assignment |
| Issuer id | ultimate parent | the table |

**Trading nights.** Let `n_any` be the number of pulled bonds that are in `current` and have at least one finite positive spread anywhere in the pull. A date is a trading night when at least `0.5 x n_any` of those bonds have a finite positive spread on it (inclusive: equality qualifies). Every other date is dropped for every bond. The latest 215 trading nights are kept (all of them when fewer); they are the loaded nights. `t`, the request night, is the latest trading night. A trading night on which a bond has no value is a gap for that bond. A pulled bond that is not in `current` is ignored everywhere in this section: it is not counted in `n_any`, not counted in any date's count, and gets no row. If no date qualifies, the request returns no scores (section 17). Because the rule counts the pulled bonds, `t` is the latest date on which most of them have a spread; it can differ from the calendar date, and from the request night of another request on the same evening whose pull is different. When most of the pulled bonds are recent issues, the dates before their first spread count against `n_any` as well, so early dates can be dropped for every bond and every bond's `history_nights` shortens. `build_history` records `n_pulled` (the ISINs passed to `fetch`), `n_any` and the per-date count of bonds with a spread in `hist.pull`; section 5 gives where the scored night is reported.

Attributes, issuer and peer-group ids are taken from the current table and held fixed across the window; the only thing that varies by night is the pulled spread. Peers are the current members of a cell. If the repo's peer groups have two levels, map fine and wide to them and set `cell_l3` to `structure / seniority / rating bucket / maturity bucket` across sectors, using the delimiter the repo uses.

### 3. Current table row schema

Each row has the keys `issuer_id`, `spread_dur`, `ttm_years` (to effective maturity: next call for FTF and hybrids), `amount_outstanding`, `call_status` in `{bullet, callable, unknown}`, `rating_bucket`, `cell_fine`, `cell_wide`, `cell_l3`. Optional: `next_call_date`, a `datetime.date`. `rating_bucket` is the bucket used for the rating-bucket scale (section 14); it may be coarser than the rating bucket inside the cell ids. The module adds `oas` (par-equivalent, bp) per night from the pull. Every row of `current` must contain `cell_fine`, `cell_wide` and `cell_l3` (a list or issuer request reads them with direct indexing and raises `KeyError` for a row without them).

A bond-night is skipped when any of the following holds: a required key (including that night's `oas`) is missing or `None`; `oas`, `spread_dur`, `ttm_years` or `amount_outstanding` is not a finite `int` or `float` (a `bool` is not); `oas <= 0` or `amount_outstanding <= 0`; `spread_dur < 0` or `ttm_years < 0`; `call_status` is not in the set; `next_call_date` is present, not `None`, and not a `datetime.date` (a `datetime.datetime` fails). A skipped bond-night is recorded as `(date, isin, reason)` in `diag["_global"]["skipped"]` with reason `missing [...]`, `non-finite [...]`, `implausible value`, `bad call_status` or `next_call_date not a datetime.date`, and is thereafter a gap for that bond. A wrong table field is wrong on every night, so the bond is skipped on every night; a bad spread skips only its night. A skipped night tonight gives the bond the reason `bad_row` (section 13). Skipping never stops the calculation.

### 4. Build order

1. **Fetch.** Wrap the repo's existing Bloomberg pull and `ParEquivOAS` as `fetch(isins) -> {isin: {datetime.date: par_equiv_oas}}`: daily, trading days only, with at least 215 trading nights back from the request date for a full history (section 11 gives the counts a window needs; 155 pulled nights for a first Mom21 and 197 for a first Mom63x are the consequence for a bond present from the first night of the pull).
2. **Current table loader.** `current = {isin: row}` from the repo's index table.
3. **Scoring.** Port `oasmom.py` with its arithmetic unchanged; naming, storage and logging follow the repo's conventions. `request(current, fetch, query=None, tables=None)` where `query` is `None` (every bond), a list of ISINs, or `{"issuer": id}`. It resolves the bonds whose history is needed from the current table, pulls them, and scores as of the latest trading night.
4. **Month-end batch.** `batch(current, fetch)` pulls every bond and returns `tables`; persist them. `tables` is JSON-native: `json.loads(json.dumps(tables)) == tables`, and a request given the reloaded tables is identical to one given the original. A bond request or issuer request given the tables of a batch run on the same night returns, for the requested bonds and issuers, the numbers that batch returns. Tables from an earlier month-end are used as given: a key they carry replaces the locally computed value for that key (section 17). `batch(current, fetch)` is `request(current, fetch, None)` plus the tables, so the batch, a whole-index request, a list request and an issuer request apply the same target taxonomy (section 17). Issuer rows are returned by whole-index requests, issuer requests and the batch, because only those load every line of the issuer; a request for a list of ISINs returns bond rows and an empty `issuers`.
5. **Display.** `display_row(isin, rec, gap, dg=None)` returns one row as a dict with keys in this order: `ISIN | Issuer | Gap | Mom21 | Conf21 | Mom63x | Conf63x | Reading | Trend`. Values are unrounded; rounding and formatting belong to the caller. Printed headers carry the sign notes `Gap (+ wide)` and `Mom21 (+ tightened)`.
   - `Gap` is the RV engine's existing spread gap to the peer group in bp (positive wider than peers, i.e. cheap), passed through unchanged and not computed by this module; `None` when the engine has none.
   - `Mom21` is the bond's `m21_z_disp`, `Conf21` its `m21_conf`, `Mom63x` its `m63x_z_disp`, `Conf63x` its `m63x_conf`; `Issuer` is its `issuer_id`.
   - `reading(gap, z21)`, with `READ_GAP_BP = 5.0` and `READ_MOVE = 1.0`, applied to the unrounded `Gap` and unrounded `Mom21`: gap class `cheap` if `gap >= +5.0`, `rich` if `gap <= -5.0`, else `fair`; momentum word `outperforming` if `z21 >= +1.0`, `underperforming` if `z21 <= -1.0`, else `flat`; the label is `"<gap class> and <word>"`. The nine labels are: `cheap and outperforming`, `cheap and underperforming`, `cheap and flat`, `rich and outperforming`, `rich and underperforming`, `rich and flat`, `fair and outperforming`, `fair and underperforming`, `fair and flat`. Boundaries are inclusive. `reading` returns `None` when `gap` or `z21` is `None`.
   - `trend(z63, z21)`: each value is classed up (`>= +1.0`), down (`<= -1.0`) or flat, on the unrounded `Mom63x` and `Mom21`. Both flat: `flat`. `Mom63x` flat and `Mom21` a move: `new`. `Mom63x` a move and `Mom21` flat: `faded`. Both moves in the same direction: `continuing`. Both moves in opposite directions: `reversing`. `None` (blank) when either value is `None`. The five labels are `flat`, `new`, `faded`, `continuing`, `reversing`.
   - `Reading` in `display_row`: when `Mom21` is present, `reading(Gap, Mom21)` (blank when `Gap` is `None`); when `Mom21` is blank, the display text of `Mom21`'s blank reason, never `Mom63x`'s. For a bond with a record the reason is `rec["m21_blank"]`; for a target with no record (no score in either window) it is `blank_code(dg, "m21")` from `dg = diag[isin]`, and `Issuer` comes from `dg["issuer_id"]` (`None` if `dg` lacks it); with neither `rec` nor `dg` the reason is `not_in_table`.
   - Blank-reason display text (`REASON_TEXT`), keyed by reason code in the order of `REASON_ORDER` (section 13): `not_in_table` "not in the table"; `no_spread_tonight` "no spread tonight"; `bad_row` "bad data row tonight"; `new_line` "new line (N nights of history)", with N the bond's `history_nights`; `multi_night_change` "multi-night change tonight"; `not_contributor_today` "stale marks (fewer than 10 changes in the last 21 nights)"; `ttm` "maturity under 1 year"; `call_unknown` "call status unknown"; `call_window` "call within 91 days"; `spread_dur` "spread duration under 1.0"; `burn_in` "not enough pulled history"; `horizon_fill` "window not covered"; `vol_fill` "volatility period not covered"; `zero_var` "zero variance"; `variance_bracket` "volatility bracket too small"; `too_few_reference_issuers` "too few reference issuers"; `no_scored_line` "no line scored" (issuer rows only). `reason_text(code, nights=None)` returns the text for a code (`None` for `None`).
   - Issuer view: `issuer_display_row(issuer_id, rec, gap)` returns `Issuer | Gap | Mom21 | Conf21 | Mom63x | Conf63x | Reading | Trend | n | max_w | pct_pos | sd | coverage`, where `rec = issuers[issuer_id]` or `None`. `Mom21` is `rec["m21_z"]`, `Mom63x` is `rec["m63x_z"]`, the grades are `m21_conf` and `m63x_conf`, and `n`, `max_w`, `pct_pos`, `sd`, `coverage` are the `m21_*` roll-up fields (section 16). `Reading` is `reading(gap, Mom21)` and `Trend` is `trend(Mom63x, Mom21)`, the same rules on the issuer-level values; when `Mom21` is blank `Reading` is "no line scored" if `rec` exists and "not in the table" if `rec` is `None`.

### 5. Interfaces

```python
fetch = <the repo's pull>                                        # fetch(isins) -> {isin: {datetime.date: par_equiv_oas}}
current = load_current_index_table()                             # {isin: row}, section 3

bonds, issuers, diag, tables = batch(current, fetch)             # month-end; persist tables
bonds, issuers, diag = request(current, fetch, ["US912828..."], tables=tables)
bonds, issuers, diag = request(current, fetch, {"issuer": "PARENT_ID"}, tables=tables)

row  = display_row(isin, bonds.get(isin), gap, diag.get(isin))   # dict, section 4
irow = issuer_display_row(issuer_id, issuers.get(issuer_id), issuer_gap)
night = diag["_global"]["request_night"]                         # the night that was scored, and the counts behind it
```

Signatures in `oasmom.py`:

| Function | Signature and result |
|---|---|
| `request` | `request(current, fetch, query=None, tables=None) -> (bonds, issuers, diag)`; `query` is `None`, a list of ISINs or `{"issuer": id}` |
| `batch` | `batch(current, fetch) -> (bonds, issuers, diag, tables)`; `request(current, fetch, None)` plus the tables |
| `batch_from_history` | `batch_from_history(hist, as_of=None) -> (bonds, issuers, diag, tables)` |
| `score` | `score(hist, query=None, as_of=None, tables=None) -> (bonds, issuers, diag)`; `as_of` is a date in `hist.dates` (default the last) |
| `build_history` | `build_history(current, fetch, isins, n_nights=215) -> History`; `History` has `dates`, `rows` (`{date: {isin: row}}`, valid rows), `skipped` (`[(date, isin, reason)]`) and `pull` (`{"n_pulled", "n_any", "count": {date: bonds with a spread}}`) |
| `needed_isins` | `needed_isins(current, targets) -> set` |
| `spread_terciles` | `spread_terciles(today) -> {isin: (wide cell, tercile)}` (section 12) |
| `min_counts` | `min_counts(name) -> {"covered": int, "n_var": int}`: `{"covered": 19, "n_var": 114}` for `m21`, `{"covered": 38, "n_var": 114}` for `m63x` (section 11) |
| `earliest_score`, `full_history` | `earliest_score(name) -> int`; `full_history(name) -> int` (section 11) |
| `reading`, `trend` | `reading(gap, z21) -> str or None`; `trend(z63, z21) -> str or None` |
| `display_row` | `display_row(isin, rec, gap, dg=None) -> dict` with keys `ISIN, Issuer, Gap, Mom21, Conf21, Mom63x, Conf63x, Reading, Trend` |
| `issuer_display_row` | `issuer_display_row(issuer_id, rec, gap) -> dict` with keys `Issuer, Gap, Mom21, Conf21, Mom63x, Conf63x, Reading, Trend, n, max_w, pct_pos, sd, coverage` |
| `blank_code`, `reason_text` | `blank_code(dg, name) -> code or None` (`not_in_table` for an empty `dg`); `reason_text(code, nights=None) -> str or None` |
| `REASON_ORDER`, `REASON_TEXT` | the reason codes in precedence order (section 13); code to display text (section 4) |

`name` is `"m21"` or `"m63x"`.

`bonds[isin]` holds the bonds with a score in at least one window that are targets of the request (all bonds with a score for `query=None`). Keys: `issuer_id`, `cell` (fine cell id), `wide`, `rating`, `oas` (spread on `t`), `outer_level`, `history_nights`, `as_of` (the scored night, a `datetime.date`). Per window `W` in `m21`, `m63x`: `W_score` (raw, `None` when the window has no score), and when scored `W_rel` (the window's relative change, sign flipped, equal to `-rel_k`), `W_step_share`, `W_jump_nights`, `W_jump_events`, `W_w` (roll-up weight), `W_w_night` (position in the History calendar of the night the weight was read, absent when no weight was found); then `W_z` (unclipped), `W_z_disp` (clipped at 3; the displayed Mom value), `W_z_centre` (`m_cell`), `W_z_ref` (`fine`, `wide` or `none`), `W_z_n_ref`, `W_conf` (the displayed Conf value; `None` when `W_z` is `None`), `W_blank` (the displayed reason code of a window without `W_z`, else `None`).

`issuers[id]` (whole-index requests, issuer requests and the batch): one row per issuer with a target line, scored or not. Keys: `as_of`, `n_bonds` (lines with a score in either window), `n_cells` (distinct fine cells among them), `n_lines` (the issuer's lines in the current table on the request paths; its target lines in the history for `score`). Per window `W`: `W_z` (displayed), `W_raw`, `W_conf` (worst line), `W_n`, `W_n_dropped_no_weight`, `W_max_w`, `W_pct_pos`, `W_sd`, `W_coverage`, `W_blank`. For a window with no scored line: `W_z`, `W_raw`, `W_conf`, `W_max_w`, `W_pct_pos`, `W_sd`, `W_coverage` are `None`, `W_n` is 0 and `W_blank` is `no_scored_line`; for a scored window `W_blank` is `None`. A request for the whole index or an issuer returns a row for every issuer with a line in `current` that is a target, including an issuer whose lines are all blank or have no spread tonight.

`diag[isin]`, for a target with a valid row on `t`: `blank` (dict: `all`, the one bond-level reason, if any; `W`, the window's reason, if any), `reasons` (every structural reason code that holds, sorted by precedence; `blank["all"]` is `reasons[0]` when any holds), `issuer_id`, `history_nights`, `bench_fine`, `bench_outer`, `outer_level`, `w_fine`, `n_fine_other`, `moves_21`, `jump_today`, `display_reason` (`{W: code}`, one entry per window, `None` for a window with a `W_z`), and per window `W` that reaches the fill test (blank or not): `covered` and `n_var`; per scored window additionally `sd_k`, `var1`, `rho`, `rho_own`, `rho_pool`, `w_own`. For a target without a valid row on `t`: `{"blank": {"all": code}, "display_reason": {W: code}}` with `issuer_id` when known, and `bad_row_detail` for `bad_row`. `diag["_query"]` is `{"blank": {"all": "not_in_table"}, "display_reason": ...}` for an empty or unmatched query. `diag["_global"]`: `as_of` (the scored night, `None` when nothing was scored); `request_night` (requests and the batch): a dict with `as_of` (the date), `n_pulled` (the ISINs passed to `fetch`), `n_any` (pulled bonds in `current` with any finite positive spread) and `n_spread` (pulled bonds in `current` with a finite positive spread on that date); per window `W_z_n0`; `min_counts` (`{W: {"covered", "n_var"}}`); `rating_scale`, `rating_centre`, `rating_n`, `rating_scale_source` (`batch` or `local` per bucket); `pooled_rho`; `w_own`; `tables` (`batch` when tables were passed, else `local`); `skipped`.

**Size and time.** A request pulls history for every bond in the targets' fine, wide and level-3 cells (`needed_isins`), whatever their number; compute time scales with that count. A one-bond request that pulls 1,000 bonds takes about 9 seconds; a full batch on a 7,000-bond test universe takes about 160 seconds (CPython 3.13, 2 CPUs, excluding the pull); the 10,000-bond index takes proportionally longer.

### 6. Parameters

The values in Appendix A are fixed. Naming, storage and logging follow the repo's conventions; nothing that changes a number does.

---

## Part II: Method reference

Every quantity is derived at request time, night by night, from each bond's pulled spread series and its current-table attributes. This part states each rule of `oasmom.py`; Appendix A lists every constant.

### 7. Calendar and daily change

**Calendar.** Trading nights (section 2) are positions `0..N-1`, `t` the last. Night `t-j` is `j` positions before `t`. The loaded nights are the latest 216 (positions `N-216..N-1`; all of them when `N < 216`, which is always so for a 215-night pull); the first loaded night is `start`. The loaded bonds are those whose history is needed (section 17). Bonds are processed in sorted ISIN order and cells in sorted order, so every float sum is reproducible and results do not depend on `PYTHONHASHSEED`.

**Daily change.** For a loaded bond with a valid row on night `i`: `prev` is the bond's most recent earlier valid row, used only if it lies within 3 nights (`i - i' <= 3`); otherwise `prev` is absent. With `prev`: `d1 = (S_i - S_prev) / max(S_prev, floor_i)`, `gap = i - i'` (1, 2 or 3), where `S` is the par-equivalent spread and `floor_i = min(40, max(15, 0.35 x M_i))` bp, with `M_i` the median spread on night `i` of every loaded bond in the bond's fine cell that has a valid row on `i` (the bond itself and non-contributors included). A change spanning `gap > 1` nights uses the floor of its last night `i`. Without `prev`: `d1` is undefined and `gap = 0`.

**Moved.** A bond has `moved` on night `i` when `d1` is defined and `S_i != S_prev` (exact inequality of the pulled spread, any span; a multi-night change counts once, on the night it appears). The stale mask therefore detects a stale price only when the pull carries the prior par-equivalent spread forward unchanged on a night with no new price; a stale price re-evaluated against a moving curve registers as a move every night.

### 8. Contributor, stale mask, jump flags

**Stale mask.** `moves_i` is the number of nights in `max(start, i-20)..i` (tonight included) on which the bond moved. The mask passes on night `i` when `i - start >= 20` (the 21st loaded night or later) and `moves_i >= 10`. The first 20 loaded nights never pass.

**Contributor** on night `i`: `d1` defined, `gap = 1`, the stale mask passes, and `spread_dur >= 1.0`. **Benchmark contributor**: a contributor with neither jump flag set. **Scoring-eligible** (section 13): `gap = 1`, the stale mask passes and `spread_dur >= 1.0`, together with the maturity and call conditions; a jump flag does not remove a bond from scoring, and a jump on the request night is scored and counted toward the grade as a jump event starting that night.

**One-night jump.** Population: the contributors tonight (the bond itself and its issuer's other lines included), taken in the bond's fine cell if that cell holds at least 5 of them, else in its wide cell if that holds at least 5; if neither does, no jump flag is set for the bond tonight. Both jump flags are evaluated only for contributors tonight; a bond that is not a contributor tonight is never flagged. The statistic is `d1`. `med` is the population median of `d1`; `MAD` is 1.4826 times the median absolute deviation of the population from `med`. The flag is set when `abs(d1 - med) > max(6 x MAD, 0.10)` (strict).

**Cumulative jump.** For every contributor with a population as above, `cum = (sum of the bond's rel_1 on the 4 preceding nights, a night without rel_1 adding 0, nights before `start` not available) + (d1 - med)`. For each population key (the fine cell, or the wide cell for bonds that fell back to it) the contributors using it supply their `cum` values (bonds with a one-night flag included); `cmed` and `cMAD` (1.4826 times the median absolute deviation about `cmed`) are computed only if at least 5 `cum` values exist. For a bond without a one-night flag, the cumulative flag is set when `abs(cum - cmed) > max(6 x cMAD, 0.15)` (strict).

**Flagged night and jump event.** A bond's night is flagged when either flag is set on it. A jump event is a maximal run of consecutive flagged trading nights of that bond (a night without a row is unflagged and ends a run). One re-mark sets the one-night flag on its night and can set the cumulative flag on up to the next 4 nights, which is one run and so one event. Either flag removes the bond from the benchmarks of that night only.

### 9. Benchmark

Night-by-night, per cell level (fine, wide, level 3, each separately) and per issuer: the **issuer value** is the mean of `d1` over the issuer's benchmark-contributing lines in that cell, weighted by `amount_outstanding x spread_dur x S_i` (tonight's spread). An issuer is a **member** of a cell tonight if it has at least one benchmark-contributing line in it. Across issuers, equal weight.

**Clipping bounds.** For a cell, let `V` be the sorted issuer values of all its members (the whole cell, before any issuer is removed) and `N = len(V)`. With zero-based indices, `lo = V[max(1, floor(0.1 (N-1)))]` and `hi = V[min(N-2, ceil(0.9 (N-1)))]` when `N >= 3` (computed as `int(0.1 * (N - 1))` and `math.ceil(0.9 * (N - 1))`); for `N < 3` there is no clipping (`lo = V[0]`, `hi = V[N-1]`). A clipped value is `min(hi, max(lo, v))`.

**Cell aggregate excluding a set `X` of issuers.** Let `n_rem` be the number of members not in `X`. If `n_rem <= 0` there is no aggregate. If `n_rem <= 8`, the aggregate is the median of the unclipped values of the remaining members. Otherwise it is the arithmetic mean of the clipped values of the remaining members.

- `B_fine`: the fine cell excluding the bond's own issuer. `n_f` is its `n_rem`.
- `B_wide`: the wide cell excluding the bond's own issuer and every issuer that is a member of the bond's fine cell tonight. `n_w` is its `n_rem`.
- `B_l3`: the level-3 cell excluding the bond's own issuer and every issuer that is a member of the bond's wide cell tonight. `n_3` is its `n_rem`.
- **Outer benchmark `B_outer`**: `B_wide` if `n_w >= 6`; else `B_l3` if `n_3 >= 6`; else none. `outer_level` is `wide`, `l3` or none.

**Weight on the fine cell.** For each fine cell `c` and night `j`, `c_j = max(0, (number of members of c on night j) - 1)`, one value per fine cell, the same for every bond in it; nights on which no loaded bond of `c` has a row add no value. `n_eff` is the median of `c_j` over tonight and the 63 preceding loaded nights (up to 64 values; fewer where fewer loaded nights exist; it is not rounded, so a median of an even count of integer counts can be a half-integer and is used as it is). `w = 0` when `n_eff < 4`, else `w = n_eff / (n_eff + 5)`.

**Benchmark `bench`:** if `B_outer` and `B_fine` both exist and `w > 0`: `w x B_fine + (1 - w) x B_outer`; else if `B_outer` exists: `B_outer`; else if `B_fine` exists and `n_f >= 6`: `B_fine`; else none (the bond has no `rel_1` that night). `bench` is computed for every loaded bond with a row, own issuer excluded as above.

### 10. Relative change

`rel_1` exists on night `i` for a bond in exactly two cases.

- **Single-night change:** the bond is a contributor on `i` (`gap = 1`, the stale mask passes, `spread_dur >= 1.0`; jump flags do not remove it) and `bench` exists. `rel_1 = d1 - bench`.
- **Multi-night change:** `gap = g > 1`, `bench` exists tonight, `spread_dur >= 1.0`, and the bond moved on at least 10 of the nights `max(start, i-20)..i` (the first-20-nights warm-up of the stale mask does not apply). `rel_1 = (1 + d1) / prod - 1`, `prod = (1 + bench_i) x prod over j = 1..g-1 of (1 + bench_{i-j})`, where `bench_{i-j}` is the `bench` the bond's (fine, wide, level-3, issuer) combination had on night `i-j`; if no bond of that combination had a benchmark that night, the benchmark that night's (fine, wide, level-3) cells give an issuer that is a member of none of them (the blend of this section computed with no own issuer to exclude); if neither exists, or night `i-j` is before `start`, `rel_1` is `None`.

On every other night `rel_1` is `None`. The single-night `rel_1` series of span 1 feeds the volatility estimate; both kinds feed the window sum.

### 11. Horizons

| Column | Window nights | k | skip | Volatility period |
|---|---|---|---|---|
| `Mom21` (`m21`) | t-20 to t | 21 | 0 | t-146 to t-21 (126 nights) |
| `Mom63x` (`m63x`) | t-62 to t-21 | 42 | 21 | t-188 to t-63 (126 nights) |

The window is the `k` nights ending `skip` nights before `t`; the volatility period is the 126 nights ending on the night before the window's first night.

`rel_k` is the sum of the counted `rel_1` in the window. A `rel_1` at in-window position `j` (`j = 0` the window's first night) of span `g` is counted only if `g <= j + 1` (all `g` nights it spans lie inside the window); a counted `rel_1` covers `g` nights, so coverage never exceeds `k`. Uncounted changes are excluded from `rel_k` and from the step share. The covered nights must be at least `0.9 k`, unrounded (18.9, so 19 of 21; 37.8, so 38 of 42), otherwise the window is blank (`horizon_fill`). The volatility period must hold at least `0.9 x 126 = 113.4`, so 114, nights with a span-1 `rel_1`, otherwise blank (`vol_fill`); `horizon_fill` is tested first.

**Counts a window needs.** A bond scores a window when it has at least 19 covered nights for `Mom21` (38 for `Mom63x`; the `0.9 k` of the paragraph above, rounded up), at least 114 span-1 `rel_1` in the volatility period, and passes the later tests. `min_counts(W)` returns `{"covered": 19, "n_var": 114}` for `m21` and `{"covered": 38, "n_var": 114}` for `m63x`, also stored in `diag["_global"]["min_counts"]`; `diag[isin][W]` carries the bond's `covered` and `n_var` for every window that reaches the fill test, blank or not.

**History needed.** `history_nights` is the count of loaded nights from the bond's first valid row through `t` inclusive. `earliest_score = 20 + 126 - 12 + k + skip` and `full_history = 20 + 126 + k + skip` are the pulled nights at which a bond listed on the pull's first night with a spread on every night first scores (155 for `Mom21`, 197 for `Mom63x`: the first 20 loaded nights never pass the stale mask, so 12 of the 126 volatility nights are unusable and the 114 minimum is met exactly) and has a fully populated volatility period (167 and 209, `n_var` 126). A bond listed part-way through a 215-night pull with a spread on every night since needs only 10 changes before the stale mask passes, so it scores a first `Mom21` at 145 nights of its own history and a first `Mom63x` at 187 (144 and 186 give `new_line`). `burn_in`: the loaded nights number fewer than `126 + k + skip` (147 for `Mom21`, 189 for `Mom63x`), so the volatility period would start before the first loaded night. `new_line` has two forms. Bond level: `history_nights` is under 21 (`STALE_WINDOW`), so the bond has no stale-mask history; it has no score in either window. Window level: a window that would be blank with `horizon_fill` or `vol_fill` reports `new_line` instead when `history_nights` is under `earliest_score`.

### 12. Volatility

`sd_k^2 = var1 x (k + 2 (k - 1) rho)`.

`var1` is the population variance (divided by the count) of the bond's span-1 `rel_1` over the volatility period; the window is blank with `zero_var` if the variance is 0.

`rho_own` is the lag-1 autocorrelation estimated per window on the same series: nights without a span-1 `rel_1` are missing; `mu` is the mean of the `n` present values, `var` their population variance, and `rho_own = (sum over adjacent night pairs with both values present of (a - mu)(b - mu)) / (pair count) / var`. It is `None` if `n < 2`, there is no such pair, or `var = 0`. It is computed for every loaded bond that has a row on `t` and at least 114 span-1 `rel_1` in the window's volatility period, whether or not the bond is a target or eligible.

**Pooled rho** (per window): from `rho_own` of every such bond, each issuer's median over its lines inside the key being pooled (for (a) its lines in that wide cell and tercile, for (b) its lines in that wide cell, for (c) all its lines), then the median over issuers for (a) each wide cell and spread tercile, (b) each wide cell, (c) the universe (`index`). (a) and (b) are kept only with at least 5 issuers. **Spread tercile:** within each wide cell, the `N` loaded bonds with a row on `t` are sorted by their spread on `t` ascending, ties by ISIN string ascending; with sorted position `p` counted from 0, a bond is tercile 0 when `p < floor(N/3)`, tercile 1 when `floor(N/3) <= p < floor(2N/3)`, else tercile 2 (20 bonds split 6, 7, 7; 7 bonds split 2, 2, 3). A wide cell with fewer than 3 bonds has no tercile 0 (1 bond: tercile 2; 2 bonds: terciles 1 and 2). `rho_pool` is (a) if present, else (b), else (c). If no loaded bond has a `rho_own` in the window, `rho_pool = -0.3` (`RHO_DEFAULT`) and nothing else is computed for the window: `diag["_global"]["pooled_rho"][W]` is `{"tercile": {}, "wide": {}, "index": -0.3}` and `diag["_global"]["w_own"][W]` is `0.0`, the tables carry the same values, and any tables passed for that window are not used.

**`w_own`** (per window, one value): `se2` is the mean over bonds with a `rho_own` of `(1 - 3 r^2 + 4 r^4) / n_obs`, `r` the universe pooled rho and `n_obs` the bond's count of span-1 `rel_1` in its volatility period; `tau2 = max(pvar(issuer medians of rho_own) - se2, 0)` (the variance is 0 for fewer than 2 issuers); `w_own = tau2 / (tau2 + se2)`.

**`rho`** is `max(-0.50, w_own x rho_own + (1 - w_own) x rho_pool)`; a bond without `rho_own` uses `max(-0.50, rho_pool)`. The floor applies to the blend. The bracket is `k + 2 (k - 1) rho`; a bracket **at or below `0.1 k`** blanks the window with `variance_bracket`.

`score_k = -rel_k / sd_k`, the raw score; `W_rel = -rel_k`.

Pooled rho, `w_own`, the rating-bucket scale and `n0_z` are universe statistics taken from the month-end tables; a request without tables, or with tables that lack a key, computes the missing ones from the loaded cells and labels the result `local` (section 17).

### 13. Score eligibility and blank reasons

A bond is scored for a window when all of the following hold on `t`:

1. it has a valid row on `t`;
2. it has at least 21 loaded nights of its own (`history_nights >= 21`), `gap = 1` on `t` and the stale mask passes (a jump flag does not matter);
3. `spread_dur >= 1.0` and `ttm_years >= 1.0`;
4. the call rule passes (below);
5. the window passes the burn-in, fill, variance and bracket tests (sections 11 and 12);
6. a standardization reference and a nonzero scale exist (section 14).

**Call rule**, measured from `t`: `bullet` passes and `next_call_date` is ignored. `callable` passes only if `next_call_date` exists and is later than `t + 91` calendar days; `t < next_call_date <= t + 91` gives `call_window` (day 91 is blank); a missing `next_call_date` or one on or before `t` gives `call_unknown`. `unknown` gives `call_unknown`.

**Blank reason codes**, in order of precedence (`REASON_ORDER`); the first that applies is displayed:

| Order | Code | Applies when |
|---|---|---|
| 1 | `not_in_table` | the ISIN is not in `current` (on `score` called on a bare `History`: the ISIN has no row, valid or skipped, on any loaded night) |
| 2 | `no_spread_tonight` | the ISIN is in `current` (on a bare `History`: has a row, valid or skipped, on some loaded night) and has no row on `t`; this includes a bond with nothing in the pull and a bond whose spreads fall only on dates dropped as non-trading |
| 3 | `bad_row` | `t`'s row failed the checks of section 3 (`diag[isin]["bad_row_detail"]`) |
| 4 | `new_line` | bond level: `history_nights` is under 21. Window level: `history_nights` is under the window's `earliest_score` and the window would be blank for `horizon_fill` or `vol_fill` |
| 5 | `multi_night_change` | `t`'s change spans `gap > 1` nights |
| 6 | `not_contributor_today` | `history_nights >= 21`, `gap <= 1` and (no `prev` within 3 nights, or the stale mask fails) |
| 7 | `ttm` | `ttm_years < 1.0` |
| 8 | `call_unknown` | the call rule gives `call_unknown` |
| 9 | `call_window` | the call rule gives `call_window` |
| 10 | `spread_dur` | `spread_dur < 1.0` |
| 11 | `burn_in` | the window's volatility period starts before the first loaded night (section 11) |
| 12 | `horizon_fill` | the window's covered nights are under `0.9 k` |
| 13 | `vol_fill` | the volatility period holds under 114 span-1 `rel_1` |
| 14 | `zero_var` | `var1 = 0`, or the standardization scale is 0 or the bond's rating bucket has no scale (section 14) |
| 15 | `variance_bracket` | the bracket is at or below `0.1 k` |
| 16 | `too_few_reference_issuers` | neither the fine nor the wide cell has 5 reference issuers |
| 17 | `no_scored_line` | issuer rows only: no line of the issuer has a displayed score in the window |

Codes 1 to 3 are target-level and are stored as `diag[isin]["blank"]["all"]`. Bond-level `new_line` and codes 5 to 10 are structural: several can hold at once; all are listed in `diag[isin]["reasons"]`, sorted by precedence, `blank["all"]` is `reasons[0]` when any holds, and a bond with any structural reason has no score in either window. Window-level `new_line` and codes 11 to 16 are per window and stored as `blank[W]`, from the score stage (11 to 15 and window-level `new_line`) or the z stage (`zero_var` from the scale, and `too_few_reference_issuers`); a window has at most one, and there is no separate key for the z stage. The displayed reason for window `W` is `blank["all"]` if present, else `blank[W]` (`blank_code`). `diag[isin]["display_reason"]` has exactly one entry per window, that code or `None` for a window with a `W_z`, and is recomputed after every change to `blank`; scored records carry it as `W_blank`. A blank is `None` in the output, never zero.

### 14. Standardization

Applies to a window `W` for every bond with a raw score in `W`; all populations below are the loaded bonds with a raw score in `W`.

**Issuer value.** An issuer's value in a group of lines is the median of the signed raw scores of its lines in the group.

**Reference population** for a bond: the issuers other than the bond's own that have at least one scored line in the bond's fine cell; if there are at least 5, the reference is `fine`; else the same in its wide cell, `wide` if at least 5; else none, and `W_z`, `W_z_disp` and `W_conf` are blank with reason `too_few_reference_issuers` (the raw score is kept, and the window has no grade). `n` is the number of reference issuers.

**Centre and cell scale.** `m_cell` is the median of the reference issuers' values; `s_cell = 1.4826 x median(abs(value - m_cell))` over the same issuers.

**Rating scale.** Per window and `rating_bucket` (matched by `str()`), over the loaded bonds with a raw score in `W`. Each issuer with a scored line in the bucket is assigned a reference cell, fixed by its first scored line in sorted ISIN order: that line's fine cell if that cell has at least 5 issuers other than this one with a scored line in `W` (any rating bucket), else that line's wide cell if it has at least 5, else the issuer is left out. The issuer's deviation is `(v - c) / sqrt(1 + Z_MEDIAN_VAR / n_other)`, with `v` the median of the issuer's scored lines in that cell, `c` the median of the other issuers' values in that cell (each other issuer's median over its lines there, as for `m_cell`), `n_other` their count and `Z_MEDIAN_VAR = 1.571`; the division takes out the sampling variance of `c`. `s_rating = 1.4826 x median(abs(deviation))` over the issuers that entered, and `n_rating` is the number that entered. `m_rating`, the median of the bucket's issuer values (each issuer's median over all its scored lines in the bucket), is carried and recorded and does not enter `z`. Where the month-end tables carry the bucket, their `rating_scale`, `rating_centre` and `rating_n` replace these three values for that bucket. A bucket in which no issuer entered has no `s_rating`; a bond in it has no `z` (reason `zero_var`). The sampling correction on the rating term is `s_rating_c = s_rating x sqrt(1 + Z_MEDIAN_VAR / n_rating)`, with `n_rating` at least 1.

**Scale.** `scale^2 = (n x s_cell^2 + n0_z x s_rating_c^2) / (n + n0_z)`; `scale_used = scale x sqrt(1 + Z_MEDIAN_VAR / n)`, which allows for the sampling variance of the median `m_cell` of `n` values (about `Z_MEDIAN_VAR / n` in scale units). The correction is on the scale, not on `m_cell`. If `scale_used = 0`, or the bond's bucket has no `s_rating`, the window has no `z`, with reason `zero_var` in `blank[W]`.

**z.** `z = (score - m_cell) / scale_used`. `z_disp = clip(z, -3, 3)` is the displayed value; `z` is retained unclipped. `m_cell` is stored as `W_z_centre`. A move shared by the whole reference group is removed by `m_cell` and does not appear in any member's `z`; it belongs to the sector view, as it does for `Gap`.

**`n0_z`** (per window): from the tables if present and non-zero, else estimated from the loaded cells: take every fine cell and every wide cell with at least 6 issuers having a scored line in `W` (`n` = that count, own issuer counted), with cell scale `1.4826 x median(abs(value - cell median))` over all of its issuers; keep those with scale above 0. If fewer than 20 remain, `n0_z = 12`. Otherwise `v` is the population variance of the natural logs of the cell scales, `tau_s^2 = max(v - mean(1.3 / n), 0.0001)` (the mean over the kept cells), and `n0_z = clip(2 / tau_s^2, 5, 60)`.

### 15. Confidence grade, one per window

Inputs for a window `W`: `jump_events` (below); `step_share`; the reference level `W_z_ref` and its `n` (`W_z_n_ref`), counted for that window alone, so depth can differ between `Conf21` and `Conf63x`.

- **Jump events.** A night inside the window is flagged if the bond's one-night or cumulative jump flag is set on it (section 8). `jump_events` is the number of flagged nights in the window whose preceding trading night (the night before, even if outside the window) is not flagged. An event counts toward the window that contains its first flagged night; a run that starts before the window and continues into it counts nothing in that window. `W_jump_nights` (flagged nights in the window) is kept as a diagnostic and does not enter the grade. Benchmarks exclude every flagged night regardless.
- **Step share.** The maximum, over every 3 consecutive nights inside the window (positions `j..j+2`, `j = 0..k-3`), of `abs(sum of the counted rel_1 on those nights)`, a night without a counted `rel_1` adding 0, divided by `abs(rel_k)`; it is `None` (treated as 0) if `rel_k = 0`.
- **Step gate.** The step share is tested only when `max(abs(raw score), abs(z)) >= 2.0` (`z` unclipped). Below that the step test counts as passed (step share treated as 0).

Grade:

- **C** if `jump_events >= 2`, or the step share is tested and is at least 0.80.
- **A** if `jump_events = 0`, and the step share is under 0.60 or not tested, and the reference is `fine` with `n >= 8`.
- **B** otherwise.

`Conf21` uses the `Mom21` window; `Conf63x` uses the `Mom63x` window. A window with no displayed score has no grade. The issuer's grade for a window is the worst grade among the lines that entered that window's roll-up.

### 16. Issuer roll-up

For query `None`, issuer queries and the batch, one row per issuer with a target line. `n_bonds` is the number of its lines with a score in at least one window and `n_cells` the number of distinct fine cells among them; `n_lines` is its number of lines in the current table on the request paths (its target lines in the history for `score`); every row carries `as_of`. Per window `W`, the lines that enter are those with a displayed score (`W_z_disp` not `None`) and a non-zero weight `W_w`; lines with a score and no weight are counted in `W_n_dropped_no_weight`.

- **Weight** `W_w = amount_outstanding x spread_dur x S`, with `S` the bond's spread on the window's first night (t-20 for `Mom21`, t-62 for `Mom63x`); if none that night, its latest spread within the 3 preceding trading nights; if none, no weight.
- `W_z` = weighted mean of `W_z_disp`; `W_raw` = weighted mean of the raw score; `W_n` = number of entering lines; `W_max_w` = largest weight share; `W_pct_pos` = share of entering lines with raw score above 0; `W_sd` = population standard deviation of the raw scores, `None` if `W_n = 1`; `W_conf` = worst entering line's grade. If the weights sum to 0 or no line enters (including an issuer with no scored line at all, or no scored line in that window), `W_z`, `W_raw`, `W_conf`, `W_max_w`, `W_pct_pos`, `W_sd` and `W_coverage` are `None`, `W_n = 0` and `W_blank = no_scored_line`; for a window that has entering lines `W_blank` is `None`. An issuer with a target line and no scored line in either window still gets its row.
- **`W_coverage`**: with `h0` the window's first night and `lo = max(start, h0 - 3)`, the numerator is the sum of the weights of the entering lines; the denominator is the sum, over every loaded line of the issuer with a valid row on any night in `lo..h0`, of `amount_outstanding x spread_dur x spread` on that line's latest such night. Coverage is at most 1; `None` if the denominator is 0.

### 17. Request paths and tables

**Single request.** `query` is a list of ISINs or `{"issuer": id}`. The targets are the listed ISINs, or every current-table line of the issuer. The pull is `needed_isins`: the targets that are in `current`, plus every `current` bond sharing the fine, wide or level-3 cell of any target. The module builds the history (section 2), then loads the targets and every bond sharing a fine, wide or level-3 cell with a target on any loaded night; for an issuer query the scored targets are every line of the issuer with a valid row on any loaded night, so the coverage denominator holds all its lines; a line without a row on `t` gets `no_spread_tonight`. It derives per-night inputs and `rel_1`, scores, standardizes against the loaded reference population with the month-end tables, and, for an issuer query, rolls up the issuer. A list request returns `issuers = {}`.

**Whole index.** `query=None` and `batch` load every bond with a valid row on any night of the loaded window and return the bonds with a score; every other bond in `current` gets a `diag` entry (below). `request(..., None)` pulls every bond in `current`. `batch(current, fetch)` is `request(current, fetch, None)` plus `tables`. A request reports the night it scored: `bonds`, `issuers` and `diag["_global"]` carry `as_of`, and `diag["_global"]["request_night"]` carries the pulled count, the count with a spread that night and `n_any` (section 5).

**Tables.** `tables = {"pooled_rho": {W: {"tercile": {"<wide id>|<tercile 0..2>": rho}, "wide": {"<wide id>": rho}, "index": rho}}, "rating_scale": {W: {"<bucket>": s_rating}}, "rating_centre": {W: {"<bucket>": m_rating}}, "rating_n": {W: {"<bucket>": n_rating}}, "w_own": {W: value}, "z_n0": {W: value}, "as_of": "<ISO date>", "method_version": "<label>"}`. Keys are `str(id)`; lookups use the same string forms. When passed to `score` or `request`: each `pooled_rho` key replaces the local value for that key and `index` replaces the local `index`; `w_own` and `z_n0` replace the local values when present (`z_n0` when non-zero); `rating_scale`, `rating_centre` and `rating_n` replace the local value bucket by bucket, a bucket absent from the tables keeping its local value (`diag["_global"]["rating_scale_source"]` is `batch` or `local` per bucket). Pooled rho and `w_own` are computed, and the tables' values for them used, only for a window in which some loaded bond has a `rho_own`; without one the pooled rho is -0.3 and `w_own` is 0.0 (section 12).

**Targets without a score.** One taxonomy applies on every path (list, issuer, whole-index request, batch). A target without a valid row on `t` gets `bad_row` when `t`'s row failed the section 3 checks (detail in `bad_row_detail`); otherwise `no_spread_tonight` when the ISIN is in `current` (on `score` called on a bare `History`: when it has a row, valid or skipped, on some loaded night); otherwise `not_in_table`. On the request paths `not_in_table` therefore means the ISIN is not in `current`; a bond in `current` with nothing in the pull, or with spreads only on dates dropped as non-trading, is `no_spread_tonight`. `score(hist, None)` takes as targets every bond with a row, valid or skipped, on some loaded night. Each such `diag` entry carries `issuer_id` when known and `display_reason` equal to its code for both windows.

**Empty and unknown queries.** `request` never raises for these inputs. An empty list, or an issuer with no line in `current`, returns `({}, {}, {"_query": {"blank": {"all": "not_in_table"}, "display_reason": ...}, "_global": {...}})`, with `as_of` `None` and `request_night` counts 0. A listed ISIN not in `current` gets `not_in_table`. If the pull yields no trading night, or nothing is pulled, it returns `({}, {}, diag)` (plus issuer rows with no scored line for a whole-index or issuer request) with `not_in_table` for targets not in `current` and `no_spread_tonight` for targets in it.

---

## Appendix A: parameters

| Parameter | Code constant | Value | Section |
|---|---|---|---|
| spread | (input) | par-equivalent OAS, bp, from the repo's pull | 2, 7 |
| history kept | `HISTORY_NIGHTS` | 215 trading nights | 2, 5 |
| trading-night share | `TRADING_NIGHT_MIN_SHARE` | 0.5 (a date is a trading night at or above this share of the pulled bonds that are in `current` and have a spread anywhere in the pull) | 2 |
| loaded window | inline: 63 + 126 + 21 + 5 | latest 216 nights (`start = max(0, pos - 215)`) | 7 |
| required row keys | `REQUIRED` | `issuer_id`, `oas`, `spread_dur`, `ttm_years`, `amount_outstanding`, `call_status`, `rating_bucket`, `cell_fine`, `cell_wide`, `cell_l3` | 3 |
| numeric row keys | `NUMERIC` | `oas`, `spread_dur`, `ttm_years`, `amount_outstanding` | 3 |
| horizons (k, skip) | `HORIZONS` | `m21` (21, 0), `m63x` (42, 21) | 11 |
| vol window | `VOL_WINDOW` | 126 nights | 11, 12 |
| fill | `MIN_FILL` | 0.90 (window: covered nights at or above 0.9 k, unrounded; volatility period: at or above 113.4, so 114) | 11 |
| earliest score | `earliest_score` | `20 + 126 - int(0.1 x 126) + k + skip` = 155, 197 | 11 |
| full history | `full_history` | `20 + 126 + k + skip` = 167, 209 | 11 |
| minimum counts | `min_counts` | covered nights `ceil(MIN_FILL x k)` = 19 (`m21`), 38 (`m63x`); span-1 `rel_1` in the volatility period `ceil(MIN_FILL x VOL_WINDOW)` = 114 | 11 |
| part-way listing | derived | a bond listed part-way through a 215-night pull with a spread on every night since: first `m21` at 145 own nights, first `m63x` at 187 | 11 |
| burn-in | derived | `126 + k + skip` = 147 (`m21`), 189 (`m63x`) loaded nights | 11 |
| new line, bond level | `STALE_WINDOW` | `history_nights` under 21 | 11, 13 |
| floor | `MIN_OAS_ABS`, `MAX_OAS_FLOOR`, `FLOOR_FRAC` | 15.0 bp, 40.0 bp, 0.35 of the fine-cell median spread tonight | 7 |
| max gap nights | `MAX_GAP_NIGHTS` | 3 (previous spread lookback; roll-up weight lookback; coverage lookback) | 7, 16 |
| stale mask | `STALE_WINDOW`, `STALE_MIN_MOVES` | 21 nights, at least 10 moves (tonight included) | 8 |
| stale warm-up | `STALE_WINDOW - 1` | the first 20 loaded nights never pass | 8, 11 |
| jump | `JUMP_MAD_MULT`, `JUMP_MIN_ABS` | 6.0, 0.10 (flag when strictly greater than max(6 MAD, 0.10)) | 8 |
| cumulative jump | `CUM_WINDOW`, `CUM_MAD_MULT`, `CUM_MIN_ABS` | 5 (4 preceding nights plus tonight), 6.0, 0.15 | 8 |
| jump population minimum | `JUMP_CELL_MIN` | 5 contributors (fine cell, else wide cell; also 5 cumulative values) | 8 |
| MAD scale | `MAD_SCALE` | 1.4826 | 8, 14 |
| robust N | `ROBUST_N` | 8 (median when 8 or fewer other issuers remain) | 9 |
| winsorize indices | inline | `max(1, floor(0.1(N-1)))`, `min(N-2, ceil(0.9(N-1)))`, zero-based; none for `N < 3` | 9 |
| n0 (benchmark blend) | `N0_BENCH` | 5 | 9 |
| thin cell outer only | `THIN_CELL_OUTER_ONLY` | `n_eff` below 4 gives `w = 0` | 9 |
| min outer | `MIN_OUTER` | 6 (outer benchmark; `B_fine` alone) | 9 |
| count median window | `N_MEDIAN_WINDOW` | 63 preceding nights plus tonight | 9 |
| min ttm (effective) | `MIN_TTM_YEARS` | 1.0 (`ttm_years >= 1.0` passes) | 13 |
| min spread duration | `MIN_SPREAD_DUR` | 1.0 (`spread_dur >= 1.0` passes) | 8, 10, 13 |
| call window | `CALL_EXCL_CAL_DAYS` | 91 calendar days (`t < next_call_date <= t + 91` blanks) | 13 |
| rho floor | `RHO_FLOOR` | -0.50 (on the blended rho) | 12 |
| default pooled rho | `RHO_DEFAULT` | -0.3 (no loaded bond has a `rho_own` in the window) | 12 |
| rho SE | `RHO_SE_NUM` | `1 - 3 r^2 + 4 r^4`, divided by `n_obs`, at the universe pooled rho | 12 |
| bracket minimum | `BRACKET_MIN_FRAC` | 0.1 (a bracket at or below 0.1 k blanks) | 12 |
| pooled rho minimum | inline | 5 issuers per tercile key and per wide key | 12 |
| spread terciles | inline | per wide cell, `N` bonds sorted by spread ascending, ties by ISIN string ascending; position `p` from 0 is tercile 0 if `p < floor(N/3)`, 1 if `p < floor(2N/3)`, else 2 (20 bonds: 6, 7, 7); fewer than 3 bonds: no tercile 0 | 12 |
| z min other | `Z_MIN_OTHER` | 5 reference issuers | 14 |
| rating scale reference cell | `Z_MIN_OTHER` | per issuer: fine cell with at least 5 other issuers with a scored line, else wide cell with at least 5, else the issuer is left out; `n_rating` = issuers that entered | 14 |
| n0_z | `Z_N0_INIT`, `Z_N0_MIN`, `Z_N0_MAX` | init 12, clip(2 / tau_s^2, 5, 60) | 14 |
| n0_z sampling | `Z_SAMPLING_CONST` | 1.3 / n | 14 |
| n0_z cell minimum | `Z_MIN_OTHER + 1` | 6 issuers per cell | 14 |
| n0_z minimum cells | inline | 20 cells with scale above 0 | 14 |
| tau_s^2 floor | inline | 0.0001 | 14 |
| median-centre variance | `Z_MEDIAN_VAR` | 1.571 (pi/2): scale factor `sqrt(1 + 1.571 / n)`, rating term `sqrt(1 + 1.571 / n_rating)`, rating-scale deviation divided by `sqrt(1 + 1.571 / n_other)` | 14 |
| clip | `Z_CLIP` | 3.0 | 14 |
| step window | inline | 3 consecutive nights | 15 |
| step share C, A | `CONF_STEP_SHARE_C`, `CONF_STEP_SHARE_A` | 0.80, 0.60 | 15 |
| step test minimum | `CONF_STEP_MIN_SCORE` | 2.0 on `max(abs(raw score), abs(z))` | 15 |
| jump events for C | `CONF_JUMP_EVENTS_C` | 2 | 15 |
| reference for A | `CONF_REF_A` | 8 other issuers, fine cell | 15 |
| within-issuer weight | inline | amount x spread_dur x spread; tonight for benchmarks, window start for roll-up | 9, 16 |
| reading: gap threshold | `READ_GAP_BP` | 5.0 bp (cheap at or above +5.0, rich at or below -5.0) | 4 |
| reading and trend: move threshold | `READ_MOVE` | 1.0 (up at or above +1.0, down at or below -1.0) | 4 |
| blank reasons | `REASON_ORDER`, `REASON_TEXT` | section 13 order; section 4 text | 4, 13 |
| tables label | `METHOD_VERSION` | "6.0", written to `tables["method_version"]`; `tables["as_of"]` is the scored night as an ISO date; read by no computation | 17 |

## Appendix B: oasmom.py

```python
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
```
