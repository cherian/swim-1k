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
from datetime import datetime
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
STYLE_TEMPLATE = Path("/Users/cherianthomas/dev/Style Template/styleguide.html")

BLUE, AMBER, RED = "#2563eb", "#d97706", "#b91c1c"
BASELINE_C, ERA_C = "#b45309", "#2563eb"
BAND_LO, BAND_HI = 20, 45          # protocol active-rest band (video 06:29)
WAY_OVER = 90                      # beyond this, rest is a full reset
RUN_THRESHOLD = 15                 # rest below this ≈ back-to-back lengths
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


def session_stats(s):
    cycles = [c for c in s["cycles"] if not c.get("artifact")]
    cycles = cycles[:-1] if len(cycles) > 1 else cycles  # last cycle has no following rest
    rests = [c["rest_est_s"] for c in cycles if c["rest_est_s"] is not None]
    n = len(rests)
    inband = sum(1 for r in rests if r <= BAND_HI)
    med = sorted(rests)[n // 2] if n else None
    # deliberate long rest: way-over rest immediately followed by a back-to-back run
    deliberate = 0
    for i, c in enumerate(cycles[:-1]):
        nxt = cycles[i + 1]["rest_est_s"]
        if c["rest_est_s"] and c["rest_est_s"] > WAY_OVER and nxt is not None and nxt <= RUN_THRESHOLD:
            deliberate += 1
    return {
        "median_rest": med,
        "inband_pct": round(100 * inband / n) if n else None,
        "deliberate_long": deliberate,
        "date_label": datetime.fromisoformat(s["start"]).strftime("%a %b %-d"),
    }


def split_rests(s):
    """Classify each rest after an artifact-filtered cycle into one of three
    kinds, walking run length the same way derive_pool does:
      - single: not part of a 2+ run yet — counted against the 30s target
      - recovery: rest > RUN_THRESHOLD ending a 2+ run — deliberate, never counted
      - connector: rest <= RUN_THRESHOLD mid a 2+ run — excluded entirely
    A None rest resets run state.
    """
    cycles = [c for c in s["cycles"] if not c.get("artifact")]
    out = []
    run = 1
    for c in cycles:
        r = c["rest_est_s"]
        if r is None:
            run = 1
            continue
        kind = ("recovery" if r > RUN_THRESHOLD else "connector") if run >= 2 else "single"
        out.append({"rest_s": r, "kind": kind, "start": c["start"]})
        run = run + 1 if r <= RUN_THRESHOLD else 1
    return out


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
    cycles = [c for c in s["cycles"] if not c.get("artifact")][:-1]
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
        nxt = cycles[i + 1]["rest_est_s"] if i + 1 < len(cycles) else None
        credited = st == "wayover" and nxt is not None and nxt <= RUN_THRESHOLD
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
REST_CAP_S = 150


def chart_rest_dots(measured):
    """Every single-lap rest as a dot vs a 30s target; hollow grey recovery
    dots (deliberate, after a 2+ run) shown for honesty but never counted."""
    W, H, pad_l, pad_r, pad_b, pad_t = 1000, 380, 52, 20, 80, 20
    plot_h = H - pad_b - pad_t
    n = len(measured)
    slot = (W - pad_l - pad_r) / max(n, 1)

    def y(v):
        return pad_t + plot_h * (1 - min(v, REST_CAP_S) / REST_CAP_S)

    g = [svg_open(W, H, "Every single-lap rest per session vs a 30s target")]
    g.append(f'<rect x="{pad_l}" y="{y(REST_TARGET_S):.1f}" width="{W-pad_l-pad_r}" '
             f'height="{y(0)-y(REST_TARGET_S):.1f}" fill="{BLUE}" opacity="0.07"/>')
    g.append(f'<line x1="{pad_l}" x2="{W-pad_r}" y1="{y(REST_TARGET_S):.1f}" y2="{y(REST_TARGET_S):.1f}" '
             f'stroke="{BLUE}" stroke-width="2" stroke-dasharray="7 4"/>')
    g.append(f'<text x="{W-pad_r}" y="{y(REST_TARGET_S)-8:.1f}" text-anchor="end" font-size="13" '
             f'fill="{BLUE}" font-weight="600">TARGET 30s</text>')
    for gv in (30, 60, 90, 120):
        g.append(f'<line x1="{pad_l}" x2="{W-pad_r}" y1="{y(gv):.1f}" y2="{y(gv):.1f}" stroke="#e2ded9" stroke-width="1"/>')
        g.append(f'<text x="{pad_l-8}" y="{y(gv)+4:.1f}" text-anchor="end" font-size="13" fill="#8a8580">{gv}s</text>')

    if not measured:
        g.append("</svg>")
        return "".join(g)

    for i, s in enumerate(measured):
        rests = split_rests(s)
        singles = [r["rest_s"] for r in rests if r["kind"] == "single"]
        col_x = pad_l + i * slot + slot / 2
        col_w = min(120, slot - 20)
        d = datetime.fromisoformat(s["start"])

        for j, r in enumerate(rests):
            if r["kind"] == "connector":
                continue
            x = col_x + _jitter(s["id"], j, col_w)
            v = r["rest_s"]
            capped = v > REST_CAP_S
            cy = y(v)
            t = datetime.fromisoformat(r["start"]).strftime("%-I:%M %p")
            if r["kind"] == "recovery":
                g.append(f'<circle cx="{x:.1f}" cy="{cy:.1f}" r="5" fill="none" stroke="#8a8580" '
                         f'stroke-width="1.5" opacity="0.8"><title>{t} · {v:.0f}s recovery after a '
                         f'back-to-back run (planned, not counted)</title></circle>')
                if capped:
                    g.append(f'<text x="{x:.1f}" y="{cy-8:.1f}" text-anchor="middle" font-size="11" '
                             f'fill="#8a8580">&#8593;</text>')
            else:
                color = BLUE if v < 30 else (AMBER if v <= 90 else RED)
                g.append(f'<circle cx="{x:.1f}" cy="{cy:.1f}" r="5" fill="{color}" opacity="0.75">'
                         f'<title>{t} · {v:.0f}s single-lap rest</title></circle>')
                if capped:
                    g.append(f'<text x="{x:.1f}" y="{cy-8:.1f}" text-anchor="middle" font-size="11" '
                             f'fill="{color}">&#8593;</text>')

        total = len(singles)
        if singles:
            med = statistics.median(singles)
            med_y = y(med)
            g.append(f'<line x1="{col_x-col_w/2:.1f}" x2="{col_x+col_w/2:.1f}" y1="{med_y:.1f}" y2="{med_y:.1f}" '
                     f'stroke="#222" stroke-width="2.5"><title>median of single-lap rests: {med:.0f}s</title></line>')
        g.append(f'<text x="{col_x:.1f}" y="{H-56}" text-anchor="middle" font-size="14" fill="#444">'
                 f'{d.strftime("%-m/%-d")}</text>')
        if total:
            under30 = sum(1 for v in singles if v < 30)
            under60 = sum(1 for v in singles if v < 60)
            g.append(f'<text x="{col_x:.1f}" y="{H-34}" text-anchor="middle" font-size="13" '
                     f'font-weight="700" fill="{BLUE}">{under30}/{total} under 30s</text>')
            g.append(f'<text x="{col_x:.1f}" y="{H-14}" text-anchor="middle" font-size="13" '
                     f'font-weight="700" fill="{BLUE}">{under60}/{total} under 60s</text>')
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
            f"(best day so far: {best['date']} at {best['pct']}%). It isn't a straight line up — "
            f"the two-minute recoveries after your 50s are the hollow dots, planned and never penalized.")


