// WebUI responsiveness probe for scripts/perf_load.py (--ui).
//
// Opens the harness server's WebUI on one Agent's current Session, prints a
// `{"event":"ready"}` line once the Chat is visible, and measures until it
// reads `stop` on stdin (or --max-seconds elapse). It then prints one
// `{"event":"result",...}` line with Long Tasks, frame gaps (rAF intervals
// above 50 ms) and the DOM latency of the fake Provider's timing markers.
//
// Lives outside the Playwright testDir (./tests) so E2E runs never collect it;
// it reuses the E2E Playwright installation (`npm ci` in tests/e2e).

import { createInterface } from "node:readline";
import { parseArgs } from "node:util";

import { chromium } from "@playwright/test";

const FRAME_GAP_MS = 50;

const { values } = parseArgs({
  options: {
    url: { type: "string" },
    agent: { type: "string" },
    "max-seconds": { type: "string", default: "900" },
  },
});

function emit(payload) {
  process.stdout.write(`${JSON.stringify(payload)}\n`);
}

function waitForStop(maxSeconds) {
  return new Promise((resolve) => {
    const timer = setTimeout(() => resolve("timeout"), maxSeconds * 1000);
    const lines = createInterface({ input: process.stdin });
    lines.on("line", (line) => {
      if (line.trim() === "stop") {
        clearTimeout(timer);
        lines.close();
        resolve("stop");
      }
    });
    lines.on("close", () => {
      clearTimeout(timer);
      resolve("stdin-closed");
    });
  });
}

// Runs inside the page before any application script.
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
      state.longTasks.push(entry.duration);
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

async function main() {
  const maxSeconds = Number.parseFloat(values["max-seconds"]);
  if (!values.url || !values.agent || !Number.isFinite(maxSeconds)) {
    throw new Error("--url, --agent and a numeric --max-seconds are required");
  }
  const browser = await chromium.launch({ headless: true });
  try {
    const context = await browser.newContext({
      viewport: { width: 1440, height: 900 },
    });
    await context.addInitScript((agentId) => {
      localStorage.setItem("vbot.selectedAgentId", agentId);
      localStorage.setItem("vbot.onboardingDismissed", "1");
    }, values.agent);
    await context.addInitScript(installObservers, FRAME_GAP_MS);
    const page = await context.newPage();
    await page.goto(`${values.url}/#chat`);
    await page
      .getByRole("region", { name: "Chat" })
      .first()
      .waitFor({ timeout: 30_000 });

    await page.evaluate(() => {
      const state = window.__vbotPerfProbe;
      state.measuring = true;
      state.startedAt = performance.now();
    });
    emit({ event: "ready" });

    const reason = await waitForStop(maxSeconds);
    const result = await page.evaluate(() => {
      const state = window.__vbotPerfProbe;
      state.measuring = false;
      return {
        duration_ms: performance.now() - state.startedAt,
        frames: state.frames,
        frame_gaps_ms: state.frameGaps,
        long_tasks_ms: state.longTasks,
        mutations: state.mutations,
        marker_latencies_ms: state.markerLatencies,
        heap_used_mb: performance.memory
          ? performance.memory.usedJSHeapSize / 1_048_576
          : null,
        dom_nodes: document.getElementsByTagName("*").length,
      };
    });
    emit({ event: "result", stop_reason: reason, ...result });
  } finally {
    await browser.close();
  }
}

main().catch((error) => {
  emit({ event: "error", message: String(error?.stack ?? error) });
  process.exitCode = 1;
});
