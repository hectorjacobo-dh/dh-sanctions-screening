# Regression check — NOT a calibration

Counts only — no partner or UBO names. Per-name detail is written to
`data/calibration-detail.csv`, which is gitignored.

> **This is not validation, and must not be cited as it.**
>
> The answer set was recovered from the engine's own archived alerts, so it
> contains only names the engine had already matched. A name it missed
> produced no alert and left no trace — of the 234 names screened in
> September, 230 appear in alerts and the 4 that matched nothing are gone.
> Recall below is therefore 100% by construction.
>
> What it does show: after alias scoring, the EU regrouping and the identity
> work, today's engine still finds everything the earlier version found.
> That is a regression check, not agreement with the humans.
>
> Real calibration needs the team's own file:
> `python calibrate.py --from-file "DH Group_Restaurants Matches in Sanctions Lists.xlsx"`

## Answer set

- Source: `DH Group_Restaurants Matches in Sanctions Lists.xlsx`
- Names recovered: **230**
- Rows in the original file: 379
- Alerts raised at the time, at 85%: 779

Precision is not measurable in either mode: that needs the names the team
screened and cleared, and no record of those survives.

## Recall by threshold

| Threshold | Found | Missed | Recall |
|---|---|---|---|
| 70% | 230 | 0 | 100.0% |
| 75% | 230 | 0 | 100.0% |
| 80% | 230 | 0 | 100.0% |
| 85% | 230 | 0 | 100.0% |
| 90% | 230 | 0 | 100.0% |
| 95% | 230 | 0 | 100.0% |

## Best-score distribution

| Band | Names |
|---|---|
| 100 | 230 |

## How to read this

A miss is not symmetrical with a false positive. A false positive costs an
officer ten minutes; a miss means a designated party was onboarded and
nobody knows. Every name below the operating threshold should be examined
individually before this engine is relied upon as the control of record.

Names scoring below 70%: **0**.

A low score does not automatically mean the engine is wrong. The manual
process produced false positives of its own, so a name it flagged may
simply not be a designated party. Each one has to be read.
