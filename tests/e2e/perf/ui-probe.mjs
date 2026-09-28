// WebUI responsiveness probe for scripts/perf_load.py (--ui).
//
// Opens the harness server's WebUI on one view, prints a `{"event":"ready"}`
// line once it is visible, and measures until it reads `stop` on stdin (or
// --max-seconds elapse). It then prints one `{"event":"result",...}` line with
// Long Tasks, frame gaps (rAF intervals above 50 ms), the DOM latency of the
// fake Provider's timing markers, and every `/api/rpc` call the browser made
// while measuring, counted per method.
//
// `--profile <path>` also records a V8 CPU profile of the page's main frame
// while measuring and writes it to <path> (`.cpuprofile`, opens in the
// DevTools Performance panel or speedscope).
//
// Views:
// - `--view chat --agent <id>` opens that Agent's current Session.
// - `--view extension:<name>:<page>` opens an Extension page. Its sandboxed
//   frame is measured too: Long Tasks of all frames are merged, and the frame
//   gaps of Extension frames are reported separately as `page_frame_gaps_ms`.
//   The stdin command `select-run` clicks the first entry in the Swarm page's
//   run list and answers `{"event":"selected"}` (or `select-failed`).
//
// Lives outside the Playwright testDir (./tests) so E2E runs never collect it;
// it reuses the E2E Playwright installation (`npm ci` in tests/e2e).

import { writeFile } from "node:fs/promises";
import { createInterface } from "node:readline";
import { parseArgs } from "node:util";

import { chromium } from "@playwright/test";

const FRAME_GAP_MS = 50;
const RPC_PATH = "/api/rpc";
const SELECT_TIMEOUT_MS = 30_000;
const RUN_ENTRY = ".run-group button.secondary-list__item";
// Microseconds between CPU profile samples; V8's default is 1000.
const PROFILE_SAMPLING_US = 250;

const { values } = parseArgs({
  options: {
    url: { type: "string" },
    view: { type: "string", default: "chat" },
    agent: { type: "string" },
    "max-seconds": { type: "string", default: "900" },
    profile: { type: "string" },
  },
});

function emit(payload) {
  process.stdout.write(`${JSON.stringify(payload)}\n`);
}

// Reads stdin commands until `stop`, stdin closes, or the time limit.
function serveCommands(maxSeconds, handlers) {
  return new Promise((resolve) => {
    const timer = setTimeout(() => resolve("timeout"), maxSeconds * 1000);
    const lines = createInterface({ input: process.stdin });
    lines.on("line", (line) => {
      const command = line.trim();
      if (command === "stop") {
        clearTimeout(timer);
        // Resolve first: closing emits "close" synchronously.
        resolve("stop");
        lines.close();
      } else if (handlers[command]) {
        handlers[command]();
      }
    });
    lines.on("close", () => {
      clearTimeout(timer);
      resolve("stdin-closed");
    });
  });
}

// Runs inside every frame before any application script.
function installObservers(frameGapMs) {
  const markerPattern = /⟦t=(\d+(?:\.\d+)?)⟧/g;
  const state = {
    measuring: false,
    startedAt: 0,
    frames: 0,
    frameGaps: [],
    longTasks: [],
    mutations: 0,
    markerLatencies: [],
    seenMarkers: new Set(),
    pendingTargets: new Set(),
  };
  window.__vbotPerfProbe = state;

  new PerformanceObserver((list) => {
    if (!state.measuring) return;
    for (const entry of list.getEntries()) {
      state.longTasks.push([
        performance.timeOrigin + entry.startTime,
        entry.duration,
      ]);
    }
  }).observe({ type: "longtask" });

  const scanMarkers = () => {
    const now = Date.now();
    for (const target of state.pendingTargets) {
      const text = target.textContent ?? "";
      for (const match of text.matchAll(markerPattern)) {
        if (state.seenMarkers.has(match[1])) continue;
        state.seenMarkers.add(match[1]);
        state.markerLatencies.push(now - Number.parseFloat(match[1]) * 1000);
      }
    }
    state.pendingTargets.clear();
  };

  let previous = null;
  const onFrame = (timestamp) => {
    if (state.measuring) {
      state.frames += 1;
      if (previous !== null && timestamp - previous > frameGapMs) {
        state.frameGaps.push(timestamp - previous);
      }
      scanMarkers();
    }
    previous = timestamp;
    requestAnimationFrame(onFrame);
  };
  requestAnimationFrame(onFrame);

  const startObserving = () => {
    new MutationObserver((records) => {
      if (!state.measuring) return;
      state.mutations += records.length;
      for (const record of records) {
        const node =
          record.target.nodeType === Node.TEXT_NODE
            ? record.target.parentElement
            : record.target;
        if (node) state.pendingTargets.add(node);
      }
    }).observe(document.body, {
      characterData: true,
      childList: true,
      subtree: true,
    });
  };
  if (document.body) {
    startObserving();
  } else {
    document.addEventListener("DOMContentLoaded", startObserving, {
      once: true,
    });
  }
}

