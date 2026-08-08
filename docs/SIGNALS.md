# Signals — what actually predicts that nobody will watch a film

> Derived by replaying a large Tautulli history against the library it belongs to, on
> one active Plex server. Every number here was measured, and is quoted as a ratio: the
> **shape** generalizes, the absolute values are that server's and yours will differ.
>
> **Read the section on population first.** It is the part that took two wrong turns to
> get right, and it will bite you too.

## The thing the score is trying to predict

**"Nobody will watch this in the next year."** That is the *only* question the score
answers. It is a **risk** estimate.

It is emphatically *not* "how much space would I get back". That is a **reward**, and
conflating the two is a real error that shipped in the first version.

---

## ⚠️ The population trap

**Lift is meaningless unless the baseline is computed over the population the scorer
actually judges.** This was got wrong twice, and each time it produced a confident,
completely wrong conclusion.

**Wrong (attempt 1):** the baseline was computed over films that had *ever been
played*, while the condemned set was mostly films that had *not*. Conclusion: "the
scorer is worse than random." False.

**Wrong (attempt 2):** the baseline was computed over every `rating_key` in Tautulli's
history. But that table also contains **items long since deleted from the library**,
which by definition are never watched again. Their zeroes dragged every bucket down
several-fold and made a *working* scorer look like it had strongly negative lift.

**Right:** compute the baseline over exactly what the scorer scores — films currently
in the library, managed by an *arr, with a file on disk.

The two curves, side by side. The error is not uniform: it is worst in
exactly the buckets the policy cares about most.

| Dormant for | Over all history keys (**wrong**) | Over the actual library (**right**) |
|---|---|---|
| 0–365d | ~44% | **~61%** |
| 365–548d | ~31% | ~31% |
| 548–730d | ~30% | ~32% |
| 730–1095d | ~19% | **~30%** |
| 1095–1825d | ~8% | **~19%** |
| 1825d+ | ~2% | **~13%** |

The wrong curve says a five-year-dormant film is nearly free to delete. The right one
says it still has a better than one-in-eight chance of being watched next year. Same
data, same code, different denominator.

---

## Ground truth: rewatch probability by dormancy

Of films last played *N* days before the cutoff, the share played again within the
following year — over the **correct** population:

| Dormant for | Rewatched within a year |
|---|---|
| 0–365 days | **~61%** |
| 365–548 | ~31% |
| 548–730 | ~32% |
| 730–1095 | ~30% |
| 1095–1825 | ~19% |
| 1825+ | **~13%** |

### There is no cliff. Nothing is ever free to delete.

A film dormant for **five years** still has a double-digit chance of being watched next
year. An active library really is active: the share of films that had *never* been
played at all was under one percent, and a film watched in the past year is more likely
than not to be watched again.

Deletion is never free here. There is only **cheaper** and **dearer**. Any tool that
tells you a five-year-old file is safe to remove is guessing.

---

## What we changed, and what it bought

Measured on one library; the direction of each change is the point, not the digits.

| Policy | Regret | Age-matched baseline | **Lift** |
|---|---|---|---|
| Original (size weighted, no dormancy gate) | 18% | 22% | +18% |
| Current default | **12%** | 17% | **+26%** |
| **Dormancy alone** | 12% | 17% | **+27%** |

Three things follow.

### 1. Size was a real error — but not a fatal one

Removing `SIZE` from the score and adding the dormancy gate cut regret by a third and
took lift from +18% to +26%. Worth doing.

The reasoning stands even though the original "worse than random" claim that motivated
it did not: **size measures how much you gain, not whether anyone wants the file.** And
on a real library, big files are big *because* they are popular — the biggest files are
4K features, which is to say the ones people chose to keep in 4K.

> Use a reward term to **rank** what the risk model has already selected. Never to
> select.

### 2. Dormancy does essentially all the work

Dormancy alone scores *marginally better* than the full signal set. `FEW_WATCHERS` and
`LOW_RATING` are, within noise, contributing **nothing**.

They are kept at low weight because they cost nothing and give the why-panel more to
say — but nobody should imagine they are earning their place. If a future change makes
the engine simpler by dropping them, drop them.

### 3. The best available policy still has ~12% regret

**Roughly one deletion in eight is a film someone comes back for.** That is the honest
number, at the best settings measured. It is not a tuning failure — it is what an active
library looks like. It is the reason the *Leaving Soon* warning and the human approval
gate are not optional decoration. (The warning is what the grace countdown drives; the
countdown itself holds nothing back.)

---

## How to check any future signal

Every *verdict* above — worth doing, contributing nothing, worse than nothing — came from
**lift**: the regret rate of the condemned set versus what you would expect by picking
randomly among films **of the same age**. The rewatch table and the regret rates it is
computed from are raw probabilities, not lift, which is the distinction the population
warning below turns on.

```
lift = (age_matched_expected_regret − actual_regret) / age_matched_expected_regret
```

- **lift > 0.05** — the scorer picks better than age alone. Below that bar a signal does
  not earn its keep, because it is not beating the one thing you get for free.
- **lift ≈ 0** — the signal adds nothing dormancy was not already carrying.
- **lift < 0** — the signal displaces dormancy, and the scorer does worse than age alone.

**Reaper does not compute this, and there is no plan for it to.** The engine that did was a
lab instrument for building Reaper, and it was deleted rather than wired: it had banked its
finding, which is this file. Measuring a new signal means measuring it the way these were —
off a real library's history, outside the app. #553 and #554 are the successors, and neither
is this: they weigh a returned title down and estimate a future rewatch, both live.

**Before believing any of it, check the population.** If the baseline is computed over
a different set of items than the scorer judges, the number is not merely imprecise —
it can have the wrong sign.

## Your library is not this library

The rewatch curve varies with **the audience that produced it**: how many people watch, and
how often they return. Every curve in this file was measured on one library, so its shape
carries that library's viewing habits.

**The curve is borrowed, full stop.** Reaper fits nothing to your server and has no code
that could: the module that could fit one from the owner's own Tautulli history was deleted
with the backtest that would have consumed it (M3g in `docs/STATUS.md`, dropped). Every
number Reaper reads is the table above, and it is one library's curve.

Treat the numbers here as a shape to reason about, never as a measurement of *your* server.
Nothing in the app labels them as borrowed, because nothing in the app can tell the
difference. #554 is the successor if a per-operator answer is ever wanted, and it asks a
different question.