TIMESHARE_COLS = [("u30", BLUE, "&lt;30s", 700),
                  ("b3040", "#4478e8", "30–40s", 400),
                  ("b4050", "#6291ee", "40–50s", 400),
                  ("b5060", "#7aa7f7", "50–60s", 400),
                  ("b6090", AMBER, "60–90s", 400), ("o90", RED, "&gt;90s", 400),
                  ("rec", "#8a8580", "recovery", 400)]


def rest_timeshare_table(measured):
    """Wall-time share per session: seconds in each rest band as a share of all
    classified wall time (singles + recoveries; mid-run connectors excluded).
    Band boundaries match chart_rest_dots: exactly 30.0s -> 30-40, 90.0s -> 60-90."""
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
{chart_rest_dots(measured)}
<p class="small">Solid dots = single-lap rests judged against a 30s target (blue under 30s, amber 30–90s, red over 90s). Hollow grey dots = the ~2-minute recoveries after your 50-yard efforts — shown for honesty, never counted. Black tick = median of single-lap rests only.</p>
<p>{esc(rest_narrative(measured))}</p>
{rest_timeshare_table(measured)}
<p class="small">Share of classified time stopped at the wall, per session (mid-run turns excluded). Recovery = the planned pauses after 50-yard efforts — never judged, but never hidden. The 30–60 split is where improvement shows — but each 10s slice holds only a handful of rests per session, so read the drift across days, not single-day jumps.</p>
<p>The count view above flatters you — the rests that beat 30s are, by definition, your shortest ones, so they add up to 1–2% of your wall time. Where the minutes actually go: the &gt;90s singles (38% on Jul 28, your worst yet) and the recoveries.</p>
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
<p>This week: <strong>8 × 50 yd with 30–45 s rest</strong>, easy pace, every repeat at the same speed. Two lengths, touch, breathe, go. Your measured best is 2 back-to-back — this makes the 50 the plan instead of the ceiling.</p>
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
