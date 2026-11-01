#!/usr/bin/env python3
"""Generate a SYNTHETIC demo match (not real data) in the same raw layout as SoccerTrack v2.

It writes
  <out>/fixtures/gsr/<id>/<id>_1st.json, _2nd.json   (SoccerNet-COCO style, as shipped)
  <out>/fixtures/bas/<id>/<id>_12_class_events.json  (key "actions", UPPER CASE labels,
                                                      positions absolute from match start)
  <out>/videos/<id>_1st.{webm,mp4}, _2nd.{webm,mp4}  (synthetic "panoramic" video with a
                                                      frame counter encoded in the top-left
                                                      pixels so sync can be tested)

Run:  python3 tools/make_demo.py --out build_demo
Then: python3 tools/convert_soccertrack_v2.py --root build_demo/fixtures --match demo ...
"""
from __future__ import annotations

import argparse
import json
import math
import random
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FPS = 25
HALF_S = 120  # demo halves are 2 minutes long
HALF_MS = 45 * 60 * 1000  # nominal half length used by the real data's BAS positions
VIDEO_LEAD_S = {1: 2.0, 2: 1.0}  # seconds of pre-kickoff footage at the start of each video
W, H = 960, 384
LABELS = [
    "PASS", "DRIVE", "HEADER", "HIGH PASS", "OUT", "CROSS", "THROW IN", "SHOT",
    "BALL PLAYER BLOCK", "PLAYER SUCCESSFUL TACKLE", "FREE KICK", "GOAL",
]
LABEL_WEIGHTS = [30, 18, 5, 6, 8, 5, 5, 6, 4, 5, 3, 2]

# 4-4-2 in pitch metres for the "left" side (attacks to +x); x in [-52.5, 52.5], y in [-34, 34]
FORMATION = [
    (-48, 0), (-33, -22), (-35, -8), (-35, 8), (-33, 22),
    (-16, -24), (-18, -8), (-18, 8), (-16, 24), (-4, -9), (-4, 9),
]


def ball_pos(t: float) -> tuple[float, float]:
    return 38 * math.sin(t / 17.0), 24 * math.sin(t / 11.0 + 1.0)


def sim_half(half: int, seed: int = 7):
    """Return {frame_index: [(track_id, x, y), ...]} and metadata for 22 players + 1 referee."""
    rng = random.Random(seed + half)
    # "left"/"right" sides swap between halves, like in the real data
    side_of_team = {"A": "left", "B": "right"} if half == 1 else {"A": "right", "B": "left"}
    players = []  # (track_id, team, role, jersey, base_x, base_y, phase)
    for team_idx, team in enumerate(("A", "B")):
        for k, (bx, by) in enumerate(FORMATION):
            tid = team_idx * 11 + k + 1
            sign = 1 if team == "A" else -1
            players.append(
                (tid, team, "goalkeeper" if k == 0 else "player", k + 1 if k else 1,
                 sign * bx if half == 1 else -sign * bx, sign * by, rng.uniform(0, 6.28))
            )
    frames: dict[int, list[tuple[int, float, float]]] = {}
    n = HALF_S * FPS
    for i in range(n):
        t = i / FPS
        bx, by = ball_pos(t)
        ents = []
        for tid, team, role, jersey, px, py, ph in players:
            if rng.random() < 0.03:  # occasional missing detection
                continue
            pull = 0.18 if role == "player" else 0.04
            x = px + (bx - px) * pull + 2.5 * math.sin(t / 5.0 + ph)
            y = py + (by - py) * pull + 2.0 * math.cos(t / 6.0 + ph)
            ents.append((tid, round(x, 2), round(y, 2)))
        rx = bx * 0.5 + 3.0 * math.sin(t / 9.0)
        ry = by * 0.5 + 3.0 * math.cos(t / 8.0)
        ents.append((23, round(rx, 2), round(ry, 2)))
        frames[i] = ents
    meta = {tid: (side_of_team[team], role, jersey) for tid, team, role, jersey, *_ in players}
    meta[23] = (None, "referee", None)
    return frames, meta


def write_gsr(path: Path, half: int, frames, meta, match_id: str) -> None:
    """Write a SoccerNet-COCO style file like the real release (see format-gsr.md, 'As shipped')."""
    seq = {1: "3", 2: "4"}[half]
    images, anns = [], []
    ann_id = 0
    for i, ents in frames.items():
        image_id = f"{seq}{i + 1:06d}"  # sequence prefix + 1-indexed 6-digit frame
        images.append({"image_id": image_id, "file_name": f"{i + 1:06d}.jpg", "width": 3840, "height": 1504,
                       "is_labeled": True})
        for tid, x, y in ents:
            side, role, jersey = meta[tid]
            ann_id += 1
            anns.append({
                "id": f"{seq}{ann_id:08d}", "image_id": image_id, "track_id": tid,
                "supercategory": "object", "category_id": 1,
                "attributes": {"role": role, "jersey": None if jersey is None else str(jersey),
                               "team": side, "player_id": 170000 + tid},
                "bbox_image": {"x": 100, "y": 100, "x_center": 120.0, "y_center": 130.0, "w": 40, "h": 60},
                "bbox_pitch": {"x_bottom_left": x, "y_bottom_left": y, "x_bottom_middle": x,
                               "y_bottom_middle": y, "x_bottom_right": x, "y_bottom_right": y},
                "bbox_pitch_raw": {"x_bottom_middle": x, "y_bottom_middle": y},
            })
        # a non-object record kind, like the pitch-lines records in the real files
        anns.append({"id": f"p{ann_id}", "image_id": image_id, "supercategory": "pitch", "category_id": 2,
                     "lines": {"Side line top": [{"x": 0.1, "y": 0.2}]}})
    doc = {"info": {"version": "1.3", "seq_length": len(frames), "frame_rate": FPS, "clip_start": "0",
                    "clip_stop": "30000"},
           "images": images, "annotations": anns,
           "categories": [{"id": 1, "name": "player"}, {"id": 2, "name": "pitch"}]}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc))


