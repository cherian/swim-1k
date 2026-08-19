#!/usr/bin/env python3
"""Build index.html (the living dashboard) from data/sessions.json + data/config.json.

Style-guide CSS is extracted verbatim from the canonical template at build time.
Charts are self-contained inline SVG (native <title> tooltips, no JS).
Palette (validated for CVD safety + contrast):
  era pair      video-era #2563eb / baseline #b45309
  rest statuses in-band #2563eb / over #d97706 / way-over #b91c1c
"""
import hashlib
import html
import json
import re
import statistics
from collections import Counter
from datetime import datetime
from pathlib import Path

import parse_health  # same directory; credit_run + CONFIG (run_credit_overrides)

PROJECT = Path(__file__).resolve().parent.parent
STYLE_TEMPLATE = Path("/Users/cherianthomas/dev/Style Template/styleguide.html")

BLUE, AMBER, RED = "#2563eb", "#d97706", "#b91c1c"
BASELINE_C, ERA_C = "#b45309", "#2563eb"
BAND_LO, BAND_HI = 20, 45          # protocol active-rest band (video 06:29)
WAY_OVER = 90                      # beyond this, rest is a full reset
RUN_JOIN = parse_health.CONFIG["run_join_max_s"]  # turn gap that keeps a run alive
FREESTYLE = parse_health.FREESTYLE
MIN_LENGTHS = 5                    # ignore false-start recordings


def esc(s):
    return html.escape(str(s))


def load():
    cfg = json.loads((PROJECT / "data" / "config.json").read_text())
    data = json.loads((PROJECT / "data" / "sessions.json").read_text())
    excluded_ids = cfg.get("excluded_sessions", {})
    recent = [s for s in data["sessions"]
              if s["start"] >= "2026-07" and s["lengths"] >= MIN_LENGTHS]
    july = [s for s in recent if s["id"] not in excluded_ids]
    excluded = [(s, excluded_ids[s["id"]]) for s in recent if s["id"] in excluded_ids]
    return cfg, data, july, excluded


def rest_status(r):
    if r is None:
        return "unknown"
    if r <= BAND_HI:
        return "inband"
    if r <= WAY_OVER:
        return "over"
    return "wayover"


def starts_of_multi_runs(s):
    """id()s of cycles that BEGIN a credited multi-lap run — for the
    'deliberate long rest fueled a run' checks."""
    return {id(run["cycles"][0]) for run in session_runs(s) if run["length"] >= 2}


