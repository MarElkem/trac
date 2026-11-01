#!/usr/bin/env python3
"""End-to-end checks for the viewer against the synthetic demo match (needs: pip install playwright).

  python3 tools/serve.py docs 8765 &       # range-capable server
  python3 tests/test_viewer.py http://localhost:8765/ [screenshot_dir]

The demo videos carry a 24-bit frame counter in their top-left pixels, so we can decode which video
frame is on screen and check it against the master clock: video_frame = (T + videoOffset) * 25.
"""
import json
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

URL = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8765/"
SHOTS = Path(sys.argv[2]) if len(sys.argv) > 2 else None
ROOT = Path(__file__).resolve().parent.parent
MATCH = json.loads((ROOT / "docs/data/demo/match.json").read_text())

DECODE = """() => {
  const v = document.getElementById('video');
  const c = document.createElement('canvas'); c.width = v.videoWidth; c.height = v.videoHeight;
  const g = c.getContext('2d'); g.drawImage(v, 0, 0);
  let n = 0;
  for (let b = 0; b < 24; b++) { const p = g.getImageData(b * 8 + 4, 4, 1, 1).data; if (p[0] > 128) n |= (1 << b); }
  return n;
}"""

fails, passes = [], 0


def check(name, cond, detail=""):
    global passes
    if cond:
        passes += 1
        print(f"  ok   {name}")
    else:
        fails.append(name)
        print(f"  FAIL {name}  {detail}")


def wait_seeked(page, timeout=5000):
    page.wait_for_function("() => { const v = document.getElementById('video'); return !v.seeking && v.readyState >= 2; }",
                           timeout=timeout)
    page.wait_for_timeout(120)


def seek(page, t):
    page.evaluate("t => window.__sts.seek(t)", t)
    wait_seeked(page)


def vframe(page):
    return page.evaluate(DECODE)


def goals(half):
    off = MATCH["halves"][half - 1]["eventOffsetMs"]
    return sorted((e["t"] + off) / 1000 for e in MATCH["events"] if e["half"] == half and e["label"] == "Goal")