def make_events(seed: int = 11):
    rng = random.Random(seed)
    acts = []
    for half in (1, 2):
        times = sorted(rng.uniform(4, HALF_S - 4) for _ in range(36))
        for t in times:
            label = rng.choices(LABELS, LABEL_WEIGHTS)[0]
            side = rng.choice(["left", "right"])
            pos = int(round(t * 1000)) + (half - 1) * HALF_MS  # absolute from match start
            mm, ss = divmod(int(t), 60)
            acts.append({"gameTime": f"{half} - {mm:02d}:{ss:02d}", "position": str(pos), "label": label,
                         "team": side, "player_id": f"demo_{side[0].upper()}_{rng.randint(1, 11)}",
                         "visibility": rng.choice(["visible", "visible", "visible", "not shown"])})
        # make sure there is at least one goal per half to look at
        t = HALF_S * 0.6
        pos = int(t * 1000) + (half - 1) * HALF_MS
        acts.append({"gameTime": f"{half} - 01:12", "position": str(pos), "label": "SHOT", "team": "left",
                     "player_id": None, "visibility": "visible"})
        acts.append({"gameTime": f"{half} - 01:12", "position": str(pos), "label": "GOAL", "team": "left",
                     "player_id": None, "visibility": "visible"})
    acts.sort(key=lambda a: int(a["position"]))
    return {"UrlLocal": "demo", "UrlYoutube": None, "actions": acts}


# ---------------------------------------------------------------- video ----------------------

def pitch_to_px(x: float, y: float) -> tuple[float, float]:
    return 40 + (x + 52.5) / 105 * (W - 80), 30 + (34 - y) / 68 * (H - 60)


def render_video(frames, meta, half: int, out_base: Path) -> None:
    lead = VIDEO_LEAD_S[half]
    n_total = int((HALF_S + lead + 1) * FPS)
    font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf", 22)
    cmd_common = ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
                  "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-"]
    procs = {
        "webm": subprocess.Popen(cmd_common + ["-c:v", "libvpx-vp9", "-crf", "32", "-b:v", "0", "-deadline",
                                               "realtime", "-cpu-used", "8", "-g", "25",
                                               f"{out_base}.webm"], stdin=subprocess.PIPE),
        "mp4": subprocess.Popen(cmd_common + ["-c:v", "libx264", "-preset", "veryfast", "-crf", "26", "-g", "25",
                                              "-pix_fmt", "yuv420p", "-movflags", "+faststart", f"{out_base}.mp4"],
                                stdin=subprocess.PIPE),
    }
    for f in range(n_total):
        img = Image.new("RGB", (W, H), (28, 70, 44))
        d = ImageDraw.Draw(img)
        d.rectangle([40, 30, W - 40, H - 30], outline=(230, 230, 230), width=2)
        d.line([W / 2, 30, W / 2, H - 30], fill=(230, 230, 230), width=2)
        d.ellipse([W / 2 - 40, H / 2 - 40, W / 2 + 40, H / 2 + 40], outline=(230, 230, 230), width=2)
        t = f / FPS - lead  # tracking time shown by this video frame
        idx = int(round(t * FPS))
        if idx >= 0:
            for tid, x, y in frames.get(idx, []):
                px, py = pitch_to_px(x, y)
                side = meta[tid][0]
                col = (70, 130, 255) if side == "left" else (255, 90, 80) if side == "right" else (255, 220, 40)
                d.ellipse([px - 6, py - 6, px + 6, py + 6], fill=col)
        else:
            d.text((W / 2 - 150, H / 2 - 12), "PRE-KICKOFF", font=font, fill=(255, 255, 255))
        d.text((60, H - 26), f"video {f / FPS:7.2f}s", font=font, fill=(255, 255, 255))
        # frame counter: 24 bits of f as 8x8 black/white squares in the top-left corner
        d.rectangle([0, 0, 24 * 8, 8], fill=(0, 0, 0))
        for b in range(24):
            if (f >> b) & 1:
                d.rectangle([b * 8, 0, b * 8 + 7, 7], fill=(255, 255, 255))
        raw = img.tobytes()
        for p in procs.values():
            p.stdin.write(raw)
    for p in procs.values():
        p.stdin.close()
    for p in procs.values():
        p.wait()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--no-video", action="store_true")
    args = ap.parse_args()
    out = Path(args.out)
    mid = "demo"
    for half, suffix in ((1, "1st"), (2, "2nd")):
        frames, meta = sim_half(half)
        write_gsr(out / "fixtures" / "gsr" / mid / f"{mid}_{suffix}.json", half, frames, meta, mid)
        if not args.no_video:
            (out / "videos").mkdir(parents=True, exist_ok=True)
            render_video(frames, meta, half, out / "videos" / f"{mid}_{suffix}")
    bas = out / "fixtures" / "bas" / mid
    bas.mkdir(parents=True, exist_ok=True)
    (bas / f"{mid}_12_class_events.json").write_text(json.dumps(make_events()))
    print("wrote", out)


if __name__ == "__main__":
    main()
