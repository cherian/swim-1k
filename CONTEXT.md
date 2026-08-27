# CONTEXT

Glossary for internal code names used in `scripts/build_dashboard.py`. This
maps code vocabulary to the plain-English wording the dashboard actually
shows — the page never uses these terms directly (see "Plain English" note
per term below).

## Frontier

The highest credited run length ever confirmed in a *measured* session.
Confirmed by two touches: either twice within one measured session, or once
each in two different measured sessions. A single touch doesn't move it —
that guards against one misdetected watch cycle permanently corrupting every
computed sentence downstream. Computed by `compute_frontier()`.

**Plain English on the page:** "longest swim."

## Baseline

The highest rung (see Rung) that has become *routine* — promoted only after
a window of consecutive measured sessions all show enough runs at that rung
with recovery rest compressed below a target. Distinct from Frontier: a rung
can be touched once (frontier candidate) long before it's routine (baseline).
Computed by `compute_comfort_baseline()`; unchanged by the frontier redesign.

**Plain English on the page:** "your baseline."

## Rung N

Shorthand for "runs of credited length ≥ N." Used throughout the comfort
ladder and frontier logic as the unit of progression.

## Merge / merge-adjacent rest

Two adjacent runs "merge" (fuse into one longer credited run) when the rest
between them compresses to ≤ `run_join_max_s` (currently 5s, in
`data/config.json`) — see `parse_health.walk_runs`. A "frontier-adjacent"
rest is a rest next to a run of Frontier length; the closest one is a merge
*opportunity*, not a guaranteed jump to the next rung, since `credit_run()`
recomputes the credited length of the fused chain from duration and stroke
totals (it can demote, never promote above the raw cycle count).

**Plain English on the page:** "rest by longest swim."

## Training phase

`training_phase` in `data/config.json` steers coaching copy without code
changes. `mode: "extend"` (or the block absent) = push the frontier to the
next rung — the original wording, byte-for-byte. `mode: "consolidate"` =
stack reps at the current frontier (`frontier_reps_target` per session,
counted as runs `>= frontier` so exceeding it still counts), convert 2-lap
runs to 3s, and compress frontier-adjacent rests
(`frontier_rest_median_target_s` median). Only steering copy, tiles, the
report card, correction #2, and the ladder's "Now" row change;
`compute_frontier`, the two-touch ratchet, and the "moved" celebration
narrative are phase-independent. Page vocabulary stays plain English
("longest swim", "this block") — never "frontier"/"phase".

## Two-touch ratchet

The confirmation rule behind Frontier (see above). Existing single-touch
consumers (`longest_run_lengths` on the session record, the goal-meter bar,
the protocol-ladder narrative) are intentionally left as-is — they answer "what's
the biggest thing that ever happened," while Frontier answers "what's
confirmed real." They can legitimately disagree for one session after a
lone big outlier; that's expected, not a bug.
