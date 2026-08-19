#!/usr/bin/env node
// Usage: node record.mjs <url> [--out=out.mp4] [--width=1280] [--height=720]
//        [--step=700] [--hold=1200] [--max-slides=60] [--headless]
//        [--dpr=2] [--wait=3000] [--click="關閉,我知道了"]
// Slideshow-style capture: jump `step` px, hold `hold` ms, jump again — not
// a continuous scroll animation. Runs headed by default so you can watch and
// dismiss cookie banners / login walls as they come up; pass --headless for
// unattended/batch runs.
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
    wait: 0,
    click: null,
  };
  for (const arg of argv) {
    if (!arg.startsWith("--")) {
      args.url = arg;
      continue;
    }
    const [key, val] = arg.slice(2).split("=");
    if (key === "out") args.out = val;
    else if (key === "width") args.width = Number(val);
    else if (key === "height") args.height = Number(val);
    else if (key === "step") args.step = Number(val);
    else if (key === "hold") args.hold = Number(val);
    else if (key === "max-slides") args.maxSlides = Number(val);
    else if (key === "headless") args.headless = true;
    else if (key === "dpr") args.dpr = Number(val);
    else if (key === "wait") args.wait = Number(val);
    else if (key === "click") args.click = val;
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
      "Usage: node record.mjs <url> [--out=out.mp4] [--width=1280] [--height=720] [--step=700] [--hold=1200] [--max-slides=60] [--headless] [--dpr=2] [--wait=3000] [--click=\"關閉,我知道了\"]",
    );
    process.exit(1);
  }

  const videoDir = await mkdtemp(path.join(tmpdir(), "scroll-capture-"));
  const browser = await chromium.launch({ headless: args.headless });
  const context = await browser.newContext({
    viewport: { width: args.width, height: args.height },
    deviceScaleFactor: args.dpr,
    recordVideo: {
      dir: videoDir,
      size: { width: args.width, height: args.height },
    },
  });
  const page = await context.newPage();
  const recordingStart = Date.now(); // recordVideo starts capturing from page creation

  console.error(`Loading ${args.url} ...`);
  await page.goto(args.url, { waitUntil: "load", timeout: 60000 });
  if (args.wait) await page.waitForTimeout(args.wait);
  for (const text of args.click ? args.click.split(",") : []) {
    try {
      await page.locator(`text=${text.trim()}`).first().click({ timeout: 3000 });
      console.error(`clicked: ${text}`);
      await page.waitForTimeout(600);
    } catch {
      console.error(`click skipped (not found): ${text}`);
    }
  }

  // seconds of blank/loading footage at the front of the recording to trim —
  // measured right as the page becomes ready, before the first slide's hold
  const trimStart = (Date.now() - recordingStart) / 1000;

  await page.waitForTimeout(args.hold);

  for (let i = 0; i < args.maxSlides; i++) {
    const { scrolled, atBottom } = await page.evaluate((step) => {
      const before = window.scrollY;
      window.scrollTo({ top: before + step, left: 0, behavior: "instant" });
      const atBottom =
        window.scrollY + window.innerHeight >=
        document.documentElement.scrollHeight - 2;
      return { scrolled: window.scrollY !== before, atBottom };
    }, args.step);
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
    "-c:v",
    "libx264",
    "-pix_fmt",
    "yuv420p",
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
