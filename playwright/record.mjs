#!/usr/bin/env node
// Usage: node record.mjs <url> [--out=out.mp4] [--width=1280] [--height=800]
//        [--step=700] [--hold=1200] [--max-slides=60] [--headless]
// Slideshow-style capture: jump `step` px, hold `hold` ms, jump again — not
// a continuous scroll animation. Runs headed by default so you can watch and
// dismiss cookie banners / login walls as they come up; pass --headless for
// unattended/batch runs.
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
    height: 800,
    step: 700,
    hold: 1200,
    maxSlides: 60,
    headless: false,
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
      "Usage: node record.mjs <url> [--out=out.mp4] [--width=1280] [--height=800] [--step=700] [--hold=1200] [--max-slides=60] [--headless]",
    );
    process.exit(1);
  }

  const videoDir = await mkdtemp(path.join(tmpdir(), "scroll-capture-"));
  const browser = await chromium.launch({ headless: args.headless });
  const context = await browser.newContext({
    viewport: { width: args.width, height: args.height },
    recordVideo: {
      dir: videoDir,
      size: { width: args.width, height: args.height },
    },
  });
  const page = await context.newPage();
  const recordingStart = Date.now(); // recordVideo starts capturing from page creation

  console.error(`Loading ${args.url} ...`);
  await page.goto(args.url, { waitUntil: "load", timeout: 60000 });

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
