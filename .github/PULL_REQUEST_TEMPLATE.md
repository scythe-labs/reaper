<!--
Your PR title becomes the commit message on `dev`, because pull requests are
squash-merged. Write it as a Conventional Commit and it will read correctly in
the history forever:

    fix(retention): an open pull request keeps its image
    feat(policy): season-level spare
    docs: contributor guide

A workflow checks the title and will tell you if it does not parse.

Keep the description short however large the change is. Summarize, and group
related edits into one item. Say what the diff cannot show.
-->

## What was wrong

<!--
What someone could see go wrong, then the cause. Add a log line or a short
example if you have one. For a feature, say what was missing. Link the issue.
-->

Closes #

## What this PR does

<!-- A numbered list, one change per item, a plain sentence or two each. -->

1.

## Tests

<!--
What you added, what you ran with its real result, and what you clicked and
saw. "CI is green" is a fine answer for a docs change and a thin one for
anything that touches a decision.
-->

## Does this touch the deletion path

<!--
Delete the line that does not apply.

- No. This does not change which media is chosen, scored, or removed.
- Yes, and here is what keeps it safe: ...

Reaper removes files that nobody can get back. A change that alters scoring,
gates, the planner, or the executor is read with that in mind, and saying so
up front makes the review faster.
-->

## Checklist

- [ ] The [verification gates](https://github.com/scythe-labs/reaper/blob/dev/CONTRIBUTING.md#verification-gates) pass locally.
- [ ] Documentation is updated if this changes what the app does.
- [ ] AI assistance is disclosed below, per [the policy](https://github.com/scythe-labs/reaper/blob/dev/CONTRIBUTING.md#ai-assistance).

**AI disclosure:**

<!--
One line. "None", "drafted by Claude Code and reviewed line by line", and
"I used an assistant to understand the codebase, the patch is mine" are all
fine answers. It tells a reviewer where to look harder. This project is built
with AI assistance itself, so the answer is welcome whatever it says.
-->
