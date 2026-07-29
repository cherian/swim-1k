# Swim 1K — Olympian Coach Project

A running project: swim **1000 m (44 lengths of a 25 yd pool) without stopping**,
graded against the Ocean Swim School protocol (Alex Coci, 2012 Olympian) with a
coach's progression layer on top.

## The one command that matters

After each new Apple Health export (Health app → profile → Export All Health Data):

```bash
./update.sh "/path/to/Apple Health export.zip"
open index.html
```

That re-parses all swim workouts, recomputes the analysis, and regenerates the
dashboard. Each export is a complete snapshot, so the rebuild is deterministic —
running it twice on the same zip changes nothing.

## What's here

| Path | What it is |
|---|---|
| `index.html` | The living dashboard (open this) |
| `reference/video.mp4` | The coaching video, saved locally |
| `reference/protocol.md` | The grading canon — protocol + 3 mistakes, with video timestamps |
| `data/config.json` | Pool length, goal, protocol start date, rest band. Edit here, then rerun `update.sh` |
| `data/sessions.json` | Parsed sessions + export provenance (regenerated) |
| `scripts/parse_health.py` | Streams export.xml → sessions.json |
| `scripts/build_dashboard.py` | sessions.json + config → index.html (style-guide CSS pulled verbatim at build time) |

## Data caveats (until the watch fix)

July 2026 swims were recorded in **open-water mode**: GPS distances (~15% hot,
discarded — true distance = lengths × 22.86 m) and no measured rests. Rest is
estimated per length (cycle time − estimated swim time) and labeled as such in the
report. Once workouts are recorded as **Pool Swim / 25 yd**, laps and rests become
exact and the estimation caveats drop out automatically.
