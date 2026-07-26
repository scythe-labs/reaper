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

## Refutations later found to be wrong

None yet. When one lands here, record what the verifier missed — that reasoning is worth more
than the finding, because it is the failure mode of the review process itself.