function startMeasuring() {
  const state = window.__vbotPerfProbe;
  if (state && !state.measuring) {
    state.measuring = true;
    state.startedAt = performance.now();
  }
}

function collectMeasurements() {
  const state = window.__vbotPerfProbe;
  if (!state) return null;
  state.measuring = false;
  return {
    duration_ms: performance.now() - state.startedAt,
    frames: state.frames,
    frame_gaps_ms: state.frameGaps,
    long_tasks: state.longTasks,
    mutations: state.mutations,
    marker_latencies_ms: state.markerLatencies,
    heap_used_mb: performance.memory
      ? performance.memory.usedJSHeapSize / 1_048_576
      : null,
    dom_nodes: document.getElementsByTagName("*").length,
  };
}

// Counts `/api/rpc` calls per method; Extension operations per name/operation.
function createRpcCounter() {
  const methods = new Map();
  let counting = false;
  const methodOf = (request) => {
    let body = null;
    try {
      body = request.postDataJSON();
    } catch {
      body = null;
    }
    const method = typeof body?.method === "string" ? body.method : "?";
    const params = body?.params;
    if (method === "extensions.operation" && params) {
      return `${method}:${params.name}/${params.operation}`;
    }
    return method;
  };
  const record = (request, failed) => {
    if (!counting || request.method() !== "POST") return;
    if (new URL(request.url()).pathname !== RPC_PATH) return;
    const method = methodOf(request);
    const entry = methods.get(method) ?? {
      count: 0,
      total_ms: 0,
      max_ms: 0,
      failed: 0,
    };
    entry.count += 1;
    if (failed) {
      entry.failed += 1;
    } else {
      const elapsed = request.timing().responseEnd;
      if (elapsed >= 0) {
        entry.total_ms += elapsed;
        entry.max_ms = Math.max(entry.max_ms, elapsed);
      }
    }
    methods.set(method, entry);
  };
  return {
    attach(context) {
      context.on("requestfinished", (request) => record(request, false));
      context.on("requestfailed", (request) => record(request, true));
    },
    start() {
      counting = true;
    },
    stop() {
      counting = false;
      return Object.fromEntries(methods);
    },
  };
}

async function openView(page, view) {
  if (view === "chat") {
    await page.goto(`${values.url}/#chat`);
    await page
      .getByRole("region", { name: "Chat" })
      .first()
      .waitFor({ timeout: 30_000 });
    return null;
  }
  if (!view.startsWith("extension:")) {
    throw new Error(`unknown --view ${view}`);
  }
  await page.goto(`${values.url}/#${view}`);
  const frame = page.frameLocator("iframe[sandbox]").first();
  await frame.locator("body *").first().waitFor({ timeout: 30_000 });
  return frame;
}

async function selectRun(frame) {
  if (frame === null) {
    emit({ event: "select-failed", message: "the view has no Extension page" });
    return;
  }
  try {
    await frame
      .locator(RUN_ENTRY)
      .first()
      .click({ timeout: SELECT_TIMEOUT_MS });
    emit({ event: "selected" });
  } catch (error) {
    emit({ event: "select-failed", message: String(error?.message ?? error) });
  }
}

