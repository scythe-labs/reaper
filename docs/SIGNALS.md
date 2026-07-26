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

The two curves, side by side. Note that the error is not uniform: it is worst in
exactly the buckets the policy cares about most.

| Dormant for | Over all history keys (**wrong**) | Over the actual library (**right**) |
|---|---|---|
| 0–365d | ~44% | **~60%** |
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
| 0–365 days | **~60%** |
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
library looks like. It is the reason the grace period, the *Leaving Soon* collection,
and the human approval gate are not optional decoration.

---

## How to check any future signal

The backtest reports **lift**: the regret rate of the condemned set versus what you
would expect by picking randomly among films **of the same age**.

```
lift = (age_matched_expected_regret − actual_regret) / age_matched_expected_regret
```

- **lift > 0.05** — the scorer picks better than age alone. This is the code's own bar
  (`backtest.beats_random`): at or below it the backtest prints "not beating age alone,
  do not arm this policy," so a merely positive lift does not earn its keep.
- **lift ≈ 0** — the signal is dormancy in a trenchcoat.
- **lift < 0** — the signal is *worse than nothing*; it displaces dormancy.

**Before believing any of it, check the population.** If the baseline is computed over
a different set of items than the scorer judges, the number is not merely imprecise —
it can have the wrong sign.

## Your library is not this library

The rewatch curve is a property of **an audience**, not of physics. A household of three
has nothing in common with a server used by a hundred people.

**Today every prior in use is the one above.** `engine/calibration.derive` can fit a curve
from the owner's own Tautulli history, and is tested, but it has **no caller anywhere in
`src/`** — not even the backtest, which imports only `RewatchPrior` and `NotCalibratedError`
from it. Its only callers are the tests, and the backtest that would consume it is itself
unreachable (see `docs/STATUS.md`, M3c and M3g). So `backtest.FALLBACK_REWATCH_PRIOR` is
what anything actually reads, and it is one library's curve.

Treat the numbers here as a shape to reason about, never as a measurement of *your* server.
The machinery to label a borrowed curve exists (`backtest.prior_is_derived`) and will start
reporting the moment the backtest gets a route.
