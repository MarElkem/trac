#!/usr/bin/env python3
"""Convert one SoccerTrack v2 match into the small JSON files the viewer loads.

Input  (the dataset layout):  <root>/gsr/<match>/<match>_1st.json, _2nd.json   (up to ~2.7 GB each)
                              <root>/bas/<match>/<match>_12_class_events.json
Output (the viewer layout):   <out>/<match>/match.json, tracking_1.json, tracking_2.json
                              and <out>/index.json gets an entry for the match.

GSR files are STREAMED with ijson (never fully loaded). Both the shipped SoccerNet-COCO layout
({"images":..., "annotations":[...]}) and the documented flat-array layout are accepted.

Example:
  python3 tools/convert_soccertrack_v2.py --root ~/soccertrack-v2 --match 117093 \
      --video-1 https://videos.example.com/117093_1st.mp4 \
      --video-2 https://videos.example.com/117093_2nd.mp4 --out docs/data

Positions are written in decimetres (ints) at --fps (default 5) to keep files small.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

try:
    import ijson
except ImportError:  # pragma: no cover
    sys.exit("This script needs ijson:  pip install ijson")

SRC_FPS = 25
HALF_MS = 45 * 60 * 1000  # nominal half length: BAS positions are absolute from match start
BAS_LABELS = (
    "Pass", "Drive", "Header", "High Pass", "Out", "Cross", "Throw In", "Shot",
    "Ball Player Block", "Player Successful Tackle", "Free Kick", "Goal",
)
_LABEL_BY_FOLD = {l.casefold(): l for l in BAS_LABELS}
ATTRIBUTION = (
    "Data: SoccerTrack v2 (Scott, Uchida, Kuroda, Kim, Fujii; arXiv:2508.01802), licensed CC BY 4.0 "
    "(github.com/AtomScott/SoccerTrack-v2). Tracking downsampled to 5 fps and reformatted for this viewer."
)


# ------------------------------------------------------------------ GSR -----------------------

def _frame_index(image_id) -> int | None:
    """Zero-based frame index within the half.

    Shipped files: string = sequence prefix + 1-indexed 6-digit frame ("3000001" -> frame 0).
    Documented spec: integer, already zero-indexed.
    """
    if isinstance(image_id, str):
        s = image_id.strip()
        if not s.lstrip("-").isdigit():
            return None
        if len(s) > 6:
            return int(s[-6:]) - 1
        return int(s) - 1
    if isinstance(image_id, (int, float)):
        return int(image_id)
    return None


def _xy(rec: dict):
    bp = rec.get("bbox_pitch")
    if isinstance(bp, dict) and "x_bottom_middle" in bp and "y_bottom_middle" in bp:
        return bp["x_bottom_middle"], bp["y_bottom_middle"]
    if "x" in rec and "y" in rec:
        return rec["x"], rec["y"]
    return None


def _attrs(rec: dict):
    a = rec.get("attributes") or {}
    role = a.get("role", rec.get("role"))
    team = a.get("team", rec.get("team_side", rec.get("team")))
    jersey = a.get("jersey", rec.get("jersey_number", rec.get("jersey")))
    if jersey in ("", None):
        jersey = None
    else:
        try:
            jersey = int(jersey)
        except (TypeError, ValueError):
            jersey = None
    return team, role or "player", jersey


def _first_char(path: Path) -> str:
    with open(path, "rb") as f:
        while True:
            c = f.read(1)
            if not c:
                return ""
            if not c.isspace():
                return c.decode()


def convert_gsr(path: Path, fps_out: int) -> dict:
    """Stream one half's GSR file -> compact tracking dict."""
    if SRC_FPS % fps_out:
        raise SystemExit(f"--fps must divide {SRC_FPS}")
    step = SRC_FPS // fps_out
    prefix = "item" if _first_char(path) == "[" else "annotations.item"
    by_frame: dict[int, list] = defaultdict(list)
    votes: dict[int, Counter] = defaultdict(Counter)
    max_frame = -1
    n_obj = 0
    with open(path, "rb") as f:
        for rec in ijson.items(f, prefix, use_float=True):
            if not isinstance(rec, dict):
                continue
            if rec.get("supercategory", "object") != "object":
                continue  # pitch / camera records
            if "track_id" not in rec:
                continue
            fr = _frame_index(rec.get("image_id"))
            xy = _xy(rec)
            if fr is None or xy is None or fr < 0:
                continue
            max_frame = max(max_frame, fr)
            n_obj += 1
            if fr % step:
                continue
            tid = int(rec["track_id"])
            team, role, jersey = _attrs(rec)
            votes[tid][(team, role, jersey)] += 1
            by_frame[fr // step].append((tid, int(round(xy[0] * 10)), int(round(xy[1] * 10))))
    if max_frame < 0:
        raise SystemExit(f"{path}: no usable object annotations found")
    n = max_frame // step + 1
    frames = []
    for i in range(n):
        row: list[int] = []
        for tid, x, y in by_frame.get(i, ()):
            row += [tid, x, y]
        frames.append(row)
    tracks = {}
    for tid, c in votes.items():
        (team, role, jersey), _ = c.most_common(1)[0]
        tracks[str(tid)] = [team, role, jersey]
    print(f"  {path.name}: {n_obj} object records, {max_frame + 1} source frames -> {n} @ {fps_out} fps, "
          f"{len(tracks)} tracks", file=sys.stderr)
    return {"fps": fps_out, "n": n, "tracks": tracks, "frames": frames, "srcFrames": max_frame + 1}


# ------------------------------------------------------------------ BAS -----------------------

def convert_bas(path: Path, mode: str = "auto") -> list[dict]:
    data = json.loads(path.read_text())
    for key in ("actions", "annotations"):
        if key in data:
            raw = data[key]
            break
    else:
        raise SystemExit(f"{path.name}: expected 'actions' or 'annotations', found {sorted(data)}")
    parsed = []
    for a in raw:
        gt = str(a.get("gameTime", ""))
        pos = int(a["position"])
        if " - " in gt:
            half = int(gt.split(" - ", 1)[0])
        else:  # events without half prefix: infer the period from absolute position
            half = pos // HALF_MS + 1
        label = _LABEL_BY_FOLD.get(str(a["label"]).casefold())
        if label is None:
            print(f"  warning: unknown BAS label {a['label']!r} kept as-is", file=sys.stderr)
            label = str(a["label"])
        parsed.append((min(half, 2), pos, label, a))
    # absolute vs relative positions (docs say relative, shipped files are absolute)
    h2 = [p for h, p, *_ in parsed if h == 2]
    if mode == "auto":
        mode = "relative" if h2 and min(h2) < HALF_MS * 0.5 else "absolute"
    print(f"  BAS: {len(parsed)} events, positions treated as {mode}", file=sys.stderr)
    events = []
    for half, pos, label, a in parsed:
        t = pos - ((half - 1) * HALF_MS if mode == "absolute" else 0)
        events.append({
            "half": half, "t": t, "label": label, "team": a.get("team"),
            "player": a.get("player_id"), "vis": a.get("visibility"), "clock": a.get("gameTime"),
        })
    events.sort(key=lambda e: (e["half"], e["t"]))
    return events


# ------------------------------------------------------------------ main ----------------------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", required=True, help="dataset root containing gsr/ and bas/")
    ap.add_argument("--match", required=True, help="match id, e.g. 117093")
    ap.add_argument("--out", required=True, help="viewer data directory (e.g. docs/data)")
    ap.add_argument("--fps", type=int, default=5, help="output tracking fps (divides 25; default 5)")
    ap.add_argument("--title", default=None)
    ap.add_argument("--video-1", default="", help="1st-half video URL(s), comma separated (first playable wins)")
    ap.add_argument("--video-2", default="", help="2nd-half video URL(s), comma separated")
    ap.add_argument("--video-offset-1", type=float, default=0.0,
                    help="seconds into video 1 at which tracking t=0 occurs (default 0: videos are trimmed to kickoff)")
    ap.add_argument("--video-offset-2", type=float, default=0.0)
    ap.add_argument("--event-offset-ms-1", type=int, default=0, help="shift all half-1 events by this many ms")
    ap.add_argument("--event-offset-ms-2", type=int, default=0, help="shift all half-2 events (stoppage-time drift)")
    ap.add_argument("--bas-mode", choices=["auto", "absolute", "relative"], default="auto")
    ap.add_argument("--pitch", default="105x68")
    ap.add_argument("--synthetic", action="store_true", help="mark the match as synthetic demo data")
    args = ap.parse_args()

    root, mid = Path(args.root), str(args.match)
    gsr_dir, bas_file = root / "gsr" / mid, root / "bas" / mid / f"{mid}_12_class_events.json"
    out_dir = Path(args.out) / mid
    out_dir.mkdir(parents=True, exist_ok=True)

    halves = []
    for h, suffix, vid, voff, eoff in (
        (1, "1st", args.video_1, args.video_offset_1, args.event_offset_ms_1),
        (2, "2nd", args.video_2, args.video_offset_2, args.event_offset_ms_2),
    ):
        src = gsr_dir / f"{mid}_{suffix}.json"
        if not src.exists():
            print(f"  skipping half {h}: {src} not found", file=sys.stderr)
            continue
        trk = convert_gsr(src, args.fps)
        (out_dir / f"tracking_{h}.json").write_text(json.dumps(trk, separators=(",", ":")))
        halves.append({
            "half": h, "tracking": f"tracking_{h}.json",
            "video": [v.strip() for v in vid.split(",") if v.strip()],
            "videoOffset": voff, "eventOffsetMs": eoff,
            "durationS": round(trk["srcFrames"] / SRC_FPS, 2),
        })
    if not halves:
        raise SystemExit("no GSR half files found; check --root/--match")

    events = convert_bas(bas_file, args.bas_mode) if bas_file.exists() else []
    if not events:
        print(f"  warning: no BAS events at {bas_file}", file=sys.stderr)

    pw, ph = (float(v) for v in args.pitch.lower().split("x"))
    title = args.title or f"SoccerTrack v2 · match {mid}"
    match = {"id": mid, "title": title, "pitch": [pw, ph], "halves": halves, "events": events,
             "attribution": "Synthetic demo data generated by tools/make_demo.py (not a real match)."
                    if args.synthetic else ATTRIBUTION, "synthetic": args.synthetic}
    (out_dir / "match.json").write_text(json.dumps(match, separators=(",", ":")))

    idx_path = Path(args.out) / "index.json"
    idx = json.loads(idx_path.read_text()) if idx_path.exists() else {"matches": []}
    idx["matches"] = [m for m in idx["matches"] if m["id"] != mid] + [
        {"id": mid, "title": title, "path": f"{mid}/match.json"}]
    idx_path.write_text(json.dumps(idx, indent=1))
    sizes = ", ".join(f"{p.name} {p.stat().st_size / 1e6:.1f} MB" for p in sorted(out_dir.iterdir()))
    print(f"wrote {out_dir}  ({sizes})", file=sys.stderr)


if __name__ == "__main__":
    main()
