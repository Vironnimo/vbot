// WebUI responsiveness probe for scripts/perf_load.py (--ui).
//
// Opens the harness server's WebUI on one view, prints a `{"event":"ready"}`
// line once it is visible, and measures until it reads `stop` on stdin (or
// --max-seconds elapse). It then prints one `{"event":"result",...}` line with
// Long Tasks, frame gaps (rAF intervals above 50 ms), the DOM latency of the
// fake Provider's timing markers, every `/api/rpc` call the browser made
// while measuring, counted per method, and the browser's own counters
// (`browser`: CDP Performance.getMetrics deltas for task, script, layout and
// style time, then DOM nodes, layout objects and JS heap after a forced
// garbage collection).
//
// `--profile <path>` also records a V8 CPU profile of the page's main frame
// while measuring and writes it to <path> (`.cpuprofile`, opens in the
// DevTools Performance panel or speedscope).
//
// `--scroll-history` (chat view) measures a scroll-through phase before
// `ready`: it scrolls the timeline to the top the way a mouse wheel does,
// waits until the older History page is rendered, and repeats until no older
// History remains, printing `{"event":"progress"}` after each page. Then it
// returns to the bottom through Jump to latest. The result carries this phase
// as `scroll_through` (per-page times, Long Tasks, frame gaps, `browser`).
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
const TIMELINE = ".chat-timeline section.messages";
const JUMP_TO_LATEST = ".chat-timeline__jump-latest";
const HISTORY_TIMEOUT_MS = 60_000;
// Two attempts stay below the harness's 90 s wait for the next progress line.
const PAGE_TIMEOUT_MS = 30_000;
const PAGE_ATTEMPTS = 2;
const MAX_PAGES = 10_000;