function mergeFrames(page, measurements) {
  const main = page.mainFrame();
  const top = measurements.get(main) ?? {};
  const seen = new Set();
  const longTasks = [];
  const pageGaps = [];
  const markers = [];
  let pageDomNodes = 0;
  let pageMutations = 0;
  for (const [frame, measured] of measurements) {
    for (const [start, duration] of measured.long_tasks) {
      const key = `${Math.round(start)}:${Math.round(duration)}`;
      if (seen.has(key)) continue;
      seen.add(key);
      longTasks.push(duration);
    }
    markers.push(...measured.marker_latencies_ms);
    if (frame !== main) {
      pageGaps.push(...measured.frame_gaps_ms);
      pageDomNodes += measured.dom_nodes;
      pageMutations += measured.mutations;
    }
  }
  return {
    duration_ms: top.duration_ms ?? null,
    frames: top.frames ?? null,
    frame_gaps_ms: top.frame_gaps_ms ?? [],
    page_frame_gaps_ms: pageGaps,
    long_tasks_ms: longTasks,
    mutations: top.mutations ?? null,
    page_mutations: pageMutations,
    marker_latencies_ms: markers,
    heap_used_mb: top.heap_used_mb ?? null,
    dom_nodes: top.dom_nodes ?? null,
    page_dom_nodes: pageDomNodes,
    measured_frames: measurements.size,
  };
}

// Samples the page's main-frame JavaScript through the DevTools protocol.
async function startProfiler(context, page) {
  const session = await context.newCDPSession(page);
  await session.send("Profiler.enable");
  await session.send("Profiler.setSamplingInterval", {
    interval: PROFILE_SAMPLING_US,
  });
  await session.send("Profiler.start");
  return {
    async stop(path) {
      const { profile } = await session.send("Profiler.stop");
      await writeFile(path, JSON.stringify(profile));
    },
  };
}

async function main() {
  const maxSeconds = Number.parseFloat(values["max-seconds"]);
  if (!values.url || !Number.isFinite(maxSeconds)) {
    throw new Error("--url and a numeric --max-seconds are required");
  }
  if (values.view === "chat" && !values.agent) {
    throw new Error("--view chat requires --agent");
  }
  const browser = await chromium.launch({ headless: true });
  try {
    const context = await browser.newContext({
      viewport: { width: 1440, height: 900 },
    });
    await context.addInitScript((agentId) => {
      // Only the WebUI itself; a sandboxed Extension frame has no storage.
      if (window.top !== window) return;
      try {
        if (agentId) localStorage.setItem("vbot.selectedAgentId", agentId);
        localStorage.setItem("vbot.onboardingDismissed", "1");
      } catch {
        // Storage unavailable: the WebUI falls back to its defaults.
      }
    }, values.agent ?? "");
    await context.addInitScript(installObservers, FRAME_GAP_MS);
    const rpcCounter = createRpcCounter();
    rpcCounter.attach(context);
    const page = await context.newPage();
    const frame = await openView(page, values.view);

    let measuring = false;
    const measure = (target) => target.evaluate(startMeasuring).catch(() => {});
    page.on("framenavigated", (navigated) => {
      if (measuring) void measure(navigated);
    });
    measuring = true;
    rpcCounter.start();
    await Promise.all(page.frames().map(measure));
    const profiler = values.profile ? await startProfiler(context, page) : null;
    emit({ event: "ready" });

    const reason = await serveCommands(maxSeconds, {
      "select-run": () => void selectRun(frame),
    });
    measuring = false;
    const rpcCalls = rpcCounter.stop();
    await profiler?.stop(values.profile).catch((error) => {
      console.error(`CPU profile not written: ${error?.message ?? error}`);
    });
    const measurements = new Map();
    for (const current of page.frames()) {
      const measured = await current
        .evaluate(collectMeasurements)
        .catch(() => null);
      if (measured) measurements.set(current, measured);
    }
    emit({
      event: "result",
      stop_reason: reason,
      ...mergeFrames(page, measurements),
      rpc_calls: rpcCalls,
    });
  } finally {
    await browser.close();
  }
}

main().catch((error) => {
  emit({ event: "error", message: String(error?.stack ?? error) });
  process.exitCode = 1;
});
