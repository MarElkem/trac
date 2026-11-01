# Match Sync Viewer

A static website (no build step, no server code) that plays a match's **panoramic video**, **player tracking**
and **event annotations** on one clock. Switch between video, tactical pitch, or both; filter by event type,
side and visibility; jump or play only the filtered moments ("clip mode"); nudge the sync by eye.

It is built for the [SoccerTrack v2](https://github.com/AtomScott/SoccerTrack-v2) dataset (10 full matches with
4K panoramic video, game-state-reconstruction tracking and 12-class ball-action events, **CC BY 4.0**).

```
docs/                 the website (this folder is what GitHub Pages serves)
  index.html app.js style.css
  data/index.json     list of matches
  data/<id>/          match.json + tracking_1.json + tracking_2.json   (small, fine on GitHub)
  videos/             DEMO ONLY (synthetic). Real video lives off GitHub, see below.
tools/
  convert_soccertrack_v2.py   real dataset -> data/<id>/ (streams the 2.7 GB GSR files)
  make_demo.py                builds the synthetic demo match + test videos
  serve.py                    local server WITH range-request support
tests/test_viewer.py          47 Playwright checks, incl. frame-exact video sync
```

## 1. Try the demo (2 minutes)

```bash
python3 tools/serve.py docs 8000      # then open http://localhost:8000/
```

The demo match is **synthetic** (labelled in the UI). Its video has a frame counter burned into the corner and
its dots are drawn from the same tracking, so you can see sync is right. Opening `index.html` by double-click
will not work (browsers block `fetch` on `file://`); use the server. `python -m http.server` works for the
page but does not support range requests, so video seeking can misbehave — prefer `tools/serve.py`.

## 2. Use a real match

**Get the data** (needs a free Hugging Face account/token; files are large):

```bash
git clone https://github.com/AtomScott/SoccerTrack-v2 && cd SoccerTrack-v2
scripts/download.sh --dest ~/soccertrack-v2 --match 117093      # one match is plenty
```

**Convert** (pip install ijson; streams the GSR JSON, so RAM stays small):

```bash
python3 tools/convert_soccertrack_v2.py --root ~/soccertrack-v2 --match 117093 --out docs/data \
  --video-1 https://YOUR-VIDEO-HOST/117093_1st.mp4 \
  --video-2 https://YOUR-VIDEO-HOST/117093_2nd.mp4
```

This writes `docs/data/117093/{match.json,tracking_1.json,tracking_2.json}` (a few MB) and adds the match to
`docs/data/index.json`. `--video-1 a.webm,a.mp4` lists several sources; the first the browser can play wins.
Tracking is downsampled 25 → 5 fps and the viewer interpolates between frames.

**Prepare the video.** The originals are 4K panoramas — far too big to stream comfortably. Re-encode:

```bash
ffmpeg -i 117093_panorama_1st_half.mp4 -vf scale=1920:-2 -c:v libx264 -crf 27 -preset slow \
       -g 50 -an -movflags +faststart 117093_1st.mp4
```

`+faststart` lets playback begin before the file is fully downloaded and `-g 50` (a keyframe every 2 s) keeps
seeking quick. Do the same for the second half. If you only have one full-match file, point both halves at it
and set `--video-offset-1` / `--video-offset-2` to the seconds into that file where each half's tracking
starts (find them with the calibration sliders, step 3).

**Host the video off GitHub.** GitHub Pages has a 1 GB site limit and a 100 MB per-file limit, so keep only the
small `docs/data` there. The video host must support (a) **HTTP range requests** and (b) **CORS**
(`Access-Control-Allow-Origin: *`) — both are needed for seeking. Cloudflare R2 (public bucket + CORS rule),
Backblaze B2, Bunny Storage/CDN, an S3 bucket behind CloudFront, or a Hugging Face dataset file URL all work in
principle; check with `curl -I -H "Range: bytes=0-99" <url>` and expect `206 Partial Content`.
YouTube/Vimeo links will not work, because the page needs a raw `<video>` it can seek frame by frame.

**Publish.** Commit `docs/` to a GitHub repo → Settings → Pages → *Deploy from a branch* → `main` / `/docs`.
Deep links work: `?match=117093&h=2&t=640` opens the 2nd half at 10:40. `?data=https://…/data` loads match
data from another host; `?v1=…&v2=…` override the video URLs (handy for testing).

## 3. Calibrate the sync (important)

The format docs and the shipped files disagree in several places (see caveats), and nobody can verify frame
alignment for you, so the viewer has a **Sync calibration** panel:

* **Video offset** — seconds into the video at which tracking t = 0 occurs (default 0, because the dataset's
  half videos are trimmed to kickoff). If dots on the tactical view run *ahead* of players in the video,
  increase it. ±1-frame buttons for fine tuning.
* **Event offset** — shifts all events in the half. Event times are converted from the match-absolute
  timestamps with a nominal 45-minute half, but real 2nd halves start slightly off that after stoppage time.
  Pick a clear event (a goal, a kick-off, a throw-in) and slide until it lines up.

Settings persist in the browser. **Copy calibration** gives JSON you can bake into `match.json`
(`videoOffset`, `eventOffsetMs` per half) so every visitor sees the corrected sync.

## 4. Controls

`Space` play/pause · `←/→` ±5 s (`Shift` = ±1 frame) · `J`/`K` next/previous filtered event · `V` cycle view.
Alt-click a label chip to show only that label. **Clip mode** plays only the filtered events (N seconds before
and after each) and skips everything between.

## What has and has not been tested

* Tested (47 automated browser checks on the synthetic match): video frame on screen = master clock ±1 frame
  after seeking and during playback, tracking ↔ video content agree, half switching, every filter, clip mode,
  next/previous event, calibration (and persistence), keyboard, graceful fallback when the video is missing, no
  horizontal scroll at phone width.
* **Not tested on a real SoccerTrack v2 file** — the build environment could not download the dataset. The
  converter is written against the format docs and the loader's documented fixes for the shipped files
  (SoccerNet-COCO object, string `image_id` with a sequence prefix and 1-indexed frame, `attributes`,
  `bbox_pitch.x/y_bottom_middle`, `actions` key, UPPER-CASE labels, absolute `position`) and also accepts the
  documented flat-array layout. If a real file trips it, send me the error plus the first ~2 KB of the file and
  it should be a quick fix.
* Real-data quirks to expect: a few matches carry a third block of events without a half prefix (mapped into the
  2nd half); `jersey` can be missing (circle without a number); pitch is assumed 105 × 68 m; the ball is not part
  of the GSR tracking, so the tactical view shows players and referees only.

## Licence and attribution

Code: MIT (use freely). Data: SoccerTrack v2 is **CC BY 4.0** — keep the attribution line the converter writes
into `match.json` (it is shown in the page footer) and cite:

> Scott, A., Uchida, I., Kuroda, K., Kim, Y., Fujii, K. *SoccerTrack v2: A Full-Pitch Panoramic Video Dataset for
> Game State Reconstruction and Ball Action Spotting.* arXiv:2508.01802, 2025.

Redistributing the video and tracking on a public site is covered by CC BY 4.0 as long as you attribute and
indicate changes (downsampling, re-encoding), but check the dataset card on Hugging Face for any extra terms
before publishing the video publicly.