const { values } = parseArgs({
  options: {
    url: { type: "string" },
    view: { type: "string", default: "chat" },
    agent: { type: "string" },
    "max-seconds": { type: "string", default: "900" },
    profile: { type: "string" },
    "scroll-history": { type: "boolean", default: false },
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
    flushLongTasks: () => {},
  };
  window.__vbotPerfProbe = state;

  const recordLongTasks = (entries) => {
    if (!state.measuring) return;
    for (const entry of entries) {
      // A task reported late may belong to the previous phase.
      if (entry.startTime < state.startedAt) continue;
      state.longTasks.push([
        performance.timeOrigin + entry.startTime,
        entry.duration,
      ]);
    }
  };
  const longTaskObserver = new PerformanceObserver((list) =>
    recordLongTasks(list.getEntries()),
  );
  longTaskObserver.observe({ type: "longtask" });
  state.flushLongTasks = () => recordLongTasks(longTaskObserver.takeRecords());

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
        // Scan only what changed: the text of an edited node, or the added
        // nodes. Scanning a list's parent would read the whole timeline.
        if (record.type === "characterData") {
          const parent = record.target.parentElement;
          if (parent) state.pendingTargets.add(parent);
        } else {
          for (const node of record.addedNodes) state.pendingTargets.add(node);
        }
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

// Starts a measuring phase with empty measurements.
function startMeasuring() {
  const state = window.__vbotPerfProbe;
  if (!state || state.measuring) return;
  state.measuring = true;
  state.startedAt = performance.now();
  state.frames = 0;
  state.frameGaps = [];
  state.longTasks = [];
  state.mutations = 0;
  state.markerLatencies = [];
  state.pendingTargets.clear();
}

function collectMeasurements() {
  const state = window.__vbotPerfProbe;
  if (!state) return null;
  state.flushLongTasks();
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

// Records every finished `chat.history` read: its paging parameters, whether
// older History remains, and its time as the browser saw it.
function createHistoryWatcher() {
  const reads = [];
  const waiters = new Set();
  const record = async (request) => {
    if (request.method() !== "POST") return;
    if (new URL(request.url()).pathname !== RPC_PATH) return;
    let body = null;
    try {
      body = request.postDataJSON();
    } catch {
      return;
    }
    if (body?.method !== "chat.history") return;
    let result = null;
    try {
      result = (await (await request.response())?.json())?.result ?? null;
    } catch {
      result = null;
    }
    reads.push({
      before: body.params?.before ?? null,
      after: body.params?.after ?? null,
      has_more: result?.has_more === true,
      messages: Array.isArray(result?.messages) ? result.messages.length : 0,
      rpc_ms: request.timing().responseEnd,
    });
    for (const wake of waiters) wake();
  };
  return {
    attach(context) {
      context.on("requestfinished", (request) => void record(request));
    },
    count() {
      return reads.length;
    },
    // The first read from index `from` on that matches, or null on timeout.
    wait(from, matches, timeoutMs) {
      return new Promise((resolve) => {
        const check = () => {
          const found = reads.slice(from).find(matches);
          if (!found) return false;
          finish(found);
          return true;
        };
        const timer = setTimeout(() => finish(null), timeoutMs);
        const finish = (value) => {
          clearTimeout(timer);
          waiters.delete(check);
          resolve(value);
        };
        if (!check()) waiters.add(check);
      });
    },
  };
}

async function readMetrics(cdp) {
  const { metrics } = await cdp.send("Performance.getMetrics");
  return Object.fromEntries(metrics.map(({ name, value }) => [name, value]));
}

// The browser's counters for one phase: seconds become milliseconds.
async function browserCounters(cdp, start, end) {
  const delta = (name) => ((end[name] ?? 0) - (start[name] ?? 0)) * 1000;
  const count = (name) => (end[name] ?? 0) - (start[name] ?? 0);
  try {
    await cdp.send("HeapProfiler.collectGarbage");
  } catch (error) {
    console.error(`garbage collection failed: ${error?.message ?? error}`);
  }
  const after = await readMetrics(cdp);
  return {
    task_ms: delta("TaskDuration"),
    script_ms: delta("ScriptDuration"),
    layout_ms: delta("LayoutDuration"),
    recalc_style_ms: delta("RecalcStyleDuration"),
    layout_count: count("LayoutCount"),
    recalc_style_count: count("RecalcStyleCount"),
    nodes: after.Nodes ?? null,
    layout_objects: after.LayoutObjects ?? null,
    js_heap_mb:
      typeof after.JSHeapUsedSize === "number"
        ? after.JSHeapUsedSize / 1_048_576
        : null,
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

// Runs in the page: resolves after two more frames rendered.
function nextFrames() {
  return new Promise((resolve) =>
    requestAnimationFrame(() => requestAnimationFrame(() => resolve())),
  );
}

// Runs in the page: scrolls the timeline to its top the way a mouse wheel
// does (the wheel event releases the follow pin, the scroll requests older
// History) and resolves once new timeline items appeared and two more frames
// rendered them, or after `timeoutMs` without new items.
function scrollToOlderPage({ selector, timeoutMs }) {
  const container = document.querySelector(selector);
  const content = container?.querySelector(".messages__content");
  if (!content) return { error: "the Chat timeline is not rendered" };
  const before = content.childElementCount;
  return new Promise((resolve) => {
    const started = performance.now();
    const observer = new MutationObserver(() => {
      if (content.childElementCount <= before) return;
      observer.disconnect();
      clearTimeout(timer);
      requestAnimationFrame(() =>
        requestAnimationFrame(() =>
          resolve({
            grew: true,
            items: content.childElementCount,
            page_ms: performance.now() - started,
          }),
        ),
      );
    });
    const timer = setTimeout(() => {
      observer.disconnect();
      resolve({ grew: false, items: content.childElementCount });
    }, timeoutMs);
    observer.observe(content, { childList: true });
    container.dispatchEvent(
      new WheelEvent("wheel", {
        deltaY: -container.clientHeight,
        bubbles: true,
        cancelable: true,
      }),
    );
    // Already at the top, a one-pixel move still reaches the scroll handler.
    container.scrollTop = container.scrollTop > 0 ? 0 : 1;
  });
}

function timelineCounts(selector) {
  const content = document.querySelector(`${selector} .messages__content`);
  return {
    items: content ? content.childElementCount : 0,
    user_messages: content
      ? content.querySelectorAll(":scope > .timeline-item > .msg.user").length
      : 0,
  };
}

// Measures loading the watched Session's whole History page by page.
async function scrollThroughHistory(page, cdp, history) {
  const initial = await history.wait(
    0,
    (read) => !read.before && !read.after,
    HISTORY_TIMEOUT_MS,
  );
  if (!initial) {
    return { error: "the WebUI did not read the Session's History", pages: [] };
  }
  await page
    .locator(".chat-timeline-loading")
    .waitFor({ state: "detached", timeout: HISTORY_TIMEOUT_MS });
  await page.evaluate(nextFrames);

  await page.evaluate(startMeasuring);
  const start = await readMetrics(cdp);
  const startedAt = Date.now();
  const pages = [];
  let hasMore = initial.has_more;
  let error = null;
  let attempts = 0;
  while (hasMore && pages.length < MAX_PAGES) {
    const readsBefore = history.count();
    const outcome = await page.evaluate(scrollToOlderPage, {
      selector: TIMELINE,
      timeoutMs: PAGE_TIMEOUT_MS,
    });
    if (outcome.error) {
      error = outcome.error;
      break;
    }
    const read = await history.wait(
      readsBefore,
      (entry) => Boolean(entry.before),
      outcome.grew ? PAGE_TIMEOUT_MS : 0,
    );
    if (!read) {
      attempts += 1;
      if (attempts >= PAGE_ATTEMPTS) {
        error = `no older History page loaded after ${attempts} scrolls to the top`;
        break;
      }
      continue;
    }
    attempts = 0;
    hasMore = read.has_more;
    pages.push({
      items: outcome.items,
      messages: read.messages,
      page_ms: outcome.grew ? outcome.page_ms : null,
      rpc_ms: read.rpc_ms >= 0 ? read.rpc_ms : null,
    });
    emit({ event: "progress", phase: "scroll-through", pages: pages.length });
  }
  const end = await readMetrics(cdp);
  const measured = await page.evaluate(collectMeasurements);
  const counts = await page.evaluate(timelineCounts, TIMELINE);
  return {
    error,
    duration_ms: Date.now() - startedAt,
    pages,
    timeline_items: counts.items,
    user_messages: counts.user_messages,
    frames: measured?.frames ?? null,
    frame_gaps_ms: measured?.frame_gaps_ms ?? [],
    long_tasks_ms: (measured?.long_tasks ?? []).map(([, duration]) => duration),
    browser: await browserCounters(cdp, start, end),
  };
}

// Returns to the live tail the way a user does, so the timeline follows it.
async function returnToLatest(page) {
  const jump = page.locator(JUMP_TO_LATEST);
  if (await jump.isVisible()) await jump.click({ timeout: 10_000 });
  await page.waitForFunction(
    (selector) => {
      const container = document.querySelector(selector);
      return (
        !container ||
        container.scrollHeight - container.scrollTop - container.clientHeight <
          2
      );
    },
    TIMELINE,
    { timeout: 10_000 },
  );
  await page.evaluate(nextFrames);
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
async function startProfiler(cdp) {
  await cdp.send("Profiler.enable");
  await cdp.send("Profiler.setSamplingInterval", {
    interval: PROFILE_SAMPLING_US,
  });
  await cdp.send("Profiler.start");
  return {
    async stop(path) {
      const { profile } = await cdp.send("Profiler.stop");
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
  if (values["scroll-history"] && values.view !== "chat") {
    throw new Error("--scroll-history requires --view chat");
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
    const history = createHistoryWatcher();
    history.attach(context);
    const page = await context.newPage();
    const cdp = await context.newCDPSession(page);
    await cdp.send("Performance.enable");
    const frame = await openView(page, values.view);
    let scrollThrough = null;
    if (values["scroll-history"]) {
      scrollThrough = await scrollThroughHistory(page, cdp, history);
      await returnToLatest(page);
    }

    let measuring = false;
    const measure = (target) => target.evaluate(startMeasuring).catch(() => {});
    page.on("framenavigated", (navigated) => {
      if (measuring) void measure(navigated);
    });
    measuring = true;
    rpcCounter.start();
    await Promise.all(page.frames().map(measure));
    const start = await readMetrics(cdp);
    const profiler = values.profile ? await startProfiler(cdp) : null;
    emit({ event: "ready" });

    const reason = await serveCommands(maxSeconds, {
      "select-run": () => void selectRun(frame),
    });
    measuring = false;
    const end = await readMetrics(cdp);
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
      browser: await browserCounters(cdp, start, end),
      ...(scrollThrough ? { scroll_through: scrollThrough } : {}),
    });
  } finally {
    await browser.close();
  }
}

main().catch((error) => {
  emit({ event: "error", message: String(error?.stack ?? error) });
  process.exitCode = 1;
});
