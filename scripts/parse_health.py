#!/usr/bin/env python3
"""Parse an Apple Health export.zip into data/sessions.json.

Streams the (multi-GB) export.xml — never loads the tree. Records precede
Workouts in the file, so swim/HR records are collected during the pass and
joined to workout windows afterwards.

Usage: python3 parse_health.py "/path/to/Apple Health export.zip"
"""
import hashlib
import json
import os
import statistics
import sys
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
CONFIG = json.loads((PROJECT / "data" / "config.json").read_text())

SWIM_TYPE = "HKWorkoutActivityTypeSwimming"
DIST = "HKQuantityTypeIdentifierDistanceSwimming"
STROKES = "HKQuantityTypeIdentifierSwimmingStrokeCount"
HR = "HKQuantityTypeIdentifierHeartRate"
# Only collect HR this far back — all-day HR across years would balloon memory.
HR_SINCE = datetime.strptime("2026-07-01 00:00:00 -0700", "%Y-%m-%d %H:%M:%S %z")


def ts(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S %z")


def parse_export(zip_path):
    dist_recs, stroke_recs, hr_recs, workouts = [], [], [], []
    export_date = None
    with zipfile.ZipFile(zip_path) as zf:
        with zf.open("apple_health_export/export.xml") as f:
            context = ET.iterparse(f, events=("start", "end"))
            _, root = next(context)
            n = 0
            for event, elem in context:
                if event != "end":
                    continue
                tag = elem.tag
                if tag == "ExportDate":
                    export_date = elem.get("value")
                elif tag == "Record":
                    rtype = elem.get("type")
                    if rtype == DIST:
                        dist_recs.append((ts(elem.get("startDate")), ts(elem.get("endDate")),
                                          float(elem.get("value"))))
                    elif rtype == STROKES:
                        style = {m.get("key"): m.get("value")
                                 for m in elem.findall("MetadataEntry")}.get("HKSwimmingStrokeStyle")
                        stroke_recs.append((ts(elem.get("startDate")), ts(elem.get("endDate")),
                                            float(elem.get("value")),
                                            int(style) if style is not None else None))
                    elif rtype == HR:
                        start = ts(elem.get("startDate"))
                        if start >= HR_SINCE:
                            hr_recs.append((start, float(elem.get("value"))))
                elif tag == "Workout":
                    if elem.get("workoutActivityType") == SWIM_TYPE:
                        meta = {m.get("key"): m.get("value")
                                for m in elem.findall("MetadataEntry")}
                        workouts.append({
                            "start": ts(elem.get("startDate")),
                            "end": ts(elem.get("endDate")),
                            "duration_min": float(elem.get("duration")),
                            "source": elem.get("sourceName"),
                            "location_type": meta.get("HKSwimmingLocationType"),
                            "lap_length": meta.get("HKLapLength"),
                        })
                else:
                    continue
                elem.clear()
                n += 1
                if n % 200000 == 0:
                    root.clear()
    for lst in (dist_recs, stroke_recs, hr_recs):
        lst.sort(key=lambda r: r[0])
    return export_date, workouts, dist_recs, stroke_recs, hr_recs


def in_window(recs, start, end):
    return [r for r in recs if r[0] >= start and r[0] <= end]


def classify_location(w, dists, sid):
    """pool vs open_water. Explicit metadata wins; config override next; else a
    coverage heuristic: pool records span only the swim, so their union covers
    far less of the workout than open-water records (which tile swim + rest)."""
    override = CONFIG.get("location_overrides", {}).get(sid)
    if override:
        return override
    if w["location_type"] == "1":
        return "pool"
    if w["location_type"] == "2":
        return "open_water"
    if len(dists) >= 5:
        covered, cur_s, cur_e = 0.0, None, None
        for start, end, _ in dists:  # dists are start-sorted; clip + union
            start, end = max(start, w["start"]), min(end, w["end"])
            if end <= start:
                continue
            if cur_e is None or start > cur_e:
                if cur_e is not None:
                    covered += (cur_e - cur_s).total_seconds()
                cur_s, cur_e = start, end
            else:
                cur_e = max(cur_e, end)
        if cur_e is not None:
            covered += (cur_e - cur_s).total_seconds()
        if covered < 0.6 * w["duration_min"] * 60:
            return "pool"
    return "open_water"


MIN_CYCLES_FOR_ARTIFACT_CHECK = 6  # too few candidates to trust a session median
FREESTYLE = 2  # HKSwimmingStrokeStyle: 1 mixed, 2 free, 3 back, 4 breast, 5 fly, 6 kick


def free_medians(real_cycles):
    """(med_dur, med_strokes) over FREESTYLE cycles only, or (None, None) when
    too few to trust — non-free lengths (slow breaststroke play) would shift
    the medians and falsely demote genuine freestyle runs."""
    free = [c for c in real_cycles if c.get("stroke_style") == FREESTYLE]
    if len(free) < MIN_CYCLES_FOR_ARTIFACT_CHECK:
        return None, None
    med_dur = statistics.median([c["dur_s"] for c in free])
    sts = [c["strokes"] for c in free if c.get("strokes") is not None]
    med_strokes = statistics.median(sts) if sts else None
    return med_dur, med_strokes


def walk_runs(real_cycles, join_max_s, med_dur, med_strokes, overrides):
    """The one shared run walker. A run is consecutive FREESTYLE lengths
    joined by turn gaps <= join_max_s; non-free or style-less cycles never
    join or start a run (they're volume, not laps) and flush the current
    chain. Returns [{"cycles", "length" (credited), "recovery_rest_s"
    (rest ending the run; None if flushed by style-change/session end
    without a measured rest... the last cycle's rest_est_s, which may be
    None), "end_hr"}]."""
    runs, cur = [], []

    def flush():
        if cur:
            runs.append({
                "cycles": cur[:],
                "length": credit_run(cur, med_dur, med_strokes, overrides),
                "recovery_rest_s": cur[-1]["rest_est_s"],
                "end_hr": cur[-1].get("hr_end"),
            })
            cur.clear()

    for i, c in enumerate(real_cycles):
        if c.get("stroke_style") != FREESTYLE:
            flush()
            continue
        cur.append(c)
        r = c["rest_est_s"]
        nxt_free = (i + 1 < len(real_cycles)
                    and real_cycles[i + 1].get("stroke_style") == FREESTYLE)
        if r is None or r > join_max_s or not nxt_free:
            flush()
    flush()
    return runs


def credit_run(run_cycles, med_dur, med_strokes, overrides):
    """Credited length of a completed run. The watch sometimes splits one real
    length into several records (plausible durations, low strokes, ~0s gaps)
    that chain into phantom multi-lap runs. Demote a run only when duration
    AND stroke totals BOTH say it's shorter (strokes alone undercount in
    genuine runs — push-off glide), crediting the higher of the two estimates.
    Never promote above the raw count. A config override wins — it covers
    what no rule can see (mid-pool standing rests). Pass med_dur/med_strokes
    as None when the session median isn't trustworthy (no demotion then)."""
    n = len(run_cycles)
    ov = overrides.get(run_cycles[0]["start"])
    if ov is not None:
        if isinstance(ov, int) and not isinstance(ov, bool) and 1 <= ov <= n:
            return ov
        print(f"  WARN run_credit_override {run_cycles[0]['start']}: invalid {ov!r}, ignored")
    if n < 2 or not med_dur or not med_strokes:
        return n
    strokes = [c.get("strokes") for c in run_cycles]
    if any(st is None for st in strokes):
        return n
    dur_est = int(sum(c["dur_s"] for c in run_cycles) / med_dur + 0.5)
    stroke_est = int(sum(strokes) / med_strokes + 0.5)
    if dur_est < n and stroke_est < n:
        return max(dur_est, stroke_est, 1)
    return n


def derive_pool(cycles, sid):
    """Derive measured metrics for a pool session from its cycle records.

    The watch sometimes logs wall-rest or drift as a "length" (e.g. 83s with
    1 stroke, or a 588s monster) — any record far longer than a plausible
    length (> 1.8 × median duration) is marked artifact and treated as rest,
    not swimming. The mirror-image bug: a wall-touch/turn can get logged as
    its own extra-short "length" (e.g. 6s with 4 strokes) sitting next to the
    real one — any record far shorter than plausible (< median / 1.8) gets
    the same treatment. Both rules are skipped if there are too few candidate
    cycles to compute a trustworthy median. Also used to reprocess archived
    sessions, so it must depend only on cycle dicts (start/dur_s), not raw
    export records. Returns (est_swim_s, real_cycles, longest_run) and
    mutates cycles in place.
    """
    durs = [c["dur_s"] for c in cycles]
    med = statistics.median(durs) if durs else None
    trust_median = med is not None and len(cycles) >= MIN_CYCLES_FOR_ARTIFACT_CHECK
    hi = 1.8 * med if trust_median else None
    lo = med / 1.8 if trust_median else None
    real = []
    for c in cycles:
        c.pop("artifact", None)
        if hi and (c["dur_s"] > hi or c["dur_s"] < lo):
            c["artifact"] = True
            c["rest_est_s"] = None
        else:
            real.append(c)
    for i, c in enumerate(real):
        if i + 1 < len(real):
            end = datetime.fromisoformat(c["start"]).timestamp() + c["dur_s"]
            nxt = datetime.fromisoformat(real[i + 1]["start"]).timestamp()
            c["rest_est_s"] = round(nxt - end, 1)
        else:
            c["rest_est_s"] = None
    rdurs = [c["dur_s"] for c in real]
    est_swim_s = statistics.median(rdurs) if rdurs else None
    med_dur, med_strokes = free_medians(real)
    overrides = CONFIG.get("run_credit_overrides", {}).get(sid, {})
    runs = walk_runs(real, CONFIG["run_join_max_s"], med_dur, med_strokes, overrides)
    longest_run = max((r["length"] for r in runs), default=0)
    return est_swim_s, real, longest_run


def hr_end_for_cycle(hrs_session, cycle_end):
    """Mean HR in the last 20s before cycle_end; fallback nearest sample
    within ±30s; else None. hrs_session is a session-local (ts, value) list."""
    end_ts = cycle_end.timestamp()
    window = [v for t, v in hrs_session if end_ts - 20 <= t.timestamp() <= end_ts]
    if window:
        return round(sum(window) / len(window), 1)
    nearest = [(abs(t.timestamp() - end_ts), v) for t, v in hrs_session
               if abs(t.timestamp() - end_ts) <= 30]
    if nearest:
        return round(min(nearest, key=lambda n: n[0])[1], 1)
    return None


def build_session(w, dist_recs, stroke_recs, hr_recs):
    dists = in_window(dist_recs, w["start"], w["end"])
    strokes = in_window(stroke_recs, w["start"], w["end"])
    hrs_session = in_window(hr_recs, w["start"], w["end"])
    hrs = [v for _, v in hrs_session]
    stroke_by_start = {s.isoformat(): (v, style) for s, _, v, style in strokes}

    cycles = []
    for start, end, gps_m in dists:
        stroke_v, stroke_style = stroke_by_start.get(start.isoformat(), (None, None))
        cycles.append({
            "start": start.isoformat(),
            "dur_s": round((end - start).total_seconds(), 1),
            "gps_m": round(gps_m, 1),
            "strokes": stroke_v,
            "stroke_style": stroke_style,
            "hr_end": hr_end_for_cycle(hrs_session, end),
        })
    sid = w["start"].strftime("%Y-%m-%d_%H%M")
    location = classify_location(w, dists, sid)
    rest_measured = location == "pool"
    if rest_measured:
        # Pool Swim mode: each record spans ONLY the swim; rest is the gap
        # until the next real record's start. Swim and rest are both measured.
        est_swim_s, real_cycles, longest_run = derive_pool(cycles, sid)
    else:
        # Open-water mode: each record tiles length-swum + rest-until-next-
        # push-off ("cycle"). Estimated pure-swim time per length = a low
        # percentile of cycle durations (the cycles with essentially no rest).
        # Rest is an ESTIMATE, not measured.
        durs = sorted(c["dur_s"] for c in cycles)
        est_swim_s = durs[max(0, int(len(durs) * 0.08))] if durs else None
        for c in cycles:
            c["rest_est_s"] = max(0.0, round(c["dur_s"] - est_swim_s, 1)) if est_swim_s else None
        real_cycles = cycles
        longest_run, run = 0, 0
        for c in real_cycles:
            run += 1
            longest_run = max(longest_run, run)
            if c["rest_est_s"] is None or c["rest_est_s"] > 15:
                run = 0
    lengths = len(real_cycles)

    era = "video-era" if w["start"].date().isoformat() >= CONFIG["protocol_start_date"] else "baseline"
    era = CONFIG.get("session_tag_overrides", {}).get(sid, era)
    return {
        "id": sid,
        "start": w["start"].isoformat(),
        "end": w["end"].isoformat(),
        "duration_min": round(w["duration_min"], 1),
        "location_type": location,
        "lap_length_setting": w["lap_length"],
        "era": era,
        "lengths": lengths,
        "true_distance_m": round(lengths * CONFIG["pool_length_m"], 1),
        "gps_distance_m": round(sum(c["gps_m"] for c in real_cycles), 1),
        "est_swim_per_length_s": est_swim_s,
        "rest_measured": rest_measured,
        "longest_run_lengths": longest_run,
        "hr_avg": round(sum(hrs) / len(hrs), 1) if hrs else None,
        "hr_max": round(max(hrs), 1) if hrs else None,
        "cycles": cycles,
    }


def main():
    zip_path = Path(sys.argv[1]).expanduser()
    print(f"Streaming {zip_path} …", flush=True)
    export_date, workouts, dist_recs, stroke_recs, hr_recs = parse_export(zip_path)
    print(f"{len(workouts)} swim workouts, {len(dist_recs)} length records, "
          f"{len(hr_recs)} HR records", flush=True)

    sessions = [build_session(w, dist_recs, stroke_recs, hr_recs)
                for w in sorted(workouts, key=lambda w: w["start"])]

    # Append-only merge: the archive on disk is the source of truth for history.
    # A fresh parse can add or refresh sessions but never drop them, and never
    # replace an archived session with a shorter parse (partial export guard).
    out_path = PROJECT / "data" / "sessions.json"
    archived = {}
    if out_path.exists():
        archived = {s["id"]: s for s in json.loads(out_path.read_text())["sessions"]}
    for s in sessions:
        old = archived.get(s["id"])
        # Guard on raw cycle count, not derived lengths — artifact filtering can
        # legitimately lower `lengths` without any loss of raw records.
        if old is not None and len(old.get("cycles", [])) > len(s["cycles"]):
            continue
        if old is not None and {k: v for k, v in old.items() if k != "source_export_date"} == s:
            continue  # unchanged — keep the stamp of the export that last changed it
        s["source_export_date"] = export_date
        archived[s["id"]] = s
    sessions = sorted(archived.values(), key=lambda s: s["start"])

    # Sanity: open-water cycles should roughly tile the workout.
    # (Pool records span only the swim, so they never tile — skip those.)
    for s in sessions:
        if s["lengths"] and not s["rest_measured"]:
            covered = sum(c["dur_s"] for c in s["cycles"]) / 60
            if abs(covered - s["duration_min"]) > s["duration_min"] * 0.25:
                print(f"  WARN {s['id']}: cycles cover {covered:.1f}min "
                      f"of {s['duration_min']}min workout")

    h = hashlib.sha256()
    with open(zip_path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)

    out = {
        "schema_version": CONFIG["schema_version"],
        "generated_from": {"zip": str(zip_path), "export_date": export_date,
                           "sha256": h.hexdigest()},
        "sessions": sessions,
    }
    tmp = out_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(out, indent=1))
    os.replace(tmp, out_path)
    print(f"Wrote {out_path} ({len(sessions)} sessions)")


if __name__ == "__main__":
    main()
