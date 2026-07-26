# Candidates that did not survive verification

Review candidates that were raised by a reviewer and **killed by an independent verifier
reading the same code**. Read this before a review; do not re-raise what is here.

**A refutation is bound to a commit, not to the repo forever.** Each entry records where the
code stood when it was refuted. If the cited code has changed since, the refutation is stale
and the candidate is live again — re-verify rather than assuming either answer.

Append here whenever a verifier kills a candidate. An entry that is later found to be *wrong*
— the defect was real after all — moves to the bottom section, so the record shows the miss
instead of hiding it.

## Refuted at `d3c3839` (2026-07-24, the fifth review pass)

Carried forward from `docs/history/CODE_REVIEW.md`, which is frozen. 45 candidates were raised
in that pass, 37 survived, and these 8 did not.

| Area | Candidate | Files it concerned |
| --- | --- | --- |
| season-path | Every spare/override click full-scans the entire, never-GC'd candidate table because `group_key` is unindexed | `services/season_pruning.py`, `db/models.py` |
| engine | A gate missing from `PolicyBody.gates` is silently not run and cannot be warned about; an empty gates tuple validates and removes every built-in protection | `engine/policy.py`, `engine/gates.py` |
| engine | `facts_from_dict` raises on any stored `facts_json` written before a `_OBS_FIELDS` entry or a `GateId` value existed, 500-ing the simulator instead of falling back to a fresh scan | `engine/facts_codec.py` |
| engine-identity | `identity.py`'s design rationale claims the single production join is reachable from the backtest and the planner; neither module references identity at all | `engine/identity.py` |
| api | The API-key lane may write `/api/profile`, so a header-only credential can turn off the run caps interlock and lower the grace window, contradicting the same block's stated rule that all setting changes stay behind the browser | `api/middleware.py`, `api/settings.py` |
| services-misc | A stored instance API key can be shipped to any host by editing `base_url` and pressing Test, defeating the module's stated write-only invariant | `services/instances.py` |
| infra | `PRAGMA synchronous=NORMAL` makes the deletion journal non-durable across a host crash, contradicting the durability the `ActionStep`/`StepState` docstrings and rule 26 assert | `db/session.py:33` |
| infra | Seerr paging advances the cursor by the requested page size, not by the number of rows the server actually returned, so a clamped or short page silently skips records | `clients/seerr.py` |

## Refuted at `59188c9` (2026-07-26, reviewing the season-prune commit `1b10c8c`)

| Area | Candidate | Why it did not survive |
| --- | --- | --- |
| executor | `measured` is typed `dict[int, int \| None]`, so `live_sizes` can hold `None` and the growth check sums it | `executor.py:1897` skips the whole season when any size is `None` and an approved size exists, and `:1904` filters `None` out of the sum. Both branches are reached before any delete. |
| executor | `_finalize_plex`'s new docstring claims a movie rescan is scoped to the movie's own folder, but the movie path may pass a root folder | `executor.py:1777` passes `movie["path"]`, which in Radarr is the movie's own folder, falling back to `folderName`. The claim holds. |
| executor | A flat season layout makes `_common_parent` return the series root, so the fix does nothing there | Correct, and correct *behavior*: when every episode sits directly in the series folder, the series folder genuinely is the narrowest directory holding the deleted files. There is no narrower true scope to pick, so this is the right answer rather than a gap. |

## Refutations later found to be wrong

None yet. When one lands here, record what the verifier missed — that reasoning is worth more
than the finding, because it is the failure mode of the review process itself.