def session_stats(s):
    all_cycles = [c for c in s["cycles"] if not c.get("artifact")]
    run_starts = starts_of_multi_runs(s)
    cycles = all_cycles[:-1] if len(all_cycles) > 1 else all_cycles  # last cycle has no following rest
    rests = [c["rest_est_s"] for c in cycles if c["rest_est_s"] is not None]
    n = len(rests)
    inband = sum(1 for r in rests if r <= BAND_HI)
    med = sorted(rests)[n // 2] if n else None
    # deliberate long rest: way-over rest that fueled a credited multi-lap run
    deliberate = 0
    for i, c in enumerate(cycles):
        if (c["rest_est_s"] and c["rest_est_s"] > WAY_OVER
                and i + 1 < len(all_cycles) and id(all_cycles[i + 1]) in run_starts):
            deliberate += 1
    return {
        "median_rest": med,
        "inband_pct": round(100 * inband / n) if n else None,
        "deliberate_long": deliberate,
        "date_label": datetime.fromisoformat(s["start"]).strftime("%a %b %-d"),
    }


def split_rests(s):
    """Classify each rest after an artifact-filtered cycle by its OUTGOING
    edge, consistent with session_runs' credited walk:
      - connector: current AND next cycles are freestyle, gap <= RUN_JOIN —
        mid-run turn, excluded entirely
      - recovery: the rest ends a run whose CREDITED length is >= 2 —
        deliberate, never counted against the 30s target
      - single: everything else (incl. rests after non-free lengths and
        after runs credit-demoted to 1) — counted against the target
    A None rest (session end) has nothing to classify.
    """
    cycles = [c for c in s["cycles"] if not c.get("artifact")]
    ends_multi = {id(run["cycles"][-1]) for run in session_runs(s) if run["length"] >= 2}
    out = []
    for i, c in enumerate(cycles):
        r = c["rest_est_s"]
        if r is None:
            continue
        nxt = cycles[i + 1] if i + 1 < len(cycles) else None
        if (c.get("stroke_style") == FREESTYLE and nxt is not None
                and nxt.get("stroke_style") == FREESTYLE and r <= RUN_JOIN):
            kind = "connector"
        elif id(c) in ends_multi:
            kind = "recovery"
        else:
            kind = "single"
        out.append({"rest_s": r, "kind": kind, "start": c["start"]})
    return out


def session_runs(s):
    """All run logic lives in parse_health.walk_runs (freestyle-only chains,
    turn gap <= RUN_JOIN, credited lengths via credit_run + config overrides).
    This is a thin adapter over the artifact-filtered cycles. Each run dict:
    {"cycles", "length" (credited), "recovery_rest_s" (rest ending the run;
    None for a session-ending run), "end_hr" (last cycle's, may be None)}."""
    cycles = [c for c in s["cycles"] if not c.get("artifact")]
    med_dur, med_strokes = parse_health.free_medians(cycles)
    overrides = parse_health.CONFIG.get("run_credit_overrides", {}).get(s["id"], {})
    return parse_health.walk_runs(cycles, RUN_JOIN, med_dur, med_strokes, overrides)


def fifty_capacity_stats(measured):
    """Per measured session: counts of >=2-length runs (split 2 vs 3+), their
    share of session volume, and back-to-back recovery rests between
    consecutive >=2 runs. session_runs() returns freestyle runs only, so
    consecutive entries in that list are NOT necessarily physically adjacent
    — a non-freestyle length can sit between them. A pair counts only when
    run i+1's first cycle immediately follows run i's last cycle in the
    session's artifact-filtered cycle list.
    share_pct numerator = credited freestyle lengths in >=2 runs; denominator
    = s["lengths"] (all recorded lengths incl. non-freestyle and
    credit-demoted) — a deliberately conservative hybrid that slightly
    understates share on sessions with demotions."""
    out = []
    for s in measured:
        runs = session_runs(s)
        multi = [r for r in runs if r["length"] >= 2]
        count2 = sum(1 for r in multi if r["length"] == 2)
        count3plus = sum(1 for r in multi if r["length"] >= 3)
        multi_lengths = sum(r["length"] for r in multi)
        run_hist = Counter(r["length"] for r in multi)
        share_pct = 100 * multi_lengths / s["lengths"] if s["lengths"] else 0

        cycles = [c for c in s["cycles"] if not c.get("artifact")]
        pos = {id(c): i for i, c in enumerate(cycles)}
        b2b_rests = []
        for r1, r2 in zip(runs, runs[1:]):
            if (r1["length"] >= 2 and r2["length"] >= 2
                    and r1["recovery_rest_s"] is not None
                    and pos.get(id(r1["cycles"][-1]), -1) + 1 == pos.get(id(r2["cycles"][0]), -2)):
                b2b_rests.append(r1["recovery_rest_s"])

        out.append({
            "session": s,
            "count2": count2,
            "count3plus": count3plus,
            "multi_lengths": multi_lengths,
            "run_hist": run_hist,
            "share_pct": share_pct,
            "b2b_rests": b2b_rests,
            "b2b_median": statistics.median(b2b_rests) if b2b_rests else None,
        })
    return out


def comfort_ladder_stats(measured):
    """Per measured session, per rung N (runs of length >= N): count of
    qualifying runs, median recovery rest ending them (session-ending runs
    excluded), and median end-of-length HR. Also the session's single-lap
    end_hr median, as the HR reference point."""
    max_n = max((r["length"] for s in measured for r in session_runs(s)), default=0)
    rungs = list(range(2, max_n + 1))
    per_session = []
    for s in measured:
        runs = session_runs(s)
        single_hrs = [r["end_hr"] for r in runs if r["length"] == 1 and r["end_hr"] is not None]
        rung_data = {}
        for n in rungs:
            qual = [r for r in runs if r["length"] >= n]
            recoveries = [r["recovery_rest_s"] for r in qual if r["recovery_rest_s"] is not None]
            hrs = [r["end_hr"] for r in qual if r["end_hr"] is not None]
            rung_data[n] = {
                "count": len(qual),
                "recovery_count": len(recoveries),
                "median_recovery": statistics.median(recoveries) if recoveries else None,
                "median_hr": statistics.median(hrs) if hrs else None,
            }
        per_session.append({
            "session": s, "rungs": rung_data,
            "single_hr_med": statistics.median(single_hrs) if single_hrs else None,
        })
    return rungs, per_session


def compute_comfort_baseline(rungs, per_session, cfg):
    """N is baseline when, in each of the last window_sessions measured
    sessions, there were >= min_runs_per_session runs of length >= N AND
    their median recovery rest was <= recovery_rest_max_s."""
    cc = cfg["comfort_baseline"]
    window, min_runs, max_rec = cc["window_sessions"], cc["min_runs_per_session"], cc["recovery_rest_max_s"]
    if len(per_session) < window:
        return 0, {}
    last_n = per_session[-window:]
    baseline, detail = 0, {}
    for n in rungs:
        # recovery_count >= min_runs too: session-ending runs count toward
        # frequency but carry no measured recovery — 3 runs with 1 measured
        # recovery must not promote.
        qualifies = [ps["rungs"][n]["count"] >= min_runs
                     and ps["rungs"][n]["recovery_count"] >= min_runs
                     and ps["rungs"][n]["median_recovery"] is not None
                     and ps["rungs"][n]["median_recovery"] <= max_rec
                     for ps in last_n]
        detail[n] = qualifies
        if all(qualifies):
            baseline = n
    return baseline, detail


def comfort_badge(baseline, rungs, per_session, cfg):
    cc = cfg["comfort_baseline"]
    window, min_runs, max_rec = cc["window_sessions"], cc["min_runs_per_session"], cc["recovery_rest_max_s"]
    next_n = baseline + 1 if baseline else 2
    if baseline:
        base_line = (f"Your baseline: {baseline} laps — routine and low-stress, promoted after "
                     f"{window} straight sessions of ≥{min_runs} runs at ≥{baseline} laps "
                     f"with recovery rest compressed to ≤{max_rec:.0f}s.")
    else:
        base_line = (f"Your baseline: 1 lap — no run length has compressed to a routine "
                     f"≤{max_rec:.0f}s recovery across {window} straight sessions yet.")
    window_sessions = per_session[-window:] if len(per_session) >= window else per_session
    _empty = {"count": 0, "recovery_count": 0, "median_recovery": None}
    rd_next = [ps["rungs"].get(next_n, _empty) for ps in window_sessions]
    qualifying = sum(1 for rd in rd_next
                      if rd["count"] >= min_runs and rd["recovery_count"] >= min_runs
                      and rd["median_recovery"] is not None and rd["median_recovery"] <= max_rec)
    latest_rd = per_session[-1]["rungs"].get(next_n, _empty) if per_session else _empty
    if latest_rd["count"] and latest_rd["median_recovery"] is not None:
        missing = (f"{next_n}-lap runs: {qualifying}/{len(window_sessions)} sessions qualify — "
                   f"recovery still {latest_rd['median_recovery']:.0f}s median")
    elif latest_rd["count"]:
        missing = f"{next_n}-lap runs: {qualifying}/{len(window_sessions)} sessions qualify — no completed recovery yet to measure"
    else:
        missing = f"{next_n}-lap runs: {qualifying}/{len(window_sessions)} sessions qualify — no {next_n}-lap runs recorded yet"
    return base_line, missing


COMFORT_RUNG_COLORS = {2: BLUE, 3: AMBER}


def chart_comfort_compression(per_session, cfg):
    target = cfg["comfort_baseline"]["recovery_rest_max_s"]
    W, H, pad_l, pad_r, pad_b, pad_t = 1000, 320, 52, 130, 50, 20
    plot_h = H - pad_b - pad_t
    cap = 150
    n = len(per_session)
    slot = (W - pad_l - pad_r) / max(n, 1)

    def y(v):
        return pad_t + plot_h * (1 - min(v, cap) / cap)

    g = [svg_open(W, H, "Median recovery rest after 2-lap and 3-lap runs, per session")]
    g.append(f'<line x1="{pad_l}" x2="{W-pad_r}" y1="{y(target):.1f}" y2="{y(target):.1f}" '
             f'stroke="#222" stroke-width="1.5" stroke-dasharray="6 4"/>')
    g.append(f'<text x="{W-pad_r+8}" y="{y(target)+4:.1f}" font-size="13" fill="#222">target {target:.0f}s</text>')
    for gv in (30, 60, 90, 120):
        g.append(f'<line x1="{pad_l}" x2="{W-pad_r}" y1="{y(gv):.1f}" y2="{y(gv):.1f}" stroke="#e2ded9" stroke-width="1"/>')
        g.append(f'<text x="{pad_l-8}" y="{y(gv)+4:.1f}" text-anchor="end" font-size="13" fill="#8a8580">{gv}s</text>')

    for rung, color in COMFORT_RUNG_COLORS.items():
        pts = [(i, ps["rungs"][rung]["median_recovery"]) for i, ps in enumerate(per_session)
               if rung in ps["rungs"] and ps["rungs"][rung]["median_recovery"] is not None]
        for (i1, v1), (i2, v2) in zip(pts, pts[1:]):
            if i2 == i1 + 1:
                x1, x2 = pad_l + i1 * slot + slot / 2, pad_l + i2 * slot + slot / 2
                g.append(f'<line x1="{x1:.1f}" x2="{x2:.1f}" y1="{y(v1):.1f}" y2="{y(v2):.1f}" '
                         f'stroke="{color}" stroke-width="2" opacity="0.6"/>')
        for i, v in pts:
            x = pad_l + i * slot + slot / 2
            d = datetime.fromisoformat(per_session[i]["session"]["start"])
            g.append(f'<circle cx="{x:.1f}" cy="{y(v):.1f}" r="5" fill="{color}"><title>{d.strftime("%a %b %-d")} '
                     f'· {rung}-lap runs · median recovery {v:.0f}s</title></circle>')
    g.append(f'<text x="{W-pad_r+10}" y="{pad_t+14}" font-size="13" fill="{COMFORT_RUNG_COLORS[2]}" font-weight="600">● 2-lap</text>')
    g.append(f'<text x="{W-pad_r+10}" y="{pad_t+32}" font-size="13" fill="{COMFORT_RUNG_COLORS[3]}" font-weight="600">● 3-lap</text>')
    for i, ps in enumerate(per_session):
        x = pad_l + i * slot + slot / 2
        d = datetime.fromisoformat(ps["session"]["start"])
        g.append(f'<text x="{x:.1f}" y="{H-24}" text-anchor="middle" font-size="12" fill="#8a8580">{d.strftime("%-m/%-d")}</text>')
    g.append(f'<text x="{pad_l}" y="{H-6}" font-size="13" fill="#8a8580">dot = median recovery rest ending runs of that '
             f'length (session-ending runs excluded) · dashed line = {target:.0f}s compression target</text>')
    g.append("</svg>")
    return "".join(g)


def comfort_hr_narrative(per_session):
    per = [ps for ps in per_session if ps["single_hr_med"] is not None]
    if not per:
        return "No heart-rate evidence yet for this era."
    latest = per[-1]
    d = datetime.fromisoformat(latest["session"]["start"]).strftime("%b %-d")
    single = latest["single_hr_med"]
    two = latest["rungs"].get(2, {}).get("median_hr")
    if two is None:
        return (f"On {d}, single-lap end-of-length HR sat at {single:.0f} bpm. Not enough 2+ lap runs yet to "
                f"compare — once they show up, this line tracks whether multi-lap HR converges toward the "
                f"single-lap number.")
    gap = single - two
    if abs(gap) <= 3:
        verdict = "essentially the same heart rate — a 2+ lap run is no longer costing you anything extra."
    elif gap > 0:
        verdict = f"running {gap:.0f} bpm lower after a 2+ lap run than after a single lap — the multi-lap swim is the easier one."
    else:
        verdict = f"still running {abs(gap):.0f} bpm higher after a 2+ lap run — there's real cost left to spend down."
    return f"On {d}: single-lap end-of-length HR {single:.0f} bpm vs 2+ lap {two:.0f} bpm — {verdict}"


def _jitter(seed, idx, spread):
    """Deterministic pseudo-random offset in [-spread/2, spread/2], seeded so
    rebuilds are byte-stable (no random/Date.now)."""
    h = int(hashlib.md5(f"{seed}-{idx}".encode()).hexdigest(), 16)
    return ((h % 1000) / 1000 - 0.5) * spread


# ---------------------------------------------------------------- charts

def svg_open(w, h, label):
    return (f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="{esc(label)}" '
            f'style="width:100%;height:auto;display:block;font-family:Inter,sans-serif">')


def chart_rest_strip(s, stats):
    """Spotlight session: one thin bar per length; height = rest after it."""
    rest_word = "measured" if s.get("rest_measured") else "estimated"
    run_starts = starts_of_multi_runs(s)
    all_cycles = [c for c in s["cycles"] if not c.get("artifact")]
    cycles = all_cycles[:-1]
    W, H, pad_l, pad_b, pad_t = 1000, 300, 46, 34, 16
    plot_h = H - pad_b - pad_t
    max_r = 150
    n = len(cycles)
    slot = (W - pad_l - 10) / max(n, 1)
    bw = max(4, min(14, slot - 3))

    def y(v):
        return pad_t + plot_h * (1 - min(v, max_r) / max_r)

    g = [svg_open(W, H, f"{rest_word.capitalize()} rest after each length, {stats['date_label']}")]
    # target band
    g.append(f'<rect x="{pad_l}" y="{y(BAND_HI):.1f}" width="{W-pad_l-10}" '
             f'height="{y(0)-y(BAND_HI):.1f}" fill="#2563eb" opacity="0.07"/>')
    for gv in (45, 90, 150):
        g.append(f'<line x1="{pad_l}" x2="{W-10}" y1="{y(gv):.1f}" y2="{y(gv):.1f}" '
                 f'stroke="#e2ded9" stroke-width="1"/>')
        g.append(f'<text x="{pad_l-8}" y="{y(gv)+4:.1f}" text-anchor="end" '
                 f'font-size="13" fill="#8a8580">{gv}s</text>')
    g.append(f'<text x="{pad_l-8}" y="{y(0)+4:.1f}" text-anchor="end" font-size="13" fill="#8a8580">0</text>')

    for i, c in enumerate(cycles):
        r = c["rest_est_s"]
        if r is None:
            continue
        st = rest_status(r)
        color = {"inband": BLUE, "over": AMBER, "wayover": RED}[st]
        x = pad_l + i * slot + (slot - bw) / 2
        bh = max(2.0, y(0) - y(r))
        credited = (st == "wayover" and i + 1 < len(all_cycles)
                    and id(all_cycles[i + 1]) in run_starts)
        label = {"inband": "in band", "over": "over band", "wayover": "full reset"}[st]
        if credited:
            label += " — credited: fueled a back-to-back run"
        t = datetime.fromisoformat(c["start"]).strftime("%-I:%M %p")
        g.append(f'<rect x="{x:.1f}" y="{y(r):.1f}" width="{bw:.1f}" height="{bh:.1f}" '
                 f'rx="3" fill="{color}"><title>Length {i+1} · {t} · '
                 f'{"" if s.get("rest_measured") else "~"}{r:.0f}s rest ({label})</title></rect>')
        if credited:
            g.append(f'<circle cx="{x+bw/2:.1f}" cy="{y(r)-8:.1f}" r="4" fill="none" '
                     f'stroke="{RED}" stroke-width="2"><title>Deliberate long rest before a back-to-back run</title></circle>')
    g.append(f'<text x="{pad_l}" y="{H-8}" font-size="13" fill="#8a8580">'
             f'each bar = one 25 yd length · bar height = {rest_word} rest before the next push-off · shaded zone = the protocol band (≤{BAND_HI}s)</text>')
    g.append("</svg>")
    return "".join(g)


def chart_volume(july, cfg):
    W, H, pad_l, pad_b, pad_t = 1000, 260, 52, 44, 14
    plot_h = H - pad_b - pad_t
    max_v = 1300
    n = len(july)
    slot = (W - pad_l - 10) / n
    bw = min(52, slot - 12)

    def y(v):
        return pad_t + plot_h * (1 - v / max_v)

    g = [svg_open(W, H, "True distance per session in July")]
    for gv in (500, 1000):
        g.append(f'<line x1="{pad_l}" x2="{W-10}" y1="{y(gv):.1f}" y2="{y(gv):.1f}" '
                 f'stroke="#e2ded9" stroke-width="1"/>')
        g.append(f'<text x="{pad_l-8}" y="{y(gv)+4:.1f}" text-anchor="end" font-size="13" fill="#8a8580">{gv}m</text>')
    g.append(f'<line x1="{pad_l}" x2="{W-10}" y1="{y(cfg["goal_m"]):.1f}" y2="{y(cfg["goal_m"]):.1f}" '
             f'stroke="#222" stroke-width="1.5" stroke-dasharray="6 4"/>')
    g.append(f'<text x="{W-12}" y="{y(cfg["goal_m"])-6:.1f}" text-anchor="end" font-size="13" fill="#222">goal volume · 1000 m</text>')
    for i, s in enumerate(july):
        x = pad_l + i * slot + (slot - bw) / 2
        v = s["true_distance_m"]
        c = ERA_C if s["era"] == "video-era" else BASELINE_C
        d = datetime.fromisoformat(s["start"])
        g.append(f'<rect x="{x:.1f}" y="{y(v):.1f}" width="{bw:.1f}" height="{y(0)-y(v):.1f}" rx="4" '
                 f'fill="{c}"><title>{d.strftime("%a %b %-d")} · {s["lengths"]} lengths · {v:.0f} m true distance</title></rect>')
        g.append(f'<text x="{x+bw/2:.1f}" y="{H-24}" text-anchor="middle" font-size="13" fill="#666">{d.strftime("%-d")}</text>')
    g.append(f'<text x="{pad_l}" y="{H-6}" font-size="13" fill="#8a8580">session dates · true distance = lengths × 22.86 m (GPS figures discarded)</text>')
    g.append("</svg>")
    return "".join(g)


REST_TARGET_S = 30
REST_Y_CAP_S = 120
REST_CHART_MAX_SESSIONS = 16

B2B_TARGET_S = 30    # gateway: back-to-back recovery compressed to <=30s
B2B_Y_CAP_S = 180    # current b2b medians run to ~132s; 120 cap would clip half the data


def rest_stat_row(measured):
    """Three tiles for the latest measured session: median single-lap rest,
    under-30s share, over-90s count. The first two carry a delta vs the
    immediately previous measured session — never reaching back further —
    shown as an em dash if either side has no single-lap rests."""
    if not measured:
        return ""

    def singles_of(s):
        return [r["rest_s"] for r in split_rests(s) if r["kind"] == "single"]

    cur = singles_of(measured[-1])
    prv = singles_of(measured[-2]) if len(measured) >= 2 else []

    def tile(caption, value, delta):
        delta_html = (f' <span style="font-size:0.95rem;font-weight:500;color:#8a8580">{esc(delta)}</span>'
                      if delta else "")
        return (f'<div style="flex:1"><p class="caption">{esc(caption)}</p>'
                f'<p style="font-size:1.6rem;font-weight:600;margin:0">{esc(value)}{delta_html}</p></div>')

    if not cur:
        med_val, u30_val, o90_val, med_delta, u30_delta = "—", "—", "—", "", ""
    else:
        med = statistics.median(cur)
        u30 = 100 * sum(1 for v in cur if v < 30) / len(cur)
        o90 = sum(1 for v in cur if v > 90)
        med_val, u30_val, o90_val = f"{med:.0f}s", f"{u30:.1f}%", str(o90)
        if prv:
            dmed = med - statistics.median(prv)
            du30 = u30 - 100 * sum(1 for v in prv if v < 30) / len(prv)
            med_delta = f"{'▲' if dmed > 0 else '▼' if dmed < 0 else '–'}{abs(dmed):.0f}s"
            u30_delta = f"{'▲' if du30 > 0 else '▼' if du30 < 0 else '–'}{abs(du30):.1f}pp"
        else:
            med_delta = u30_delta = "—"

    return (f'<div class="card"><div class="card-body"><div class="card-row">'
            f'{tile("median rest", med_val, med_delta)}'
            f'{tile("under 30s", u30_val, u30_delta)}'
            f'{tile("over 90s", o90_val, "")}'
            f'</div></div></div>')


def chart_rest_band(measured):
    """Median + middle-50% band of single-lap rests per session (rolling
    window). Stats run over ALL singles; only the SVG y-axis clamps at
    REST_Y_CAP_S, so a session with a Q3 above the cap is visibly clipped —
    called out in the caption, not hidden. Sessions with n<4 singles get a
    median dot only, and the band renders as contiguous segments so a future
    n<4 session breaks it rather than bridging the gap."""
    window = measured[-REST_CHART_MAX_SESSIONS:]
    W, H, pad_l, pad_r, pad_b, pad_t = 1000, 380, 52, 20, 56, 20
    plot_h = H - pad_b - pad_t
    n = len(window)
    slot = (W - pad_l - pad_r) / max(n, 1)

    def y(v):
        return pad_t + plot_h * (1 - min(v, REST_Y_CAP_S) / REST_Y_CAP_S)

    g = [svg_open(W, H, "Median and middle-50% band of single-lap rests per session")]
    g.append(f'<rect x="{pad_l}" y="{y(REST_TARGET_S):.1f}" width="{W-pad_l-pad_r}" '
             f'height="{y(0)-y(REST_TARGET_S):.1f}" fill="{BLUE}" opacity="0.07"/>')
    g.append(f'<line x1="{pad_l}" x2="{W-pad_r}" y1="{y(REST_TARGET_S):.1f}" y2="{y(REST_TARGET_S):.1f}" '
             f'stroke="{BLUE}" stroke-width="2" stroke-dasharray="7 4"/>')
    g.append(f'<text x="{pad_l+6}" y="{y(REST_TARGET_S)-8:.1f}" text-anchor="start" font-size="13" '
             f'fill="{BLUE}" font-weight="600">TARGET 30s</text>')
    for gv in (30, 60, 90):
        g.append(f'<line x1="{pad_l}" x2="{W-pad_r}" y1="{y(gv):.1f}" y2="{y(gv):.1f}" stroke="#e2ded9" stroke-width="1"/>')
        g.append(f'<text x="{pad_l-8}" y="{y(gv)+4:.1f}" text-anchor="end" font-size="13" fill="#8a8580">{gv}s</text>')

    if not window:
        g.append("</svg>")
        return "".join(g)

    col_x = [pad_l + i * slot + slot / 2 for i in range(n)]
    per_session = []
    for s in window:
        singles = sorted(r["rest_s"] for r in split_rests(s) if r["kind"] == "single")
        med = statistics.median(singles) if singles else None
        q1 = q3 = None
        if len(singles) >= 4:
            q1, _, q3 = statistics.quantiles(singles, n=4)
        tail = sum(1 for v in singles if v > REST_Y_CAP_S)
        per_session.append({"singles": singles, "med": med, "q1": q1, "q3": q3, "tail": tail})

    # IQR band: contiguous polygon segments; a run of n<4 columns breaks it.
    i = 0
    while i < n:
        if per_session[i]["q1"] is None:
            i += 1
            continue
        j = i
        while j + 1 < n and per_session[j + 1]["q1"] is not None:
            j += 1
        top = [(col_x[k], y(per_session[k]["q3"])) for k in range(i, j + 1)]
        bot = [(col_x[k], y(per_session[k]["q1"])) for k in range(j, i - 1, -1)]
        pts = " ".join(f"{px:.1f},{py:.1f}" for px, py in top + bot)
        g.append(f'<polygon points="{pts}" fill="{BLUE}" opacity="0.12"/>')
        i = j + 1

    for i in range(n - 1):
        m0, m1 = per_session[i]["med"], per_session[i + 1]["med"]
        if m0 is not None and m1 is not None:
            g.append(f'<line x1="{col_x[i]:.1f}" x2="{col_x[i+1]:.1f}" y1="{y(m0):.1f}" y2="{y(m1):.1f}" '
                     f'stroke="{BLUE}" stroke-width="2"/>')

    meds = [(k, ps["med"]) for k, ps in enumerate(per_session) if ps["med"] is not None]
    labels = {}
    if meds:
        labels.setdefault(meds[-1][0], []).append("latest")
        labels.setdefault(min(meds, key=lambda kv: kv[1])[0], []).append("best")

    for k, s in enumerate(window):
        ps = per_session[k]
        d = datetime.fromisoformat(s["start"])
        if ps["med"] is not None:
            cy = y(ps["med"])
            q_str = f"Q1 {ps['q1']:.0f}–Q3 {ps['q3']:.0f}s" if ps["q1"] is not None else "n<4, no band"
            title = f"{d.strftime('%a %b %-d')} · median {ps['med']:.0f}s · {q_str} · n={len(ps['singles'])}"
            if ps["tail"]:
                title += f" · {ps['tail']} over {REST_Y_CAP_S:.0f}s"
            g.append(f'<circle cx="{col_x[k]:.1f}" cy="{cy:.1f}" r="5" fill="{BLUE}"><title>{esc(title)}</title></circle>')
            if k in labels:
                label = f"{ps['med']:.0f}s · {' & '.join(labels[k])}"
                g.append(f'<text x="{col_x[k]:.1f}" y="{cy-12:.1f}" text-anchor="middle" font-size="12" '
                         f'font-weight="700" fill="{BLUE}">{esc(label)}</text>')
            if ps["tail"]:
                g.append(f'<text x="{col_x[k]:.1f}" y="{y(REST_Y_CAP_S)-6:.1f}" text-anchor="middle" '
                         f'font-size="11" fill="#8a8580">{ps["tail"]}&#8593;</text>')
        g.append(f'<text x="{col_x[k]:.1f}" y="{H-30:.1f}" text-anchor="middle" font-size="14" fill="#444">{d.strftime("%-m/%-d")}</text>')
        g.append(f'<text x="{col_x[k]:.1f}" y="{H-12:.1f}" text-anchor="middle" font-size="12" fill="#8a8580">n={len(ps["singles"])}</text>')
    g.append("</svg>")
    return "".join(g)


def fifty_stat_row(cap_stats):
    """Three tiles for the latest measured session: 50-yd run count, share of
    volume, median back-to-back rest — vs the immediately previous measured
    session, shown as an em dash when only one measured session exists."""
    if not cap_stats:
        return ""

    def tile(caption, value, delta):
        delta_html = (f' <span style="font-size:0.95rem;font-weight:500;color:#8a8580">{esc(delta)}</span>'
                      if delta else "")
        return (f'<div style="flex:1"><p class="caption">{esc(caption)}</p>'
                f'<p style="font-size:1.6rem;font-weight:600;margin:0">{esc(value)}{delta_html}</p></div>')

    cur = cap_stats[-1]
    prv = cap_stats[-2] if len(cap_stats) >= 2 else None
    cur_runs, prv_runs = cur["count2"] + cur["count3plus"], (prv["count2"] + prv["count3plus"]) if prv else None

    runs_val, share_val = str(cur_runs), f"{cur['share_pct']:.0f}%"
    b2b_val = f"{cur['b2b_median']:.0f}s" if cur["b2b_median"] is not None else "—"

    if prv is None:
        runs_delta = share_delta = b2b_delta = "—"
    else:
        drun = cur_runs - prv_runs
        runs_delta = f"{'▲' if drun > 0 else '▼' if drun < 0 else '–'}{abs(drun)}"
        dshare = round(cur["share_pct"] - prv["share_pct"])
        share_delta = f"{'▲' if dshare > 0 else '▼' if dshare < 0 else '–'}{abs(dshare)}pp"
        if cur["b2b_median"] is not None and prv["b2b_median"] is not None:
            db2b = cur["b2b_median"] - prv["b2b_median"]
            b2b_delta = f"{'▼' if db2b < 0 else '▲' if db2b > 0 else '–'}{abs(db2b):.0f}s"
        else:
            b2b_delta = "—"

    return (f'<div class="card"><div class="card-body"><div class="card-row">'
            f'{tile("50-yd runs", runs_val, runs_delta)}'
            f'{tile("share of volume", share_val, share_delta)}'
            f'{tile("median b2b rest", b2b_val, b2b_delta)}'
            f'</div></div></div>')


def chart_fifty_count(cap_stats):
    """Stacked bar per session: count of >=2-length runs, split 2 (blue) vs
    3+ (amber). Share-of-volume label above each bar."""
    W, H, pad_l, pad_b, pad_t = 1000, 260, 52, 44, 14
    plot_h = H - pad_b - pad_t
    n = len(cap_stats)
    max_v = max(12, max((c["count2"] + c["count3plus"] for c in cap_stats), default=0) + 1)
    slot = (W - pad_l - 10) / max(n, 1)
    bw = min(52, slot - 12)

    def y(v):
        return pad_t + plot_h * (1 - v / max_v)

    g = [svg_open(W, H, "Count of 50-yard-plus runs per session, stacked by run length")]
    step = 2 if max_v <= 12 else 4
    gv = step
    while gv < max_v:
        g.append(f'<line x1="{pad_l}" x2="{W-10}" y1="{y(gv):.1f}" y2="{y(gv):.1f}" '
                 f'stroke="#e2ded9" stroke-width="1"/>')
        g.append(f'<text x="{pad_l-8}" y="{y(gv)+4:.1f}" text-anchor="end" font-size="13" fill="#8a8580">{gv}</text>')
        gv += step

    for i, c in enumerate(cap_stats):
        x = pad_l + i * slot + (slot - bw) / 2
        s = c["session"]
        d = datetime.fromisoformat(s["start"])
        total = c["count2"] + c["count3plus"]
        hist_str = " · ".join(f"{cnt}×{length}" for length, cnt in sorted(c["run_hist"].items()))
        tooltip = (f"{d.strftime('%a %b %-d')} · {total} runs ≥2 lengths"
                   + (f" ({hist_str})" if hist_str else "")
                   + f" · {c['multi_lengths']}/{s['lengths']} lengths = {c['share_pct']:.0f}% of volume")
        y0, y2 = y(0), y(c["count2"])
        if c["count2"]:
            g.append(f'<rect x="{x:.1f}" y="{y2:.1f}" width="{bw:.1f}" height="{y0-y2:.1f}" rx="4" '
                     f'fill="{BLUE}"><title>{esc(tooltip)}</title></rect>')
        if c["count3plus"]:
            y3 = y(total)
            g.append(f'<rect x="{x:.1f}" y="{y3:.1f}" width="{bw:.1f}" height="{y2-y3:.1f}" rx="4" '
                     f'fill="{AMBER}"><title>{esc(tooltip)}</title></rect>')
        if total:
            g.append(f'<text x="{x+bw/2:.1f}" y="{y(total)-6:.1f}" text-anchor="middle" font-size="12" '
                     f'fill="#8a8580">{c["share_pct"]:.0f}%</text>')
        g.append(f'<text x="{x+bw/2:.1f}" y="{H-24}" text-anchor="middle" font-size="13" fill="#666">{d.strftime("%-m/%-d")}</text>')
    g.append("</svg>")
    return "".join(g)


def chart_fifty_b2b(cap_stats):
    """Rest between back-to-back (cycle-adjacent) >=2-length runs, per
    session: faint raw dots + solid median line, dashed 30s gateway target.
    A session with zero b2b pairs breaks the median line and shows n=0."""
    W, H, pad_l, pad_r, pad_b, pad_t = 1000, 380, 52, 20, 56, 20
    plot_h = H - pad_b - pad_t
    n = len(cap_stats)
    slot = (W - pad_l - pad_r) / max(n, 1)

    def y(v):
        return pad_t + plot_h * (1 - min(v, B2B_Y_CAP_S) / B2B_Y_CAP_S)

    g = [svg_open(W, H, "Rest between back-to-back 50-yard-plus runs, per session")]
    g.append(f'<rect x="{pad_l}" y="{y(B2B_TARGET_S):.1f}" width="{W-pad_l-pad_r}" '
             f'height="{y(0)-y(B2B_TARGET_S):.1f}" fill="{BLUE}" opacity="0.07"/>')
    g.append(f'<line x1="{pad_l}" x2="{W-pad_r}" y1="{y(B2B_TARGET_S):.1f}" y2="{y(B2B_TARGET_S):.1f}" '
             f'stroke="{BLUE}" stroke-width="2" stroke-dasharray="7 4"/>')
    g.append(f'<text x="{pad_l+6}" y="{y(B2B_TARGET_S)-8:.1f}" text-anchor="start" font-size="13" '
             f'fill="{BLUE}" font-weight="600">TARGET {B2B_TARGET_S}s</text>')
    for gv in (30, 60, 90, 120, 150):
        g.append(f'<line x1="{pad_l}" x2="{W-pad_r}" y1="{y(gv):.1f}" y2="{y(gv):.1f}" stroke="#e2ded9" stroke-width="1"/>')
        g.append(f'<text x="{pad_l-8}" y="{y(gv)+4:.1f}" text-anchor="end" font-size="13" fill="#8a8580">{gv}s</text>')

    if not cap_stats:
        g.append("</svg>")
        return "".join(g)

    col_x = [pad_l + i * slot + slot / 2 for i in range(n)]

    for i, c in enumerate(cap_stats):
        for j, v in enumerate(c["b2b_rests"]):
            dx = col_x[i] + _jitter(c["session"]["id"], j, 14)
            g.append(f'<circle cx="{dx:.1f}" cy="{y(min(v, B2B_Y_CAP_S)):.1f}" r="3.5" fill="{BLUE}" opacity="0.35"/>')

    for i in range(n - 1):
        m0, m1 = cap_stats[i]["b2b_median"], cap_stats[i + 1]["b2b_median"]
        if m0 is not None and m1 is not None:
            g.append(f'<line x1="{col_x[i]:.1f}" x2="{col_x[i+1]:.1f}" y1="{y(m0):.1f}" y2="{y(m1):.1f}" '
                     f'stroke="{BLUE}" stroke-width="2"/>')

    for i, c in enumerate(cap_stats):
        s = c["session"]
        d = datetime.fromisoformat(s["start"])
        over_cap = sum(1 for v in c["b2b_rests"] if v > B2B_Y_CAP_S)
        if c["b2b_median"] is not None:
            title = f"{d.strftime('%a %b %-d')} · median b2b rest {c['b2b_median']:.0f}s · n={len(c['b2b_rests'])}"
            g.append(f'<circle cx="{col_x[i]:.1f}" cy="{y(c["b2b_median"]):.1f}" r="5" fill="{BLUE}">'
                     f'<title>{esc(title)}</title></circle>')
        if over_cap:
            g.append(f'<text x="{col_x[i]:.1f}" y="{y(B2B_Y_CAP_S)-6:.1f}" text-anchor="middle" '
                     f'font-size="11" fill="#8a8580">{over_cap}&#8593;</text>')
        g.append(f'<text x="{col_x[i]:.1f}" y="{H-30:.1f}" text-anchor="middle" font-size="14" fill="#444">{d.strftime("%-m/%-d")}</text>')
        g.append(f'<text x="{col_x[i]:.1f}" y="{H-12:.1f}" text-anchor="middle" font-size="12" fill="#8a8580">n={len(c["b2b_rests"])}</text>')
    g.append("</svg>")
    return "".join(g)


def rest_narrative(measured):
    per = []
    for s in measured:
        singles = [r["rest_s"] for r in split_rests(s) if r["kind"] == "single"]
        if not singles:
            continue
        under30 = sum(1 for v in singles if v < 30)
        pct = round(100 * under30 / len(singles))
        d = datetime.fromisoformat(s["start"])
        per.append({"date": d.strftime("%a %b %-d"), "n": under30, "t": len(singles), "pct": pct})
    if not per:
        return "No single-lap rests recorded yet in the measured sessions."
    latest, best = per[-1], max(per, key=lambda p: p["pct"])
    return (f"On {latest['date']}, {latest['n']} of {latest['t']} single-lap rests beat 30s "
            f"(best day so far: {best['date']} at {best['pct']}%).")


TIMESHARE_COLS = [("u30", BLUE, "&lt;30s", 700),
                  ("b3040", "#4478e8", "30–40s", 400),
                  ("b4050", "#6291ee", "40–50s", 400),
                  ("b5060", "#7aa7f7", "50–60s", 400),
                  ("b6090", AMBER, "60–90s", 400), ("o90", RED, "&gt;90s", 400),
                  ("rec", "#8a8580", "recovery", 400)]


def rest_timeshare_table(measured):
    """Wall-time share per session: seconds in each rest band as a share of all
    classified wall time (singles + recoveries; mid-run connectors excluded).
    Bands: <30s, 30-40s, 40-50s, 50-60s, 60-90s (all half-open on the low
    end, closed on the high end at 90), >90s, plus recovery."""
    rows = []
    for s in measured:
        rests = split_rests(s)
        singles = [r["rest_s"] for r in rests if r["kind"] == "single"]
        t = {"u30": sum(v for v in singles if v < 30),
             "b3040": sum(v for v in singles if 30 <= v < 40),
             "b4050": sum(v for v in singles if 40 <= v < 50),
             "b5060": sum(v for v in singles if 50 <= v < 60),
             "b6090": sum(v for v in singles if 60 <= v <= 90),
             "o90": sum(v for v in singles if v > 90),
             "rec": sum(r["rest_s"] for r in rests if r["kind"] == "recovery")}
        total = sum(t.values())
        if not total:
            continue
        d = datetime.fromisoformat(s["start"])
        cells = "".join(
            f'<td style="text-align:right;color:{color};font-weight:{weight}">'
            f'{100 * t[key] / total:.0f}%</td>'
            for key, color, _, weight in TIMESHARE_COLS)
        rows.append(f'<tr><td>{d.strftime("%b %-d")}</td>{cells}'
                    f'<td style="text-align:right;color:#8a8580">{total/60:.0f}m</td></tr>')
    if not rows:
        return "<p class='small'>No measured wall time yet.</p>"
    head = "".join(f'<th style="text-align:right;color:{color}">{label}</th>'
                   for _, color, label, _ in TIMESHARE_COLS)
    return (f'<div class="table-wrap"><table class="striped compact" style="font-variant-numeric:tabular-nums">'
            f'<thead><tr><th>Date</th>{head}<th style="text-align:right">at the wall</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div>')


STROKE_MIN, STROKE_MAX = 8, 20    # outside this range: watch glitch, red ring
GLIDE_LO, GLIDE_HI = 8, 11
STROKE_YMIN, STROKE_YMAX = 6, 22
SWOLF_YMIN, SWOLF_YMAX = 15, 70


def stroke_lengths(s):
    """Filter pipeline (in order): drop artifact cycles entirely -> drop
    strokes=None lengths (counted as `missing`) -> implausible <8/>20 become
    rings (excluded from stats). Returns (valid, rings, missing) where valid
    and rings are dicts with 'strokes' and 'swolf' (dur_s + strokes)."""
    cycles = [c for c in s["cycles"] if not c.get("artifact")]
    missing = sum(1 for c in cycles if c.get("strokes") is None)
    valid, rings = [], []
    for c in cycles:
        st = c.get("strokes")
        if st is None:
            continue
        item = {"strokes": st, "swolf": c["dur_s"] + st}
        (rings if st < STROKE_MIN or st > STROKE_MAX else valid).append(item)
    return valid, rings, missing


def _efficiency_chart(measured, key, y_lo, y_hi, grid_step, bands, title):
    W, H, pad_l, pad_r, pad_b, pad_t = 1000, 340, 52, 20, 56, 18
    plot_h = H - pad_b - pad_t
    n = len(measured)
    slot = (W - pad_l - pad_r) / max(n, 1)

    def y(v):
        v = max(y_lo, min(y_hi, v))
        return pad_t + plot_h * (1 - (v - y_lo) / (y_hi - y_lo))

    g = [svg_open(W, H, title)]
    for lo, hi, color, label in bands:
        g.append(f'<rect x="{pad_l}" y="{y(hi):.1f}" width="{W-pad_l-pad_r}" '
                 f'height="{max(0.0, y(lo)-y(hi)):.1f}" fill="{color}" opacity="0.08"/>')
        g.append(f'<text x="{W-pad_r}" y="{y(hi)+14:.1f}" text-anchor="end" font-size="12" '
                 f'fill="{color}">{esc(label)}</text>')
    gv = y_lo + (grid_step - y_lo % grid_step if y_lo % grid_step else 0)
    while gv <= y_hi:
        g.append(f'<line x1="{pad_l}" x2="{W-pad_r}" y1="{y(gv):.1f}" y2="{y(gv):.1f}" stroke="#e2ded9" stroke-width="1"/>')
        g.append(f'<text x="{pad_l-8}" y="{y(gv)+4:.1f}" text-anchor="end" font-size="13" fill="#8a8580">{gv:g}</text>')
        gv += grid_step

    if not measured:
        g.append("</svg>")
        return "".join(g)

    col_x = [pad_l + i * slot + slot / 2 for i in range(n)]
    per_session = [stroke_lengths(s) for s in measured]
    medians = [statistics.median([it[key] for it in valid]) if valid else None
               for valid, _, _ in per_session]

    for i in range(len(medians) - 1):
        if medians[i] is not None and medians[i + 1] is not None:
            g.append(f'<line x1="{col_x[i]:.1f}" x2="{col_x[i+1]:.1f}" y1="{y(medians[i]):.1f}" '
                     f'y2="{y(medians[i+1]):.1f}" stroke="{BLUE}" stroke-width="2" opacity="0.5"/>')

    box_w = min(28, slot * 0.4)
    for i, (s, (valid, rings, missing)) in enumerate(zip(measured, per_session)):
        x, d = col_x[i], datetime.fromisoformat(s["start"])
        vals = sorted(it[key] for it in valid)
        med = medians[i]
        if len(vals) >= 4:
            q1, _, q3 = statistics.quantiles(vals, n=4)
            g.append(f'<rect x="{x-box_w/2:.1f}" y="{y(q3):.1f}" width="{box_w:.1f}" '
                     f'height="{max(1.0, y(q1)-y(q3)):.1f}" rx="6" fill="{BLUE}" opacity="0.25"/>')
        for j, v in enumerate(vals):
            dx = x + _jitter(f"{s['id']}-{key}", j, min(24, box_w + 8))
            g.append(f'<circle cx="{dx:.1f}" cy="{y(v):.1f}" r="3.5" fill="{BLUE}" opacity="0.5"/>')
        for r in rings:
            rv = r[key]
            g.append(f'<circle cx="{x:.1f}" cy="{y(rv):.1f}" r="4" fill="none" stroke="{RED}" '
                     f'stroke-width="1.5" opacity="0.8"><title>{rv:.0f} — watch glitch, excluded from stats</title></circle>')
        if med is not None:
            miss = f" · {missing} length{'s' if missing != 1 else ''} missing stroke data" if missing else ""
            g.append(f'<line x1="{x-box_w/2-2:.1f}" x2="{x+box_w/2+2:.1f}" y1="{y(med):.1f}" y2="{y(med):.1f}" '
                     f'stroke="#111" stroke-width="3"><title>median: {med:.1f}{miss}</title></line>')
        g.append(f'<text x="{x:.1f}" y="{H-24}" text-anchor="middle" font-size="14" fill="#444">{d.strftime("%-m/%-d")}</text>')
        if med is not None:
            g.append(f'<text x="{x:.1f}" y="{H-6}" text-anchor="middle" font-size="15" font-weight="700" fill="{BLUE}">{med:.0f}</text>')
    g.append("</svg>")
    return "".join(g)


def chart_strokes(measured):
    return _efficiency_chart(measured, "strokes", STROKE_YMIN, STROKE_YMAX, 2,
                              [(GLIDE_LO, GLIDE_HI, "#047857", "glide zone")],
                              "Strokes per length, per session")


def chart_swolf(measured):
    return _efficiency_chart(measured, "swolf", SWOLF_YMIN, SWOLF_YMAX, 10,
                              [(35, 45, "#047857", "very good (25 yd)"),
                               (30, 35, "#065f46", "elite")],
                              "SWOLF per length, per session")


def stroke_narrative(measured):
    per = []
    for s in measured:
        valid, _, _ = stroke_lengths(s)
        if not valid:
            continue
        d = datetime.fromisoformat(s["start"])
        per.append({
            "date": d.strftime("%b %-d"),
            "strokes": statistics.median([v["strokes"] for v in valid]),
            "swolf": statistics.median([v["swolf"] for v in valid]),
        })
    if not per:
        return "No measured stroke data yet."
    strokes_list = ", ".join(f"{p['strokes']:.0f}" for p in per)
    swolf_list = ", ".join(f"{p['swolf']:.0f}" for p in per)
    return (f"Stroke count has held at {strokes_list} strokes per length across every measured session — "
            f"already inside the glide zone. SWOLF ({swolf_list}) backs it up: that's the \"very good\" band "
            f"for a 25 yd pool, not a stall dressed up as glide. Flat is honest here — the chart exists to "
            f"catch the next real gain, not to flatter this one.")


RUNGS = [(1, ""), (2, "8 × 50 yd"), (5, "10 × 5"), (10, "5 × 10"),
         (22, "2 × 22"), (44, "GOAL · 1000 m")]


def chart_progression(sessions, cfg):
    """The climb: longest continuous run per session vs the protocol ladder.
    Milestone-anchored y-scale — rungs evenly spaced, values interpolated —
    so early progress is visible under the 44-length goal line."""
    W, H, pad_l, pad_r, pad_b, pad_t = 1000, 380, 46, 130, 44, 22
    plot_h, plot_w = H - pad_b - pad_t, W - pad_l - pad_r
    vals = [v for v, _ in RUNGS]

    def y(v):
        v = max(vals[0], min(vals[-1], v))
        for i in range(len(vals) - 1):
            lo, hi = vals[i], vals[i + 1]
            if v <= hi:
                frac = (i + (v - lo) / (hi - lo)) / (len(vals) - 1)
                return pad_t + plot_h * (1 - frac)
        return pad_t

    n = len(sessions)
    x = lambda i: pad_l + 20 + (plot_w - 40) * (i / max(n - 1, 1))

    g = [svg_open(W, H, "Longest continuous swim per session, climbing the protocol ladder")]
    for v, label in RUNGS:
        g.append(f'<line x1="{pad_l}" x2="{W-pad_r}" y1="{y(v):.1f}" y2="{y(v):.1f}" '
                 f'stroke="{"#b91c1c" if v == 44 else "#e2ded9"}" stroke-width="{2 if v == 44 else 1}" '
                 f'{"stroke-dasharray=\"6 4\"" if v == 44 else ""}/>')
        g.append(f'<text x="{pad_l-8}" y="{y(v)+4:.1f}" text-anchor="end" font-size="13" fill="#8a8580">{v}</text>')
        if label:
            g.append(f'<text x="{W-pad_r+8}" y="{y(v)+4:.1f}" font-size="13" '
                     f'fill="{RED if v == 44 else "#8a8580"}"'
                     f'{" font-weight=\"600\"" if v == 44 else ""}>{esc(label)}</text>')

    # best-so-far step line, built from MEASURED sessions only — open-water
    # estimates produced phantom runs and don't get to set the headline line.
    best, steps = 0, []
    for i, s in enumerate(sessions):
        if s.get("rest_measured") and s["longest_run_lengths"] > best:
            best = s["longest_run_lengths"]
        steps.append((i, best))
    for i, best_v in steps[:-1]:
        if best_v == 0:
            continue  # line starts at the first measured session
        nxt_v = steps[i + 1][1]
        g.append(f'<line x1="{x(i):.1f}" x2="{x(i+1):.1f}" y1="{y(best_v):.1f}" y2="{y(best_v):.1f}" '
                 f'stroke="{BLUE}" stroke-width="3" opacity="0.85"/>')
        if nxt_v != best_v:
            g.append(f'<line x1="{x(i+1):.1f}" x2="{x(i+1):.1f}" y1="{y(best_v):.1f}" y2="{y(nxt_v):.1f}" '
                     f'stroke="{BLUE}" stroke-width="3" opacity="0.85"/>')

    for i, s in enumerate(sessions):
        d = datetime.fromisoformat(s["start"])
        # Estimated sessions get no dot: open-water estimation fabricates runs
        # (near-zero estimated rests chain into back-to-back laps that never happened).
        if s.get("rest_measured"):
            st = session_stats(s)
            r = s["longest_run_lengths"]
            med = f"{st['median_rest']:.0f}s" if st["median_rest"] is not None else "?"
            g.append(f'<circle cx="{x(i):.1f}" cy="{y(r):.1f}" r="7" fill="{BLUE}" stroke="{BLUE}" stroke-width="2.5">'
                     f'<title>{d.strftime("%a %b %-d")} · longest run {r} lengths · {s["lengths"]} lengths total · '
                     f'median rest {med} (measured)</title></circle>')
        g.append(f'<text x="{x(i):.1f}" y="{H-pad_b+20}" text-anchor="middle" font-size="12" fill="#8a8580">'
                 f'{d.strftime("%-m/%-d")}</text>')

    g.append(f'<text x="{pad_l}" y="{H-6}" font-size="13" fill="#8a8580">'
             f'dot = longest continuous swim, measured sessions only (open-water estimates fabricated runs) · '
             f'blue step = best so far · rungs = the protocol ladder</text>')
    g.append("</svg>")
    return "".join(g)


def goal_meter(longest_run, goal_lengths):
    W, H = 1000, 86
    bar_y, bar_h = 26, 26
    frac = min(1.0, longest_run / goal_lengths)
    fill_w = max(8, (W - 4) * frac)
    g = [svg_open(W, H, "Longest continuous swim versus the 44-length goal")]
    g.append(f'<rect x="2" y="{bar_y}" width="{W-4}" height="{bar_h}" rx="13" fill="#eceae6"/>')
    g.append(f'<rect x="2" y="{bar_y}" width="{fill_w:.1f}" height="{bar_h}" rx="13" fill="{BLUE}">'
             f'<title>Longest continuous: {longest_run} lengths of 44</title></rect>')
    g.append(f'<text x="2" y="16" font-size="14" fill="#666">longest continuous swim</text>')
    g.append(f'<text x="{W-2}" y="16" text-anchor="end" font-size="14" fill="#666">goal: 44 lengths · 1000 m</text>')
    g.append(f'<text x="{max(fill_w+14, 30):.1f}" y="{bar_y+18}" font-size="15" font-weight="600" fill="#222">'
             f'{longest_run} lengths · {longest_run*22.86:.0f} m</text>')
    g.append("</svg>")
    return "".join(g)


# ---------------------------------------------------------------- verdict

def make_verdict(latest, prev, stats, prev_stats):
    med = stats["median_rest"]
    if latest.get("rest_measured"):
        first_measured = not prev.get("rest_measured")
        if first_measured:
            win = (f"You fixed the watch. This is the first fully measured swim — exact laps, exact rests, "
                   f"honest distance, and a measured {latest['est_swim_per_length_s']:.0f}s per length. "
                   f"Longest back-to-back run: {latest['longest_run_lengths']} lengths, no asterisk.")
        elif latest["longest_run_lengths"] > prev["longest_run_lengths"]:
            win = (f"Longest back-to-back run: {latest['longest_run_lengths']} lengths "
                   f"({latest['longest_run_lengths']*22.86:.0f} m), measured — up from "
                   f"{prev['longest_run_lengths']} last session.")
        else:
            win = (f"Your pace held: a measured {latest['est_swim_per_length_s']:.0f}s per length even on a "
                   f"{latest['lengths']}-length day. The stroke is repeatable — that's the raw material.")
        if med is not None and med > BAND_HI:
            headline = ("First measured swim — and the estimates were flattering you." if first_measured
                        else "The engine is fine. The wall stops are where the race is lost.")
            fix = (f"Measured median rest is ~{med:.0f}s, above the 20–45s band — only "
                   f"{stats['inband_pct']}% of rests in band. Touch, three breaths, go. And keep "
                   "chunking up: the protocol's smallest unit is 100 m — four lengths.")
        else:
            headline = ("First measured swim — and the rest discipline is real." if first_measured
                        else "Rests in band, measured. Now stretch the chunks.")
            fix = ("Your repeats are one length long. The protocol's smallest unit is 100 m — four lengths. "
                   "Rest discipline is no longer your gap; chunk size is.")
        return headline, win, fix
    win, fix = [], []
    if stats["inband_pct"] is not None and prev_stats["inband_pct"] is not None:
        if stats["inband_pct"] >= prev_stats["inband_pct"]:
            win.append(f"{stats['inband_pct']}% of your rests landed inside the 20–45s protocol band — up from {prev_stats['inband_pct']}% the session before.")
        else:
            win.append(f"Median rest held at ~{stats['median_rest']:.0f}s — inside the protocol band, which is where endurance is built.")
    if latest["longest_run_lengths"] >= prev["longest_run_lengths"]:
        win.append(f"Longest back-to-back run: {latest['longest_run_lengths']} lengths ({latest['longest_run_lengths']*22.86:.0f} m) without a real stop.")
    fix.append("Your repeats are one length long. The protocol's smallest unit is 100 m — four lengths. "
               "Rest discipline is no longer your gap; chunk size is.")
    headline = ("Your rest is closer to protocol than you think. "
                "Your chunks are the gap now.")
    return headline, win[0], fix[0]


# ---------------------------------------------------------------- page

def build():
    cfg, data, july, excluded = load()
    css = re.search(r"<style>(.*?)</style>", STYLE_TEMPLATE.read_text(), re.S).group(1)

    latest, prev = july[-1], july[-2]
    l_stats, p_stats = session_stats(latest), session_stats(prev)
    headline, win, fix = make_verdict(latest, prev, l_stats, p_stats)
    # Trust measured sessions over open-water estimates for the headline number.
    measured_runs = [s["longest_run_lengths"] for s in july if s.get("rest_measured")]
    best_run = max(measured_runs) if measured_runs else max(s["longest_run_lengths"] for s in july)
    era_sessions = [s for s in july if s["era"] == "video-era"]
    era_med = sorted(session_stats(s)["median_rest"] for s in era_sessions)[len(era_sessions) // 2]
    measured = [s for s in july if s.get("rest_measured")]
    gen_date = data["generated_from"]["export_date"] or "unknown"
    l_date = datetime.fromisoformat(latest["start"]).strftime("%A, %B %-d")

    prog = [s for s in july if s["start"] >= cfg["protocol_start_date"]]
    prog_measured = [s["longest_run_lengths"] for s in prog if s.get("rest_measured")]
    prog_best = max(prog_measured) if prog_measured else max(s["longest_run_lengths"] for s in prog)
    next_rung = next((v for v, _ in RUNGS if v > prog_best), cfg["goal_lengths"])
    prog_coach = (f"Since the protocol started (July 19), your best continuous swim is {prog_best} lengths "
                  f"({prog_best*22.86:.0f} m). Next rung on the ladder: {next_rung} lengths "
                  f"({next_rung*22.86:.0f} m) in one go. Every measured session adds a dot — the job is to make "
                  f"the blue step line climb, one rung at a time, until it touches the red line at 44.")

    comfort_rungs, comfort_per_session = comfort_ladder_stats(measured)
    comfort_baseline, _ = compute_comfort_baseline(comfort_rungs, comfort_per_session, cfg)
    comfort_base_line, comfort_missing = comfort_badge(comfort_baseline, comfort_rungs, comfort_per_session, cfg)
    fifty_stats = fifty_capacity_stats(measured)

    if latest.get("rest_measured") and not prev.get("rest_measured"):
        spotlight_p = (f"<p>First swim with the watch in Pool Swim mode — so for the first time these bars are "
                       f"<strong>measured rests, not estimates</strong>. The honest news: your median wait on {esc(l_date)} was about "
                       f"<strong>{l_stats['median_rest']:.0f} seconds</strong>, above the 20–45s active-rest band. The open-water "
                       f"estimates were kinder than reality. Nothing here is alarming — your swim pace is a genuine "
                       f"{latest['est_swim_per_length_s']:.0f}s per length — but the wall stops are where the next gain lives: "
                       f"touch, three breaths, go.</p>")
    elif latest.get("rest_measured"):
        inband_word = ("inside" if l_stats["median_rest"] is not None and l_stats["median_rest"] <= BAND_HI else "above")
        spotlight_p = (f"<p>Measured rests, no asterisks. Your median wait on {esc(l_date)} was about "
                       f"<strong>{l_stats['median_rest']:.0f} seconds</strong> — {inband_word} the 20–45s active-rest band, "
                       f"with {l_stats['inband_pct']}% of rests in band. Swim pace: a measured "
                       f"{latest['est_swim_per_length_s']:.0f}s per length. "
                       + ("The swimming isn't the limiter right now — the wall is. Touch, three breaths, go.</p>"
                          if inband_word == "above" else "Hold that and stretch the chunks.</p>"))
    else:
        spotlight_p = (f"<p>Here's the thing you told me: <em>\"I swim 25 yards, wait a minute.\"</em> The data disagrees, in your favor. "
                       f"Your median wait on {esc(l_date)} was about <strong>{l_stats['median_rest']:.0f} seconds</strong> — inside the "
                       f"20–45s active-rest band the protocol demands. The red bars are the full resets, and when a red bar wears a ring, "
                       f"you spent that rest deliberately and paid for it with a back-to-back 50: that's not drift, that's instinct, and it's credited.</p>")

    rows = []
    table_entries = sorted([(s, None) for s in july] + excluded, key=lambda e: e[0]["start"])
    for s, reason in table_entries:
        d = datetime.fromisoformat(s["start"])
        if reason is not None:
            rows.append(f"<tr style='opacity:0.45'><td>{d.strftime('%b %-d')}</td>"
                        f"<td><span class='badge muted'>{esc(reason)} · not counted</span></td>"
                        f"<td>{s['lengths']}</td><td>{s['true_distance_m']:.0f} m</td>"
                        f"<td>—</td><td>—</td><td>—</td><td>—</td></tr>")
            continue
        st = session_stats(s)
        rows.append(f"<tr><td>{d.strftime('%b %-d')}</td>"
                    f"<td><span class='badge{' muted' if s['era']=='baseline' else ''}'>{'video era' if s['era']=='video-era' else 'baseline'}</span></td>"
                    f"<td>{s['lengths']}</td><td>{s['true_distance_m']:.0f} m</td>"
                    f"<td>~{st['median_rest']:.0f}s</td><td>{st['inband_pct']}%</td>"
                    f"<td>{s['longest_run_lengths'] if s.get('rest_measured') else '—'}</td><td>{s['hr_avg']:.0f}</td></tr>")

    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Swim 1K — Coach's Report</title>
<style>{css}</style>
</head>
<body>
<main>

<header>
<p class="small" style="text-transform:uppercase;letter-spacing:0.08em;color:#8a8580">Swim 1K · a running project · updated from the {esc(gen_date)} export</p>
<h1>The road to 1,000 meters</h1>
<p class="intro">Coach's report for Cherian — graded against Alex Coci's Ocean Swim School protocol, with an honest eye on what the watch actually measured.</p>
</header>

<figure><img src="assets/hero-swimmer.png" alt="Flat illustration of a freestyle swimmer" class="rounded"></figure>

<section>
<h2>The verdict — {esc(l_date)}</h2>
<div class="card">
<div class="card-body">
<h3 style="margin-top:0">{esc(headline)}</h3>
<p><strong>The win.</strong> {esc(win)}</p>
<p><strong>The correction.</strong> {esc(fix)}</p>
<div class="card-row">
<div><p class="caption">lengths</p><p style="font-size:1.6rem;font-weight:600;margin:0">{latest['lengths']}</p></div>
<div><p class="caption">true distance</p><p style="font-size:1.6rem;font-weight:600;margin:0">{latest['true_distance_m']:.0f} m</p></div>
<div><p class="caption">rests in band</p><p style="font-size:1.6rem;font-weight:600;margin:0">{l_stats['inband_pct']}%</p></div>
</div>
</div>
</div>
<p class="small">"True distance" counts lengths × 22.86 m. The watch's own numbers run ~15% hot — see <a href="#honesty">how this is measured</a>.</p>
</section>

<section>
<h2>Where you stand</h2>
{goal_meter(best_run, cfg['goal_lengths'])}
<p>The bar looks short. It's supposed to. You're swimming close to <strong>a full kilometer of volume per session</strong> — the engine is there. What hasn't been trained yet is swimming continuously, and that's precisely what the five-week ladder below builds. This bar is the one number this whole project exists to move.</p>
</section>

<section>
<h2>The climb — session by session</h2>
{chart_progression(prog, cfg)}
<p>{esc(prog_coach)}</p>
</section>

<section>
<h2>Comfort ladder — mastery, not just the max</h2>
<div class="card">
<div class="card-body">
<h3 style="margin-top:0">{esc(comfort_base_line)}</h3>
<p class="small">{esc(comfort_missing)}</p>
</div>
</div>
<p class="small" style="margin-top:0.75rem">A run length only counts as "baseline" once it's routine, not a one-time high. A run = consecutive <strong>freestyle</strong> lengths joined by turn gaps of ≤{RUN_JOIN:.0f}s — other strokes still count as swimming volume, but they end the run, and any stop longer than {RUN_JOIN:.0f}s is a rest. Recovery after an N-lap run can never literally hit zero; at ≤{RUN_JOIN:.0f}s it simply becomes an (N+1)-lap run. The real compression target is a recovery ≤30s, and full compression shows up as the run count itself growing.</p>
{chart_comfort_compression(comfort_per_session, cfg)}
<p>{esc(comfort_hr_narrative(comfort_per_session))}</p>
</section>

<section>
<h2>50-yard capacity — multiply it, then compress it</h2>
<p>The headline "longest run" number has been stuck at 2 lengths for a month — but that hides the real progress underneath: you're now landing more 50s per session, and the rest between back-to-back 50s is compressing. These two dials are what turn a single 50 into a routine, not a personal best.</p>
{fifty_stat_row(fifty_stats)}
{chart_fifty_count(fifty_stats)}
<p style="margin-top:0.5rem"><span class="tag" style="background:{BLUE};color:#fff">2-length</span> <span class="tag" style="background:{AMBER};color:#fff">3+ length</span></p>
<p class="small">Bar = count of runs of 2+ credited lengths that session, stacked by length; % label = share of that session's total lengths spent inside a 2+ run.</p>
{chart_fifty_b2b(fifty_stats)}
<p class="small">Dot = each rest between back-to-back ≥2-length runs (only counted when the two runs are physically adjacent — no other length in between) · line = session median · dashed = {B2B_TARGET_S}s gateway target.</p>
</section>

<section>
<h2>What the coach saw — {esc(l_date)}</h2>
{chart_rest_strip(latest, l_stats)}
<p style="margin-top:0.5rem"><span class="tag" style="background:{BLUE};color:#fff">in band ≤45s</span> <span class="tag" style="background:{AMBER};color:#fff">over band 45–90s</span> <span class="tag" style="background:{RED};color:#fff">full reset &gt;90s</span> <span class="tag">○ credited — fueled a back-to-back run</span></p>
{spotlight_p}
</section>

<section>
<h2>The training log</h2>
<h3>Distance per session</h3>
{chart_volume(july, cfg)}
<p style="margin-top:0.5rem"><span class="tag" style="background:{BASELINE_C};color:#fff">baseline</span> <span class="tag" style="background:{ERA_C};color:#fff">video era</span></p>
<h3>Rest discipline per session</h3>
{rest_stat_row(measured)}
{chart_rest_band(measured)}
<p class="small">Blue line = session median of single-lap rests; band = middle 50% (computed from all singles, clipped at 120s); k↑ = k rests over 120s. Recoveries after back-to-back runs are excluded — see the comfort ladder.</p>
<p>{esc(rest_narrative(measured))}</p>
{rest_timeshare_table(measured)}
<p class="small">Share of classified time stopped at the wall, per session (mid-run turns excluded). Recovery = the pauses after back-to-back freestyle runs — never judged, but never hidden. The 30–60 split is where improvement shows — but each 10s slice holds only a handful of rests per session, so read the drift across days, not single-day jumps.</p>
<p>The under-30 share above flatters you — the rests that beat 30s are, by definition, your shortest ones, so they add up to 1–2% of your wall time. Where the minutes actually go: the &gt;90s singles (38% on Jul 28, your worst yet) and the recoveries.</p>
</section>

<section>
<h2>Stroke efficiency</h2>
<h3>Strokes per length</h3>
{chart_strokes(measured)}
<p class="small">Faint dot = every length; shaded box = middle 50%; black tick = session median. Green band = the 8–11 stroke glide zone you're training toward. Hollow red rings are watch glitches (implausible counts), excluded from every stat.</p>
<h3>SWOLF (seconds + strokes)</h3>
{chart_swolf(measured)}
<p class="small">SWOLF = swim time + strokes for the length — the honesty check, since gliding into a stall doesn't improve it. Bands: 35–45 "very good," 30–35 "elite" for a 25 yd pool.</p>
<p>{esc(stroke_narrative(measured))}</p>
</section>

<section>
<h2>Report card vs. the three mistakes</h2>
<table class="striped">
<thead><tr><th>The mistake (video)</th><th>Your grade</th><th>The coach's note</th></tr></thead>
<tbody>
<tr><td><strong>1 · Random laps, no plan</strong> <span class="small">(01:30)</span></td><td><span class="badge" style="background:{BLUE};color:#fff">B+</span></td><td>You now show up with a plan and repeat it. That's base training. Write it down before each swim to make it an A.</td></tr>
<tr><td><strong>2 · The wrong kind of rest</strong> <span class="small">(04:54)</span></td><td><span class="badge" style="background:{BLUE};color:#fff">B</span></td><td>Median rest ~{era_med:.0f}s is inside the 20–45s band. The remaining leak: the 90s+ full resets that aren't feeding a bigger chunk. One hand on the wall, stay ready.</td></tr>
<tr><td><strong>3 · Chasing the goal distance</strong> <span class="small">(08:12)</span></td><td><span class="badge" style="background:{AMBER};color:#fff">C</span></td><td>You're not overreaching — you're underreaching. One length per repeat is smaller than anything on the ladder. Time to chunk up: this is your growth edge.</td></tr>
</tbody>
</table>
</section>

<section>
<h2>Corrections — in order of payoff</h2>
<div class="callout">
<p class="callout-title">1 · Fix the watch — ✅ done, July 22</p>
<p>You switched to <strong>Pool Swim mode</strong> and it shows: exact laps, exact rests, honest distance. Sessions before July 22 keep their "estimated" asterisks; from here on the numbers are measured. Keep the setting.</p>
</div>
<div class="callout">
<p class="callout-title">2 · Double the chunk, keep the rest</p>
<p>This week: <strong>8 × 50 yd with 30–45 s rest</strong>, easy pace, every repeat at the same speed. Two lengths, touch, breathe, go — make the 50 the plan instead of the ceiling.</p>
</div>
<div class="callout">
<p class="callout-title">3 · Kill the innocent full resets</p>
<p>A 2-minute rest that buys a 50 is spent well. A 2-minute rest that buys another single 25 is Mistake #2 — your heart restarts from zero. If you're going to rest long, make it feed something bigger.</p>
</div>
</section>

<section>
<h2>The ladder to 1,000 m</h2>
<figure style="max-width:280px;margin-left:0"><img src="assets/goal-medal.png" alt="Illustration of a swim medal" class="rounded"></figure>
<p>Alex's five-week progression, translated to your pool (25 yd lengths; 100 m ≈ 4½ lengths, rounded to 5 for honesty — slightly over-distance, never under):</p>
<table class="bordered compact">
<thead><tr><th>Block</th><th>The set</th><th>Session total</th><th>You're ready when</th></tr></thead>
<tbody>
<tr><td>Now</td><td>8 × 50 yd @ 30–45s</td><td>≈ 366 m</td><td>all repeats feel the same, breathing steady</td></tr>
<tr><td>Next</td><td>10 × 5 lengths @ 30s</td><td>≈ 1,143 m</td><td>30s starts to feel like plenty of rest</td></tr>
<tr><td>Then</td><td>5 × 10 lengths @ 30s</td><td>≈ 1,143 m</td><td>same — chunks up, rest unchanged</td></tr>
<tr><td>Then</td><td>2 × 22 lengths @ 30s</td><td>≈ 1,006 m</td><td>finishing thinking "I could do one more"</td></tr>
<tr><td>Goal</td><td><strong>44 lengths, no stops</strong></td><td><strong>1,006 m</strong></td><td>—</td></tr>
</tbody>
</table>
<p><strong>Timeline:</strong> at your current five-swims-a-week rhythm, the honest window for the first continuous 1,000 m is <strong>late August to mid-September</strong>. Pool Swim mode is on as of July 22 — give me a few more measured swims and I'll commit to a date.</p>
</section>

<section>
<h2>Every session</h2>
<div class="table-wrap">
<table class="striped compact">
<thead><tr><th>Date</th><th>Era</th><th>Lengths</th><th>True dist.</th><th>Median rest*</th><th>In band*</th><th>Longest run*</th><th>Avg HR</th></tr></thead>
<tbody>{''.join(rows)}</tbody>
</table>
</div>
<p class="small">* estimated for sessions recorded in open-water mode (through Jul 21); measured for training swims from Jul 22 onward — see below. Longest run is shown for measured sessions only: open-water estimation fabricates back-to-back runs (your fastest laps get rests estimated near zero, chaining into runs that never happened). Muted rows are recorded but not counted toward any stat.</p>
</section>

<section id="honesty">
<h2>How this is measured (the honest part)</h2>
<aside>
<p><strong>Through July 21</strong> the watch was in open-water mode: no true laps or rests, just one timestamped record per detected length spanning <em>swim + the rest that followed</em>. For those sessions, swim time per length is estimated from the fastest cycles and rest = cycle − swim; every inherited number wears an asterisk or the word "estimated." <strong>From July 22</strong> training swims are in Pool Swim mode: each record spans only the swim, rest is the measured gap to the next push-off, and nothing is estimated. (Non-training sessions — e.g. diving — are archived as recorded but excluded from every stat.) In both eras, distances marked "true" are lengths × 22.86 m; the watch's own meters are discarded.</p>
</aside>
</section>

<hr class="decorative">
<p class="footer-note">Coach's canon: <a href="reference/protocol.md">the protocol</a>, extracted from <a href="reference/video.mp4">the video</a> (saved locally). Data: {len(july)} sessions from the {esc(gen_date)} Apple Health export · rebuild with <code>./update.sh &lt;export.zip&gt;</code>.</p>

</main>
</body>
</html>"""
    out = PROJECT / "index.html"
    out.write_text(page)
    print(f"Wrote {out}")


if __name__ == "__main__":
    build()
