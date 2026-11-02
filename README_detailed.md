# OAS Relative Momentum

A score for any bond in the IG index, and for any issuer, measuring how its spread has moved relative to comparable bonds over two windows: the last 21 trading nights (Mom21, about a month) and the 42 trading nights before those (Mom63x, about the two months before that). It is computed on request from the current index table and spread history pulled from Bloomberg. The relative value engine (the repo's peer-group model) publishes a `Gap` column in basis points for the same bonds; this score is read beside it.

---

## What it measures

Credit momentum at the index level is mostly beta. This score removes that and asks a narrower question: over a given window, did this bond's spread tighten or widen more than bonds that look like it?

"Bonds that look like it" are the bond's peer group from the relative value engine: the engine's cells of similar bonds, defined under Terms.

A positive value means the bond tightened relative to its peers over the window. A negative value means it widened relative to them.

**Terms used throughout.**

- **Median.** Every median in this document is the usual one: the middle value for an odd count, the mean of the two middle values for an even count.
- **Bond, line, issuer.** A bond is one security in the index. A line is one bond of an issuer, the word used when an issuer has several. The issuer is the ultimate parent. All lines of one issuer count as one observation when they appear in a peer group.
- **Spread.** The desk's par-equivalent OAS in basis points.
- **Current index table.** The table in the repo, refreshed each morning, with one row per constituent: spread duration, years to effective maturity, amount outstanding, call status and next call date, rating bucket, issuer, and the three peer-group ids. These values are used as they stand on the request date for every night in the window.
- **The pull.** The spread history fetched from Bloomberg for the bonds a request needs: for each bond the daily par-equivalent spread, from the desk's existing Bloomberg pull and `ParEquivOAS` process. Which bonds a request pulls is under How a request is answered.
- **Trading night, request night, t, loaded nights, history.** A trading night is a date on which at least half of the pulled bonds that are in the current table and have a spread somewhere in the pull have a spread; other dates are dropped for every bond. A trading night with no value for a bond is a gap for that bond. Only the latest 215 trading nights are kept; older nights are ignored. The kept nights are the loaded nights. The request night, written `t`, is the latest trading night; because the rule counts the pulled bonds, it is the latest date on which most of them have a spread and can differ from the calendar date. `t-20` is 20 trading nights before `t`. A bond's history (`history_nights` in the output, N in "N nights of history") is the number of loaded nights from the bond's first spread that passes the data checks through `t`, inclusive. When most of the pulled bonds are recent issues, the majority rule can drop early dates for every bond, which shortens every bond's history; the `as_of` and `request_night` fields show the night that was scored and the counts behind it.
- **Windows.** Mom21 covers the 21 trading nights `t-20` to `t`. Mom63x covers the 42 trading nights `t-62` to `t-21`: the 63 nights back from `t`, less the latest 21. The two windows do not overlap.
- **Weight.** The weight of a line is its amount outstanding times its spread duration times its spread.
- **Peer group.** The cells the relative value engine assigns to each bond in the current table, at three nested levels: the fine cell (sector, subsector, structure, seniority, rating bucket, maturity bucket), the wide cell (the fine cell without subsector), and the level-3 cell (the wide cell without sector). The peers of a bond are the current members of its cells, and a bond keeps its current cells for every night in the window. When the table moves a bond to a new cell (a rating change, a maturity bucket roll), its whole history is scored against the new cell's members from that morning, including nights when it traded as a member of the old one (see "When the table moves a bond to new peers" under How to read the outputs).
- **Jump.** A one-night change, or a cumulative change over five nights (tonight and the four before), that sits far outside what the bond's peer group did (the exact test is under Contributing line). A night on which a bond carries a jump flag is a flagged night. Consecutive flagged nights are one jump event.
- **Raw score.** A bond's relative spread change over a window (its percentage spread change less its peer group's benchmark change) divided by the bond's own volatility of relative changes, with the sign flipped so that tightening is positive. A scored line is a line with a raw score in the window. The exact rules are under Definitions.
- **Reference group.** For a bond in a window: the other issuers (never the bond's own) with at least one scored line in the bond's fine cell, if there are at least 5 of them; otherwise the same in its wide cell; otherwise there is no reference group and the bond has no standardised score. The reference group is what the bond's raw score is compared with.
- **Month-end tables.** Statistics for the whole index, computed by the month-end run and used by single requests: the pooled autocorrelation by wide cell and spread tercile (a typical lag-1 autocorrelation of one-night relative changes; a spread tercile is a third of the bonds of a wide cell by spread on the request night: lowest, middle, highest); `w_own`, the weight a bond's own autocorrelation gets against the pooled value; the rating-bucket scales, centres and issuer counts (the rating-bucket scale measures how widely issuers' scores spread within the bond's rating bucket; the rule is under Standardised score); and `n0_z`, the weight, in issuer-count units, that the rating-bucket scale gets against the reference group's own scale when the two are combined. The tables carry the date they were computed on (`as_of`).

---

## How it is built

**Spread moves in percentage terms.** A 10bp move on a 60bp bond and a 50bp move on a 300bp bond are the same move per unit of spread risk, and the score treats them that way. The spread is the desk's par-equivalent OAS. For very tight bonds the denominator is floored relative to the peer group so tiny spreads do not produce outsized percentages.

**Subtract the peer group.** The benchmark is built from the other issuers in the group, never from the bond's own issuer. Within an issuer, lines are weighted by size and spread risk (the weight above); across issuers, each counts once. When a group is small, the benchmark is blended with a broader group, with more weight on the narrow group as it gets deeper. The average is a robust one (a median when 8 or fewer other issuers remain, a clipped mean otherwise), so a single issuer's one-night re-mark has a bounded effect on the benchmark. Bonds with frozen marks (spread changed on fewer than 10 of the last 21 nights), spread duration under 1.0, or a jump are kept out of the benchmark that night.

**Scale by the bond's own noise.** Each bond's relative move over the window is divided by its own historical volatility of relative moves, measured over the 126 trading nights (six months) ending immediately before that window begins. This is done separately for each window, so Mom21 and Mom63x each have their own volatility from their own six-month period. Night-to-night spread changes partly reverse (a negative lag-1 autocorrelation: a rise tends to be followed by a fall), and the volatility estimate accounts for that rather than assuming square-root-of-time scaling.

**Standardise within the reference group.** The displayed number is the bond's raw score less the median raw score of the issuers in its reference group, divided by that group's scale (1.4826 times the median absolute deviation of those issuers' scores about their median). The scale is pulled toward the rating-bucket scale when the group is small.

**Grade each window.** Each score carries an A, B or C that says whether the move was spread across the window or came in one step, whether jump events sit inside it, and how deep the reference group was.

**Roll up to the issuer.** Bond scores are averaged to one number per issuer, weighted by each line's amount outstanding times spread duration (both from the current table) times its spread on the first night of the window, with the worst line's grade.

---

## The columns

| Column | Definition |
|---|---|
| **Gap (+ wide)** | The relative value engine's spread gap to the peer group, in basis points. +10 means the bond trades 10bp wider than its peer group; -10 means 10bp tighter. Passed through for display; not computed here. |
| **Mom21 (+ tightened)** | How much the bond's spread moved against its peer group over the last 21 trading nights, in units of that bond's own normal monthly noise. +1.5 means the bond tightened relative to its peers by one and a half times its ordinary monthly move; -1.5 means it widened relative to them by the same amount; 0 means it moved with the median issuer in its reference group. Computed as the relative spread change over `t-20` to `t`, divided by the bond's own volatility, then centred on and scaled by the issuers in its reference group (Standardised score, under Definitions). |
| **Conf21** | How far the Mom21 number can be taken at face value. A: the move built up across the month, no jump event started inside it, and a deep reference group stands behind the comparison. B: one of those is weaker, such as a thinner reference group or one jump event. C: the number is driven by a single event, so read the event rather than the number. Exact rules under The grade, below. |
| **Mom63x** | The same statistic over the two months before the Mom21 window: the 42 trading nights `t-62` to `t-21`. +1.5 means the bond tightened relative to its peers over those two months by one and a half times its ordinary move for a window of that length. The name is the 63 nights back from `t`, less the latest 21. The two windows do not overlap. |
| **Conf63x** | The same grade for Mom63x, judged on its own two-month window. The two grades differ when an event sits in one window and not the other: a re-mark three weeks ago lowers Conf21 and leaves Conf63x at A. |
| **Reading** | A label from Gap and Mom21 together, or the reason Mom21 is blank (With the gap, below). |
| **Trend** | A label from Mom21 and Mom63x together (Mom63x beside Mom21, below). |

Every term in that table is defined in this document.

---

## How to read the outputs

Mom21 and Mom63x measure the **change** in a bond's spread relative to its peers over a window, not where the bond sits against its peers today. Where it sits today is the relative value engine's `Gap`. The two are read together.

**The scale.** Each Mom value is a z-score: the bond's raw score less the median raw score of the issuers in its reference group, divided by that group's scale. Zero means the bond moved in line with the median issuer of its group over the window. Plus or minus 1.0 is the point at which a move earns a label in the Reading and Trend columns; about one in three lines in a quiet book sits at or beyond it on any night, so the label marks the top third of moves by size and is not a rare event. Plus or minus 2 or beyond is unusual both for that bond's own history and against the dispersion of its reference group. Positive is relative tightening (the bond outperformed its peers); negative is relative widening (it underperformed). The displayed value is clipped at plus or minus 3.

Gap and Mom have opposite sign conventions: a positive Gap is wide (cheap), a positive Mom is relative tightening (outperformed). The Reading label carries the direction in words so the two numbers need not be reconciled by eye.

**With the gap.** The `Reading` column labels each scored bond from its `Gap` and its Mom21. Cheap is a gap of +5bp or wider, rich is -5bp or tighter, fair is in between. Mom21 counts as a move at +1.0 or beyond (outperforming) or -1.0 or beyond (underperforming) in either direction; inside that it is flat. Both thresholds are inclusive and apply to the unrounded values: a Mom21 of +0.96 shown as +1.0 is flat, and a Gap of +4.6 shown as +5 is fair.

| Gap | Mom21 | Reading |
|---|---|---|
| cheap | +1.0 or above | cheap and outperforming |
| cheap | -1.0 or below | cheap and underperforming |
| cheap | between | cheap and flat |
| rich | +1.0 or above | rich and outperforming |
| rich | -1.0 or below | rich and underperforming |
| rich | between | rich and flat |
| fair | +1.0 or above | fair and outperforming |
| fair | -1.0 or below | fair and underperforming |
| fair | between | fair and flat |

The words describe the percentage move relative to peers. About one in three lines in a quiet book carries a move label on any night. When Mom21 is blank the `Reading` column carries the display text of Mom21's blank reason instead (the reasons and their order are listed under Which bonds get a score), never Mom63x's. When Mom21 is present and Gap is blank, `Reading` is blank.

A bond showing 20bp cheap to its curve with Mom21 of +1.5 has tightened against its peers over the past month by more than an ordinary month for that bond. Whether that is the start of the gap closing or a move that gives back is not something the statistic says. The same bond with Mom21 of -1.5 has widened against them; it is cheap and still underperforming.

**On its own.** A large value with grade A is a bond that has moved against its peers by 2 or more in z, with no jump event starting inside the window and a deep reference group. Mom21 of -2.5 and Conf21 A on a name with no level view is still worth a look.

**When the table moves a bond to new peers.** For a fallen angel the first month's Mom21 compares its old-bucket behaviour to its new-bucket peers, and the downgrade move itself usually carries a jump event, so the grade is B or C. Read the Reading against the new peers with that in mind until the event has left Mom21.

**Mom63x beside Mom21.** Mom63x covers the two months before the Mom21 window. The `Trend` column labels the pair, using the same threshold of 1.0 for a move on each window, inclusive and on unrounded values.

| Mom63x | Mom21 | Trend |
|---|---|---|
| move | same direction | continuing |
| move | opposite direction | reversing |
| flat | move | new |
| move | flat | faded |
| flat | flat | flat |
| blank | any | blank |
| any | blank | blank |

Read `Trend` with the sign of Mom21: "continuing" on a negative Mom21 is sustained relative widening; "reversing" on a positive Mom21 is a bond that widened for two months and has started to tighten back.

Mom21 is a 21-night sum with no decay. An event stays in it at full weight for exactly 21 nights and then moves into Mom63x for the following 42, so a bond's Reading can change on the 22nd night after an event with no new information; the Trend column is where the event shows from then on. The displayed value is clipped at plus or minus 3: a bond at the clip shows -3.0 whether its underlying score is -4 or -9.

**The grade.** A: no jump event started inside the window for this bond; when the move was large enough to test (2.0 or more in the raw score or in the standardised score), no three consecutive nights made up 60% or more of it; and the reference group is the bond's own fine cell with at least 8 other issuers. B: the reference group is the wide cell or a fine cell with 5 to 7 other issuers, or 60% to 80% of a large move came in three nights, or one jump event started in the window. C: two or more jump events started in the window, or 80% or more of a large move came in three nights. Read B as a shallower reference group, a partly concentrated move or one jump event, and C as one event behind this window's number: read the event. One re-mark can flag up to five consecutive nights and counts as one jump event. The grade does not test whether marks are fresh; a line marked on 10 of the last 21 nights still scores. A labelled move of 1.0 or more whose raw score and standardised score are both under 2.0 in size is not tested for concentration, so it can be a single night's move and still grade A. Conf answers "was the move clean"; the label answers "was it large".

**Output layout.** Columns are presented in this order. `Gap` is positive wider than peers (cheap) and negative tighter (rich); Mom values are positive for relative tightening. `Reading` is the label from the gap table above, or the display text of Mom21's blank reason when Mom21 is blank. `Trend` is the label from the Mom63x table above, blank when either window is blank.

| ISIN | Issuer | Gap (+ wide) | Mom21 (+ tightened) | Conf21 | Mom63x | Conf63x | Reading | Trend |
|---|---|---|---|---|---|---|---|---|
| US00000AAA11 | Issuer A | +18 | +1.6 | A | +0.4 | A | cheap and outperforming | new |
| US00000BBB22 | Issuer B | +25 | -2.3 | A | -1.3 | B | cheap and underperforming | continuing |
| US00000CCC33 | Issuer C | -15 | -1.4 | B | +2.1 | A | rich and underperforming | reversing |
| US00000DDD44 | Issuer D | -8 | +1.2 | A | | | rich and outperforming | |
| US00000EEE55 | Issuer E | +12 | | | +0.9 | A | window not covered | |

Row 1 tightened against peers over the last month after no move of size over the prior two. Row 2 widened sharply against peers over the last month with no jump event starting inside the window, extending the prior two months. Row 3 widened against peers over the last month after a strong two-month tightening. Row 4 is a recent issue with enough pulled history for Mom21 but not yet for Mom63x, so Mom63x, Conf63x and Trend are blank. Row 5 has gaps in the last 21 nights that leave the Mom21 window under 90% covered, so `Reading` carries the reason; Mom63x is shown because its window is covered, and Trend is blank because Mom21 is blank.

Issuer output has the columns Issuer, Gap, Mom21, Conf21, Mom63x, Conf63x, Reading, Trend, n, max_w, pct_pos, sd, coverage. `Gap` is the engine's issuer gap. `Mom21` and `Mom63x` are the issuer-level values, and `Reading` and `Trend` follow the same rules on them. `n`, `max_w`, `pct_pos`, `sd` and `coverage` are the Mom21 window's number of lines, largest weight share, share of lines with positive raw score, standard deviation of raw scores, and coverage. An issuer in the current table always has a row on an issuer request and on the whole index; when it has no scored line in the Mom21 window, `Mom21`, `Conf21`, `n` and the other roll-up fields are blank (`n` is 0) and `Reading` shows "no line scored". An issuer that is not in the current table has no row and shows "not in the table". Issuer rows are returned for an issuer request and for the whole index, and not for a request that lists ISINs.

---

## Definitions

Medians are as defined under Terms.

**One-night change.** The bond's percentage change in spread from its previous pulled value, `(S - S_prev) / max(S_prev, floor)`, where `S` is the bond's spread tonight and `S_prev` its previous pulled spread. The previous value is the bond's last pulled spread within the 3 trading nights before; with none, the bond has no change that night. The floor is 35% of the median spread that night of the bonds in the bond's fine cell, no lower than 15bp and no higher than 40bp. A change from a value 2 or 3 nights back is a multi-night change and uses the floor of its last night.

**Contributing line.** A bond's one-night change enters its issuer's change in the benchmark (the bond is a contributing line that night) only when: the change is a single-night change; the bond's spread changed (exact inequality of the pulled spread) on at least 10 of the last 21 trading nights, tonight included, and the night is at least the 21st loaded night, so the first 20 loaded nights never qualify; spread duration is at least 1.0; and the change is not a jump. A line that meets the first three conditions is a contributor (the jump test counts these); a contributor that is not a jump is a contributing line (the benchmark and the blend count these). A jump is a one-night change further than the larger of six cell-MADs and 0.10 from the cell's median change that night, or a cumulative change (the sum of the bond's relative changes on the previous four nights plus tonight's deviation from the cell median) further than the larger of six cell-MADs of those cumulative values and 0.15 from their median. MAD is 1.4826 times the median absolute deviation. The cell is the bond's fine cell when it holds at least 5 contributors, otherwise its wide cell when that holds at least 5; with neither, no jump test is made. The jump tests are made for contributors only; a bond that is not a contributor that night is never flagged. A jump keeps the bond out of its peers' benchmark that night only; the bond's own score still includes the move, a jump on the request night does not remove the bond from scoring, and the flagged nights count toward its grade as jump events.

**Benchmark change (one night).** Built from the other issuers in the bond's peer group, never from the bond's own issuer. Each issuer's change is the mean of its contributing lines' one-night changes weighted by amount outstanding times spread duration times that night's spread. Across issuers, each counts once. The aggregate is the median when 8 or fewer other issuers remain, otherwise the mean after clipping each issuer's change to the values at sorted positions `max(1, floor(0.1 (N-1)))` and `min(N-2, ceil(0.9 (N-1)))`, counting from 0, of the `N` issuer changes in the whole cell (no clipping when `N` is under 3). The fine-cell aggregate is blended with an outer aggregate: the wide cell excluding the bond's own issuer and every issuer with a contributing line in the bond's fine cell, or, if that leaves fewer than 6 issuers, the level-3 cell excluding the bond's own issuer and every issuer with a contributing line in the bond's wide cell, or, if that also leaves fewer than 6, none. The weight on the fine cell is `n / (n + 5)`, where `n` is the median, over tonight and the 63 preceding nights, of the number of issuers with a contributing line in the fine cell, less one (not rounded); when `n` is below 4 the outer aggregate is used alone. With no outer aggregate the fine cell is used alone only when it has at least 6 other issuers; otherwise the bond has no relative change that night.

**Relative spread change (one night).** The bond's one-night change less the benchmark change for its peer group that night. If the bond's previous value is more than one night back (up to three), its relative change is `(1 + change) / product - 1`, where `product` is the product of one plus the benchmark change over each night the change spans (the last night and the nights between). For a spanned night on which the bond's issuer was not in the benchmark, the benchmark of its cells with no issuer excluded is used. The change counts only when a benchmark exists on every night it spans, spread duration is at least 1.0 and the bond's spread changed on at least 10 of the last 21 nights.

**Window change.** The sum of the bond's one-night relative changes over the window. A multi-night change counts only if all the nights it spans lie inside the window. At least 90% of the window's nights must be covered (19 of 21, 38 of 42); otherwise the window is blank.

**Volatility.** Computed separately for each window, from the 126 trading nights (six months) ending immediately before that window begins. The two periods are different and neither overlaps its own window. With `t` the request night:

| Window | Scoring window | Volatility period |
|---|---|---|
| Mom21 | t-20 to t (21 nights) | t-146 to t-21 (126 nights) |
| Mom63x | t-62 to t-21 (42 nights) | t-188 to t-63 (126 nights) |

For each: `var1` is the population variance (divided by the count) of the bond's one-night relative changes over its volatility period. `rho_own` is the lag-1 autocorrelation of those same changes over the same period. `rho = max(-0.50, w_own x rho_own + (1 - w_own) x rho_pool)`, where `w_own` is the month-end weight on the bond's own autocorrelation (computed from the pulled cells when no tables are passed; a bond with no `rho_own` uses `rho_pool`). `rho_pool` is the pooled autocorrelation: the median across issuers (each issuer's median over its lines in the key) for the bond's wide cell and spread tercile when that key has at least 5 issuers, else for its wide cell with at least 5 issuers, else for the whole index; with no autocorrelation anywhere in the window it is -0.3. The spread terciles of a wide cell are set on the request night: its `N` bonds sorted by spread ascending (ties by ISIN, ascending); the first `floor(N/3)` are tercile 0, the next up to `floor(2N/3)` tercile 1, the rest tercile 2, so 20 bonds split 6, 7, 7, and a cell with fewer than 3 bonds has no tercile 0. The window volatility is `sd_k = sqrt(var1 x (k + 2(k-1) rho))` with `k` = 21 or 42. At least 90% of the 126 nights (114) must have a single-night relative change, otherwise that window is blank. If `k + 2(k-1) rho` is at or below `0.1 k` the window is blank.

**Raw score.** Minus the window change divided by the volatility. The sign flip makes relative tightening positive.

**Standardised score (the displayed Mom21 and Mom63x).** The reference group for a window is defined under Terms. Each issuer in the group contributes the median of its lines' signed raw scores. The group's centre is the median of those issuer values, and its scale is 1.4826 times the median absolute deviation of the issuer values about the centre. The rating-bucket scale is measured over the issuers with a scored line in the bond's rating bucket, from within-cell deviations. Each such issuer is compared with its own reference cell, fixed by its first scored line in ISIN order: that line's fine cell if the cell has at least 5 other issuers with a scored line in the window, else its wide cell if that has at least 5, else the issuer is left out. The issuer's deviation is its median score in that cell less the median of the other issuers' values there, divided by `sqrt(1 + 1.571 / n_other)` (`n_other` the number of other issuers), which takes out the sampling noise of that median. The rating-bucket scale is 1.4826 times the median absolute value of these deviations, and `n_rating` is the number of issuers that entered. A rating bucket in which no issuer entered has no scale, and its bonds have no standardised score (shown as zero variance). The two scales are combined in variance form: the rating-bucket scale is first multiplied by `sqrt(1 + 1.571 / n_rating)`, and then scale squared = `(n x group scale squared + n0_z x rating scale squared) / (n + n0_z)`, where `n` is the number of issuers in the reference group and `n0_z` is the month-end weight (estimated from the pulled cells when no tables are passed: between 5 and 60, 12 when fewer than 20 cells qualify; the estimate is in the implementation guide, section 14). The scale, the square root of that quantity, is then multiplied by `sqrt(1 + 1.571 / n)`, which allows for the sampling noise of a median of `n` values. The standardised score is the raw score less the group's centre, divided by that scale. A move shared by the whole reference group does not appear in any member's score; it belongs to the sector view, as it does for Gap. The displayed value is clipped at plus or minus 3; the unclipped value is retained.

**Confidence grade.** For a window: C if two or more jump events start inside the window, or the largest absolute sum of the bond's relative changes over 3 consecutive nights inside the window is 80% or more of the window change. A if there are no jump events, that 3-night share is under 60%, and the reference is the fine cell with at least 8 other issuers. B otherwise. A jump event is a run of consecutive flagged nights, counted in the window that holds its first flagged night; a run that began before the window counts nothing in it. The 3-night test is applied only when the raw score or the unclipped standardised score is at least 2.0 in size; for a smaller move the test counts as passed. A window with no displayed score has no grade. Conf21 is computed on the Mom21 window and Conf63x on the Mom63x window, so the two grades differ whenever a jump event or a one-step move sits in one window and not the other. The reference depth is counted separately for each window from the issuers that have a score in that window, so it can differ between Conf21 and Conf63x.

**Issuer columns.** For each window, the weighted mean of the issuer's lines' displayed scores, weight amount outstanding times spread duration times spread on the first night of the window (or, if the line has no spread that night, its latest spread in the 3 nights before; a line with none is left out and counted as dropped). The issuer's confidence grade for that window is the worst grade among the lines that entered the mean. Reported alongside: number of lines, largest line's weight share, share of lines with positive raw score, population standard deviation of raw scores (blank for one line), and coverage: the weight of the lines that entered, as a share of the weight of all the issuer's lines in the pull that have a spread on the window's first night or in the 3 nights before, each at its latest such night. Coverage is at most 1. An issuer with a line in the current table and no scored line in a window has its row for that window with the score, grade and roll-up fields blank, the number of lines 0 and the reason "no line scored".

---

## Which bonds get a score

A bond gets a score for a window when all of the following hold on the request night:

- it has at least 21 nights of history of its own, a single-night change that night (a spread on the night before) and has moved on at least 10 of the last 21 nights; a jump on that night does not remove it;
- years to effective maturity (to the next call for fixed-to-float and hybrid structures) at least 1.0, and spread duration at least 1.0;
- call status known and no call date within 91 calendar days of `t`: a bullet passes whatever its call date; a callable bond passes only with a next call date later than `t` plus 91 days (a date from `t+1` to `t+91` is blocked); a callable bond with no next call date or one on or before `t`, and any bond with call status unknown, is blocked as call status unknown;
- the window has at least 19 covered nights of 21 (38 of 42 for Mom63x) and the 126-night volatility period has at least 114 nights with a change, and the volatility bracket is above `0.1 k`;
- the reference group has at least 5 other issuers with a score in that window, and the standardisation scale is above zero.

A bond without a score shows blank, never zero, with the reason recorded. One reason is shown for the bond (or, for a window-level reason, for the window); when several bond-level reasons apply, the first in this list is shown:

| Display text | When |
|---|---|
| not in the table | the ISIN is not in the current table |
| no spread tonight | the ISIN is in the current table and has no spread on the request night: this includes a bond with nothing in the pull and a bond whose spreads fall only on dates dropped as non-trading |
| bad data row tonight | the request night's row failed the data checks (missing field, non-finite or non-positive value, bad call status or call date type) |
| new line (N nights of history) | N is the bond's history. Either N is under 21, so the bond has no frozen-mark history yet (no score in either window); or N is under 155 for Mom21 (197 for Mom63x) and the window or the volatility period is not covered |
| multi-night change tonight | the bond's previous spread is 2 or 3 nights back |
| stale marks (fewer than 10 changes in the last 21 nights) | the bond has at least 21 nights of history and either its spread changed on fewer than 10 of the last 21 nights or it has no spread in the 3 nights before |
| maturity under 1 year | years to effective maturity under 1.0 |
| call status unknown | see the call rule above |
| call within 91 days | next call date from `t+1` to `t+91` |
| spread duration under 1.0 | spread duration under 1.0 |
| not enough pulled history | the pull holds fewer than 147 trading nights for Mom21 (189 for Mom63x) |
| window not covered | under 90% of the window's nights are covered (fewer than 19 of 21, 38 of 42) |
| volatility period not covered | under 114 of the 126 volatility nights have a change |
| zero variance | the volatility period has zero variance, or the reference group's scale is zero, or the bond's rating bucket has no scale |
| volatility bracket too small | `k + 2(k-1) rho` at or below `0.1 k` |
| too few reference issuers | neither the fine nor the wide cell has 5 other issuers with a score in the window |
| no line scored | issuer rows only: no line of the issuer has a score in the window |

History required: a window scores when it has at least 19 covered nights (Mom21) or 38 (Mom63x) and at least 114 of the 126 volatility nights have a change, and the later tests pass. For a bond present from the first night of the pull with a spread on every night, that means a pull of 155 trading nights for a first Mom21 and 197 for a first Mom63x (the first 20 nights of the pull are warm-up for the frozen-mark test, which leaves 114 of the 126 volatility nights usable, the minimum). A bond with 167 nights for Mom21 and 209 for Mom63x has a fully populated volatility period (126 usable nights) and can miss up to 12 of them and still score. A bond listed part-way through a 215-night pull with a spread on every night since needs only 10 changes before the frozen-mark test passes, so it scores a first Mom21 at 145 nights of its own history and a first Mom63x at 187; one night fewer shows new line. The output fields `covered` and `n_var` give a bond's counts against the minimums (19 or 38 covered, 114 volatility nights).

---

## How a request is answered

For a bond, an issuer, a list of bonds or the whole index: the engine reads the current table to find every bond in the targets' fine, wide and level-3 cells, all of them whatever their number, and pulls those bonds' spread history from Bloomberg. It keeps the trading nights (a date counts when at least half of the pulled bonds that are in the table and have any spread in the pull have one on it), keeps the latest 215, and takes the latest as the request night. The request night is therefore the latest trading night by that majority rule, which can differ from the calendar date: on an evening when part of the market has printed and the rest has not, the latest date is not a trading night until half of the pulled bonds carry it, and two requests with different pulls, or a request and the month-end run, can be scored on different nights. It then derives the one-night relative changes night by night and computes the columns as of the request night. Compute time scales with the number of bonds pulled: a request for one bond that pulls 1,000 bonds takes about 9 seconds after the pull, and a full batch on a 7,000-bond test universe takes about 160 seconds (CPython 3.13, 2 CPUs); the 10,000-bond index takes proportionally longer.

Check the scored night before reading a number. Every bond row and every issuer row carries `as_of`, the request night it was scored on, and the request returns `request_night` with the same date, the number of bonds pulled (`n_pulled`), the number of those with a spread on that night (`n_spread`) and the number with a spread anywhere in the pull (`n_any`). The month-end tables carry their own `as_of`.

A request for an issuer scores all its lines and returns the issuer row, scored or not. A request for a list of bonds returns bond rows only. An empty list, an unknown ISIN or an unknown issuer returns "not in the table" (the ISIN or issuer is not in the current table); a bond in the table with no spread on the request night returns "no spread tonight"; a bond whose request-night row failed the data checks returns "bad data row tonight". A bond or issuer request, a whole-index request and the month-end run give the same reason for the same bond.

At each month-end the engine pulls every constituent's history, runs over the whole index, and stores the month-end tables that single requests use. A request given the tables of a month-end run on the same night returns the numbers that run returns for the same bonds and issuers; without them it computes the pooled autocorrelation, `w_own`, the rating-bucket scales and `n0_z` from the pulled cells.

---

## Further reading

The implementation guide (integration, build order, method reference, parameter table, reference implementation) is `oas_relative_momentum_spec.md`.
