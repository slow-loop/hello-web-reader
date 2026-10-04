#!/usr/bin/env node
// Usage: node record.mjs <url> [--out=out.mp4] [--width=1280] [--height=720]
//        [--step=700] [--hold=1200] [--max-slides=60] [--headless]
//        [--dpr=2] [--zoom=2] [--glide=600] [--scroll=200] [--fps=30] [--wait=3000] [--click="關閉,我知道了"]
// The way a person reads a page: glide `step` px over `glide` ms, hold `hold`
// ms, glide again (600 ms by default; --glide=0 jumps instead of gliding, a
// slideshow). With --scroll=<px/s> there are no steps at all: one
// continuous linear scroll from the top to the bottom (or `max-slides * step`
// px, whichever comes first), after an initial `hold` — the reading pace of a
// short, where the page moves the whole time the narration does. Runs headed
// by default so you can watch and dismiss cookie banners / login walls as they
// come up; pass --headless for unattended/batch runs.
//
// The viewport is the video's aspect ratio — keep it a shape a real browser
// window has (the 1280x720 default is 16:9). Do NOT set it to whatever box the
// consuming layout happens to reserve: a layout band is a bounding box that
// content gets fitted into, not a ratio to stretch the browser to.
//
// --dpr renders at N device pixels per CSS pixel; the video stays at viewport
// size, so those extra pixels become supersampling — crisper text at the same
// resolution. Do NOT enlarge recordVideo.size to match: Playwright only ever
// scales a frame DOWN to fit the requested size, so asking for more than the
// viewport pins the page in the top-left corner and pads the rest.
// --zoom=N is the way to get a BIG video: it sets CSS zoom on the document, so
// a 2880x1800 viewport with --zoom=2 lays out like a 1440x900 window but is
// recorded at the full 2880x1800 — real pixels to punch into later, not
// supersampling. `step` is in pre-zoom (layout) px either way.
// Playwright records at a fixed 25 fps. Dropped into a 30 fps timeline that
// duplicates every 6th frame — a visible tick on a continuous scroll. --fps=30
// motion-interpolates to the target rate (ffmpeg minterpolate; ~6x realtime,
// so a 12s clip takes about a minute). Skip it for step-and-hold captures.
// --phone emulates a phone for real (430 css px wide unless --width says otherwise, dpr 2, an iPhone's user
// agent, touch): a site that picks its layout by user agent or viewport gives its phone page, which CSS zoom
// on a wide window does not get. --shot=<png> takes one still instead of a recording: --height css px from
// the top (default 1800), and with --mark="<text>" (any number) <png>.marks.json, each text's line boxes
// [x, y, w, h] in the still's pixels, read from the page's text itself. --hide="<css>" hides more before it.
// --clean hides ads (iframes, ad/sponsor/taboola class names) and everything floating (chat avatars,
// back-to-top, bottom bars, summary bars), kept up while the page scrolls since ads load late.
// --wait adds settle time before capture (slow/animated pages); --click
// dismisses cookie/subscribe overlays by visible text (comma-separated
// candidates, misses ignored). Both happen before the trim point, so neither
// shows up in the output.
import { chromium } from "playwright";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { spawn } from "node:child_process";

function parseArgs(argv) {
  const args = {
    url: null,
    out: null,
    width: 1280,
    height: 720,
    step: 700,
    hold: 1200,
    maxSlides: 60,
    headless: false,
    dpr: 1,
    zoom: 1,
    glide: 600,
    scroll: 0,
    fps: 0,
    wait: 0,
    click: null,
    clean: false,
    phone: false,
    shot: null,
    marks: [],
    hide: null,
    opencli: null,
    cookies: null,
    waitSelector: null,
  };
  for (const arg of argv) {
    if (!arg.startsWith("--")) {
      args.url = arg;
      continue;
    }
    const eqIdx = arg.indexOf("=");
    const key = eqIdx === -1 ? arg.slice(2) : arg.slice(2, eqIdx);
    const val = eqIdx === -1 ? null : arg.slice(eqIdx + 1);
    if (key === "out") args.out = val;
    else if (key === "width") args.width = Number(val);
    else if (key === "height") args.height = Number(val);
    else if (key === "step") args.step = Number(val);
    else if (key === "hold") args.hold = Number(val);
    else if (key === "max-slides") args.maxSlides = Number(val);
    else if (key === "headless") args.headless = true;
    else if (key === "dpr") args.dpr = Number(val);
    else if (key === "zoom") args.zoom = Number(val);
    else if (key === "glide") args.glide = Number(val);
    else if (key === "scroll") args.scroll = Number(val);
    else if (key === "fps") args.fps = Number(val);
    else if (key === "wait") args.wait = Number(val);
    else if (key === "click") args.click = val;
    else if (key === "clean") args.clean = true;
    else if (key === "phone") args.phone = true;
    else if (key === "shot") args.shot = val;
    else if (key === "mark") args.marks.push(val);
    else if (key === "hide") args.hide = val;
    else if (key === "opencli") args.opencli = val || "default";
    else if (key === "cookies") args.cookies = val;
    else if (key === "wait-selector") args.waitSelector = val;
  }
  return args;
}