with sync_playwright() as p:
    browser = p.chromium.launch()
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    page = ctx.new_page()
    errors = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(URL)
    page.wait_for_function("() => window.__sts && window.__sts.S.match && window.__sts.S.usingVideo", timeout=15000)

    print("load")
    check("demo match listed", page.locator("#matchSel option").count() == 1)
    check("synthetic badge visible", page.locator("#badge").is_visible())
    labels = {e["label"] for e in MATCH["events"]}
    check("one chip per label", page.locator(".chip").count() == len(labels), f"{page.locator('.chip').count()} vs {len(labels)}")
    n_h1 = sum(1 for e in MATCH["events"] if e["half"] == 1)
    check("event list shows all half-1 events", page.locator(".ev").count() == n_h1, f"{page.locator('.ev').count()} vs {n_h1}")
    check("attribution shown", "synthetic" in page.locator("#attrib").inner_text().lower())

    print("view toggle")
    check("split shows both panes", page.locator("#pitch").is_visible() and page.locator("#video").is_visible())
    page.click('#viewSeg [data-view=video]')
    check("video view hides pitch", not page.locator("#pitch").is_visible() and page.locator("#video").is_visible())
    page.click('#viewSeg [data-view=tactical]')
    check("tactical view shows pitch", page.locator("#pitch").is_visible())
    vb = page.locator("#video").bounding_box()
    check("tactical view hides video visually", vb is None or vb["width"] <= 4, str(vb))
    page.click('#viewSeg [data-view=split]')

    print("sync, half 1 (videoOffset = %.1f s)" % MATCH["halves"][0]["videoOffset"])
    off1 = MATCH["halves"][0]["videoOffset"]
    for T in (0.0, 10.0, 47.32, 95.0):
        seek(page, T)
        got, want = vframe(page), round((T + off1) * 25)
        check(f"seek T={T}: video frame {got} ≈ {want}", abs(got - want) <= 1, f"got {got} want {want}")
    seek(page, 30)
    # video drew the pitch dots from the same tracking; compare a dot's pixel position with the tactical canvas
    # (verifies tracking frame <-> video frame mapping, not just the counter)
    dot = page.evaluate("""() => {
      const v = document.getElementById('video'); const c = document.createElement('canvas');
      c.width = v.videoWidth; c.height = v.videoHeight; const g = c.getContext('2d'); g.drawImage(v, 0, 0);
      const d = g.getImageData(0, 0, c.width, c.height).data; let sx = 0, sy = 0, n = 0;
      for (let y = 12; y < c.height - 30; y++) for (let x = 0; x < c.width; x++) {
        const i = (y * c.width + x) * 4; if (d[i] < 120 && d[i + 1] < 170 && d[i + 2] > 220) { sx += x; sy += y; n++; }
      }
      return n ? [sx / n, sy / n, n] : null; }""")
    S = page.evaluate("""() => { const s = window.__sts.S, trk = s.trk[s.half], f = Math.round(s.T * trk.fps);
      const row = trk.frames[f]; let sx = 0, sy = 0, n = 0;
      for (let k = 0; k < row.length; k += 3) { const m = trk.tracks[row[k]]; if (m[0] === 'left') { sx += row[k+1]/10; sy += row[k+2]/10; n++; } }
      return [sx / n, sy / n, n]; }""")
    if dot and S[2]:
        W, H = 960, 384
        cx_v, cy_v = (dot[0] - 40) / (W - 80) * 105 - 52.5, 34 - (dot[1] - 30) / (H - 60) * 68
        check("centroid of blue dots in video ≈ tracking centroid (within 2 m)",
              abs(cx_v - S[0]) < 2 and abs(cy_v - S[1]) < 2, f"video ({cx_v:.1f},{cy_v:.1f}) tracking ({S[0]:.1f},{S[1]:.1f})")
    else:
        check("blue dots found in video frame", False, str(dot))

    print("playback")
    seek(page, 20)
    t0, w0 = page.evaluate("window.__sts.S.T"), time.time()
    page.click("#playBtn")
    page.wait_for_timeout(3000)
    t1, w1 = page.evaluate("window.__sts.S.T"), time.time()
    fr = vframe(page)
    page.click("#playBtn")
    adv, wall = t1 - t0, w1 - w0
    check(f"clock advanced {adv:.2f}s in {wall:.2f}s wall", abs(adv - wall) < 0.45, "")
    check("play button toggled back", "Play" in page.inner_text("#playBtn"))
    # the frame we decoded was just after t1 was read; allow some latency
    expect = (t1 + off1) * 25
    check(f"video frame on screen tracks clock (drift {abs(fr - expect):.1f} frames)", abs(fr - expect) < 20, f"{fr} vs {expect}")
    page.click('#speedSel')
    page.select_option("#speedSel", "2")
    seek(page, 10)
    a = page.evaluate("window.__sts.S.T")
    page.click("#playBtn"); page.wait_for_timeout(2000); page.click("#playBtn")
    b = page.evaluate("window.__sts.S.T")
    check(f"2x speed advanced {b - a:.2f}s in ~2s", 3.0 < b - a < 5.0, "")
    page.select_option("#speedSel", "1")

    print("filters")
    check("all chips on at start", page.locator(".chip.on").count() == len(labels))
    page.click("#fNone")
    check("none → empty list", page.evaluate("window.__sts.filtered.length") == 0)
    page.click('.chip[data-label="Goal"]')
    want = len(goals(1))
    check(f"goals only → {want} events in half 1", page.evaluate("window.__sts.filtered.length") == want)
    check("counts badge on chip", page.locator('.chip[data-label="Goal"] small').inner_text() == str(want))
    page.click("#fAll")
    page.click('#teamSeg [data-team=left]')
    nl = sum(1 for e in MATCH["events"] if e["half"] == 1 and e["team"] == "left")
    check("team=left filter", page.evaluate("window.__sts.filtered.length") == nl, f"{page.evaluate('window.__sts.filtered.length')} vs {nl}")
    page.click('#teamSeg [data-team=both]')
    page.check("#visChk")
    nv = sum(1 for e in MATCH["events"] if e["half"] == 1 and e["vis"] == "visible")
    check("visible-only filter", page.evaluate("window.__sts.filtered.length") == nv)
    page.uncheck("#visChk")
    page.click('.chip[data-label="Shot"]', modifiers=["Alt"])
    check("alt-click solos a label", page.evaluate("[...window.__sts.S.labels]") == ["Shot"])
    page.click("#fAll")

    print("event navigation")
    page.click("#fNone"); page.click('.chip[data-label="Goal"]')
    g1 = goals(1)
    page.locator(".ev").first.click()
    wait_seeked(page)
    T = page.evaluate("window.__sts.S.T")
    check(f"clicking event seeks to goal−3 s (T={T:.2f}, goal={g1[0]:.2f})", abs(T - (g1[0] - 3)) < 0.8, "")
    check("clicking event starts playback", page.evaluate("window.__sts.S.playing"))
    page.click("#playBtn")
    seek(page, 0)
    page.click("#nextEv")
    wait_seeked(page)
    check("next event jumps to first goal", abs(page.evaluate("window.__sts.S.T") - (g1[0] - 3)) < 0.1)
    page.click("#prevEv")
    wait_seeked(page)
    check("previous event from first goal goes nowhere earlier", abs(page.evaluate("window.__sts.S.T") - (g1[0] - 3)) < 0.1)

    print("clip mode")
    seek(page, 0)
    page.check("#clipChk")
    page.click("#playBtn")
    page.wait_for_timeout(1200)
    T = page.evaluate("window.__sts.S.T")
    check(f"clip mode skipped to first goal window (T={T:.1f})", g1[0] - 3.2 < T < g1[0] + 3, "")
    page.select_option("#speedSel", "4")
    page.evaluate("document.getElementById('video').playbackRate = 4")
    page.wait_for_function("() => !window.__sts.S.playing", timeout=60000)
    T = page.evaluate("window.__sts.S.T")
    check(f"clip mode stops after last goal window (T={T:.1f})", g1[-1] < T < g1[-1] + 4.5, "")
    page.uncheck("#clipChk")
    page.select_option("#speedSel", "1")

    print("half 2 + calibration")
    page.click("#fAll")
    page.click('#halfSeg [data-half="2"]')
    page.wait_for_function("() => window.__sts.S.half === 2 && window.__sts.S.usingVideo", timeout=10000)
    off2 = MATCH["halves"][1]["videoOffset"]
    check("half-2 list rebuilt", page.locator(".ev").count() == sum(1 for e in MATCH["events"] if e["half"] == 2))
    for T in (5.0, 61.2):
        seek(page, T)
        got, want = vframe(page), round((T + off2) * 25)
        check(f"half 2 seek T={T}: video frame {got} ≈ {want}", abs(got - want) <= 1, "")
    g2 = goals(2)
    page.click("#fNone"); page.click('.chip[data-label="Goal"]')
    check("half-2 goal at the same in-half time (absolute→half conversion)", abs(g2[0] - 72.0) < 0.1 and abs(page.evaluate("window.__sts.filtered[0].t") / 1000 - 72) < 0.1, str(g2))
    seek(page, 40)
    before = vframe(page)
    page.evaluate("window.__sts.setVidOff(window.__sts.S.vidOff[2] + 0.5)")
    wait_seeked(page)
    after = vframe(page)
    check(f"video offset +0.5 s moves video by +12 frames ({before}→{after}), T unchanged",
          abs((after - before) - 12.5) <= 1.5 and abs(page.evaluate("window.__sts.S.T") - 40) < 0.05)
    page.evaluate("window.__sts.setEvOff(1.0)")
    check("event offset shifts event list time", abs(page.evaluate("window.__sts.filtered[0].t") / 1000 + 1.0 - 73.0) < 0.01)
    page.reload()
    page.wait_for_function("() => window.__sts && window.__sts.S.match", timeout=15000)
    check("calibration persisted across reload",
          abs(page.evaluate("window.__sts.S.vidOff[2]") - (off2 + 0.5)) < 0.01 and abs(page.evaluate("window.__sts.S.evOff[2]") - 1.0) < 0.01)
    page.evaluate("localStorage.clear()")

    print("keyboard + deep link")
    page.goto(URL + "?h=2&t=33")
    page.wait_for_function("() => window.__sts && window.__sts.S.match && window.__sts.S.half === 2 && window.__sts.S.usingVideo", timeout=15000)
    wait_seeked(page)
    check("deep link ?h=2&t=33", abs(page.evaluate("window.__sts.S.T") - 33) < 0.1)
    page.keyboard.press("ArrowRight")
    check("→ seeks +5 s", abs(page.evaluate("window.__sts.S.T") - 38) < 0.1)
    page.keyboard.press("Shift+ArrowLeft")
    check("Shift+← seeks −1 frame", abs(page.evaluate("window.__sts.S.T") - 37.96) < 0.02)
    page.keyboard.press("v")
    check("V cycles view", page.evaluate("window.__sts.S.view") == "video")
    page.keyboard.press("Space")
    check("Space plays", page.evaluate("window.__sts.S.playing"))
    page.keyboard.press("Space")

    print("fallback without video")
    page.goto(URL + "?v1=videos/missing.webm")
    page.wait_for_function("() => window.__sts && window.__sts.S.match && window.__sts.S.vidOff", timeout=15000)
    page.wait_for_timeout(1200)
    check("missing video → message shown, viewer alive", page.locator("#vmsg").is_visible() and not page.evaluate("window.__sts.S.usingVideo"))
    a = page.evaluate("window.__sts.S.T")
    page.click("#playBtn"); page.wait_for_timeout(1500); page.click("#playBtn")
    b = page.evaluate("window.__sts.S.T")
    check(f"tracking-only clock still runs ({b - a:.2f}s)", 1.1 < b - a < 2.0)

    check("no console errors on the happy path", not [e for e in errors if "missing.webm" not in e and "404" not in e], str(errors))

    if SHOTS:
        SHOTS.mkdir(parents=True, exist_ok=True)
        page.goto(URL)
        page.wait_for_function("() => window.__sts && window.__sts.S.usingVideo", timeout=15000)
        page.click("#fNone"); page.click('.chip[data-label="Goal"]'); page.click('.chip[data-label="Pass"]'); page.click('.chip[data-label="Shot"]')
        seek(page, 70.0)
        page.evaluate("window.__sts.S.trails = true")
        page.wait_for_timeout(300)
        page.screenshot(path=str(SHOTS / "split.png"))
        page.click('#viewSeg [data-view=tactical]'); page.wait_for_timeout(250)
        page.screenshot(path=str(SHOTS / "tactical.png"))
        page.click('#viewSeg [data-view=video]'); page.wait_for_timeout(250)
        page.screenshot(path=str(SHOTS / "video.png"))
        m = browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2).new_page()
        m.goto(URL)
        m.wait_for_function("() => window.__sts && window.__sts.S.usingVideo", timeout=15000)
        m.evaluate("window.__sts.seek(70)")
        m.wait_for_timeout(600)
        m.screenshot(path=str(SHOTS / "mobile.png"), full_page=True)
        ow = m.evaluate("document.documentElement.scrollWidth")
        check("mobile: no horizontal overflow", ow <= 390, f"scrollWidth {ow}")
    browser.close()

print(f"\n{passes} passed, {len(fails)} failed")
if fails:
    print("FAILED:", *fails, sep="\n  ")
    sys.exit(1)