function run(cmd, cmdArgs) {
  return new Promise((resolve, reject) => {
    const p = spawn(cmd, cmdArgs, { stdio: "inherit" });
    p.on("exit", (code) =>
      code === 0 ? resolve() : reject(new Error(`${cmd} exited ${code}`)),
    );
  });
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  if (!args.url) {
    console.error(
      "Usage: node record.mjs <url> [--out=out.mp4] [--width=1280] [--height=720] [--step=700] [--hold=1200] [--max-slides=60] [--headless] [--dpr=2] [--zoom=2] [--glide=600] [--scroll=200] [--fps=30] [--wait=3000] [--click=\"關閉,我知道了\"] [--clean] [--phone] [--shot=out.png --mark=\"…\" --hide=\"css\"]",
    );
    process.exit(1);
  }

  if (args.phone) {
    if (!process.argv.some((a) => a.startsWith("--width="))) args.width = 430;
    if (!process.argv.some((a) => a.startsWith("--height="))) args.height = args.shot ? 1800 : 932;
    if (!process.argv.some((a) => a.startsWith("--dpr="))) args.dpr = 2;
  } else if (args.shot && !process.argv.some((a) => a.startsWith("--height="))) args.height = 1800;
  const videoDir = await mkdtemp(path.join(tmpdir(), "scroll-capture-"));
  const browser = await chromium.launch({ headless: args.headless });
  const context = await browser.newContext({
    // a still is shot from a phone-high window and clipped to --height; a recording is the window itself
    viewport: { width: args.width, height: args.shot ? (args.phone ? 932 : 900) : args.height },
    deviceScaleFactor: args.dpr,
    // light theme: a dark page reads as another site next to the rest of a short (hello-video, 2026-09-14)
    colorScheme: "light",
    ...(args.phone
      ? { isMobile: true, hasTouch: true,
          userAgent: "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1" }
      : { userAgent: "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36" }),
    ...(args.shot ? {} : { recordVideo: { dir: videoDir, size: { width: args.width, height: args.height } } }),
  });

  if (args.opencli) {
    try {
      const { Page } = await import("/Users/cc/Developer/GitHub/OpenCLI/dist/src/browser/page.js");
      const opencliPage = new Page(args.opencli);
      const rawCookies = await opencliPage.getCookies({ url: args.url });
      if (rawCookies && rawCookies.length > 0) {
        const pwCookies = rawCookies.map((c) => ({
          name: c.name,
          value: c.value,
          domain: c.domain || new URL(args.url).hostname,
          path: c.path || "/",
          httpOnly: Boolean(c.httpOnly),
          secure: Boolean(c.secure),
          sameSite: c.sameSite === "none" ? "None" : (c.sameSite === "strict" ? "Strict" : "Lax"),
        }));
        await context.addCookies(pwCookies);
        console.error(`[record.mjs] Injected ${pwCookies.length} cookies from OpenCLI session '${args.opencli}'`);
      }
    } catch (e) {
      console.error(`[record.mjs] OpenCLI cookie injection warning: ${e.message}`);
    }
  } else if (args.cookies) {
    try {
      const fs = await import("node:fs/promises");
      const data = JSON.parse(await fs.readFile(args.cookies, "utf-8"));
      await context.addCookies(Array.isArray(data) ? data : data.cookies || []);
      console.error(`[record.mjs] Injected cookies from ${args.cookies}`);
    } catch (e) {
      console.error(`[record.mjs] Cookie file warning: ${e.message}`);
    }
  }

  const page = await context.newPage();
  const recordingStart = Date.now(); // recordVideo starts capturing from page creation

  console.error(`Loading ${args.url} ...`);
  try {
    await page.goto(args.url, { waitUntil: "domcontentloaded", timeout: 60000 });
    await page.waitForTimeout(2500);
  } catch (err) {
    console.error(`[record.mjs] Navigation notice: ${err.message}`);
  }
  if (args.wait) await page.waitForTimeout(args.wait);
  if (args.zoom !== 1) {
    await page.evaluate((z) => {
      document.documentElement.style.zoom = String(z);
    }, args.zoom);
  }
  for (const text of args.click ? args.click.split(",") : []) {
    try {
      await page.locator(`text=${text.trim()}`).first().click({ timeout: 3000 });
      console.error(`clicked: ${text}`);
      await page.waitForTimeout(600);
    } catch {
      console.error(`click skipped (not found): ${text}`);
    }
  }

  if (args.waitSelector) {
    try {
      await page.waitForSelector(args.waitSelector, { timeout: 30000 });
      console.error(`[record.mjs] Selector ready: ${args.waitSelector}`);
    } catch (e) {
      console.error(`[record.mjs] Wait-selector warning: ${e.message}`);
    }
  }

  if (args.clean) {
    await page.evaluate(() => {
      const ad = /(^|[-_ ])(ad|ads|adv|advert|advertisement|sponsor|sponsored|taboola|outbrain|dfp|gpt)([-_ 0-9]|$)/i;
      // into shadow roots too: floating widgets (summary bars, chat buttons) often live in one
      const sweep = (root = document.body) => root.querySelectorAll("*").forEach((el) => {
        if (el.shadowRoot) sweep(el.shadowRoot);
        if (el.style.getPropertyPriority("display") === "important") return;
        const cls = typeof el.className === "string" ? el.className : "";
        const pos = getComputedStyle(el).position;
        // !important: a site's own !important rule (a bar shown on scroll) would win over a plain inline style
        if (el.matches("iframe, ins") || ad.test(el.id) || ad.test(cls) || pos === "fixed" || pos === "sticky") el.style.setProperty("display", "none", "important");
      });
      // every frame and on every change: the site brings its widgets back on each scroll
      const loop = () => { sweep(); requestAnimationFrame(loop); };
      loop();
      new MutationObserver(() => sweep()).observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ["class", "style"] });
    });
    // widgets that appear only once the page scrolls: scroll down and back first, so they come up and are
    // hidden before the trim point instead of flashing in the recording
    await page.evaluate(() => window.scrollTo({ top: 1500, behavior: "instant" }));
    await page.waitForTimeout(1500);
    await page.evaluate(() => window.scrollTo({ top: 0, behavior: "instant" }));
    await page.waitForTimeout(1000);
  }

  if (args.hide) {
    await page.evaluate((sel) => document.querySelectorAll(sel).forEach((e) => e.style.setProperty("display", "none", "important")), args.hide);
    await page.waitForTimeout(500);
  }

  if (args.shot) {
    await page.evaluate(() => window.scrollTo({ top: 0, behavior: "instant" }));
    await page.waitForTimeout(500);
    // each text's line boxes, from the text itself (first occurrence that is on the page), in css px
    const boxes = await page.evaluate((qs) => {
      const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
      const nodes = [], starts = [];
      let text = "";
      for (let n; (n = walker.nextNode());) { starts.push(text.length); nodes.push(n); text += n.data; }
      const at = (k) => { let i = starts.length - 1; while (starts[i] > k) i--; return [nodes[i], k - starts[i]]; };
      const out = {};
      for (const q of qs) {
        for (let k = text.indexOf(q); k >= 0; k = text.indexOf(q, k + 1)) {
          const r = document.createRange();
          r.setStart(...at(k));
          const [en, eo] = at(k + q.length - 1);
          r.setEnd(en, eo + 1);
          const rects = [...r.getClientRects()].filter((c) => c.width > 1 && c.height > 1);
          if (!rects.length) continue;
          const lines = [];
          for (const c of rects) {
            const l = lines.find((l) => Math.abs(l.y - c.top) < c.height / 2);
            if (l) { l.x = Math.min(l.x, c.left); l.x2 = Math.max(l.x2, c.right); l.b = Math.max(l.b, c.bottom); }
            else lines.push({ x: c.left, y: c.top, x2: c.right, b: c.bottom });
          }
          out[q] = lines.map((l) => [l.x, l.y + window.scrollY, l.x2 - l.x, l.b - l.y]);
          break;
        }
      }
      return out;
    }, args.marks);
    const w = await page.evaluate(() => document.documentElement.clientWidth);
    await page.screenshot({ path: args.shot, fullPage: true, clip: { x: 0, y: 0, width: w, height: args.height } });
    const pad = 4, d = args.dpr;
    const marks = Object.fromEntries(Object.entries(boxes).map(([q, ls]) => [q, ls.map(([x, y, bw, bh]) =>
      [Math.round(x * d - pad), Math.round(y * d - pad), Math.round(bw * d + 2 * pad), Math.round(bh * d + 2 * pad)])]));
    const { writeFile } = await import("node:fs/promises");
    // written even when empty: an older one's boxes belong to an older shot, and older cuts import the file
    if (args.marks.length) await writeFile(args.shot + ".marks.json", JSON.stringify(marks, null, 0));
    const missing = args.marks.filter((q) => !marks[q]);
    console.log(`${args.shot} (${w * d} px wide)${args.marks.length ? `, marks ${Object.keys(marks).length}/${args.marks.length}` : ""}${missing.length ? `; not on the page: ${missing.join(" | ")}` : ""}`);
    await browser.close();
    return;
  }

  // seconds of blank/loading footage at the front of the recording to trim —
  // measured right as the page becomes ready, before the first slide's hold
  const trimStart = (Date.now() - recordingStart) / 1000;

  await page.waitForTimeout(args.hold);

  if (args.scroll > 0) {
    // continuous mode: linear px/s, frame by frame, until the bottom or the px budget
    await page.evaluate(
      ({ pxPerSec, budget }) =>
        new Promise((done) => {
          const start = window.scrollY, t0 = performance.now();
          const tick = (now) => {
            const y = start + ((now - t0) / 1000) * pxPerSec;
            window.scrollTo({ top: y, left: 0, behavior: "instant" });
            const atBottom = window.scrollY + window.innerHeight >= document.documentElement.scrollHeight - 2;
            atBottom || y - start >= budget ? done() : requestAnimationFrame(tick);
          };
          requestAnimationFrame(tick);
        }),
      { pxPerSec: args.scroll, budget: args.maxSlides * args.step },
    );
    await page.waitForTimeout(args.hold);
  }

  for (let i = 0; args.scroll === 0 && i < args.maxSlides; i++) {
    const { scrolled, atBottom } = await page.evaluate(
      async ({ step, glide }) => {
        const before = window.scrollY;
        if (glide > 0) {
          // ease-in-out over `glide` ms, one frame at a time — deterministic,
          // unlike behavior:"smooth" whose duration the browser decides
          await new Promise((done) => {
            const t0 = performance.now();
            const tick = (now) => {
              const p = Math.min(1, (now - t0) / glide);
              const e = p < 0.5 ? 2 * p * p : 1 - Math.pow(-2 * p + 2, 2) / 2;
              window.scrollTo({ top: before + step * e, left: 0, behavior: "instant" });
              p < 1 ? requestAnimationFrame(tick) : done();
            };
            requestAnimationFrame(tick);
          });
        } else {
          window.scrollTo({ top: before + step, left: 0, behavior: "instant" });
        }
        const atBottom =
          window.scrollY + window.innerHeight >=
          document.documentElement.scrollHeight - 2;
        return { scrolled: window.scrollY !== before, atBottom };
      },
      { step: args.step, glide: args.glide },
    );
    await page.waitForTimeout(args.hold);
    if (atBottom || !scrolled) break;
  }

  await context.close();
  const webmPath = await page.video().path();
  await browser.close();

  const out = path.resolve(args.out || `scroll-capture-${Date.now()}.mp4`);
  console.error(
    `Encoding -> ${out} (trimming ${trimStart.toFixed(2)}s of loading)`,
  );
  await run("ffmpeg", [
    "-y",
    "-i",
    webmPath,
    "-ss",
    trimStart.toFixed(3),
    ...(args.fps
      ? ["-vf", `minterpolate=fps=${args.fps}:mi_mode=mci:mc_mode=aobmc:me_mode=bidir:vsbmc=1`]
      : []),
    "-c:v",
    "libx264",
    "-pix_fmt",
    "yuv420p",
    "-g", "30", "-keyint_min", "30",   // keyframe every second: HyperFrames seeks per frame
    "-movflags",
    "+faststart",
    out,
  ]);
  await rm(videoDir, { recursive: true, force: true });

  console.log(out);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
