import { expect, test } from "@playwright/test";

import { startIsolatedChat } from "./chat-run-support.js";
import { runToolScenario } from "./chat-tool-support.js";

test("Statistics aggregates persisted Run and Tool activity across its views", async ({
  page,
}) => {
  const chat = await startIsolatedChat(page, { agentName: "Main" });
  await runToolScenario(chat, {
    prompt: "E2E_TOOL_RUNTIME Create Statistics evidence",
    finalText: "Runtime tools completed.",
  });

  await page.goto("/#statistics");
  const statistics = page.getByRole("region", { name: "Statistics" });
  await expect(
    statistics.getByRole("tab", { name: "Overview" }),
  ).toHaveAttribute("aria-selected", "true");
  const metricValue = (label) =>
    statistics
      .locator(".stats-card")
      .filter({ has: page.getByText(label, { exact: true }) })
      .locator(".stats-card__value");
  await expect(metricValue("Runs")).toHaveText(/^[1-9]\d*$/);
  await expect(metricValue("Chat messages")).toHaveText(/^[1-9]\d*$/);
  await expect(metricValue("Tool calls")).toHaveText(/^[1-9]\d*$/);

  await statistics.getByRole("tab", { name: "Tools" }).click();
  const toolTable = statistics.getByRole("table").filter({ hasText: "status" });
  await expect(
    statistics.getByRole("row", { name: /^status\s+\d+\s+100\.0%/ }),
  ).toContainText("100.0%");
  const bashRow = statistics.getByRole("row", { name: /^bash\s+/ });
  const bashCells = bashRow.getByRole("cell");
  await expect(bashCells.nth(0)).toHaveText("bash");
  await expect(bashCells.nth(1)).toHaveText(/^[1-9]\d*$/);
  const bashSuccessRate = Number.parseFloat(await bashCells.nth(2).innerText());
  const bashErrorRate = Number.parseFloat(await bashCells.nth(3).innerText());
  expect(bashSuccessRate).toBeGreaterThan(0);
  expect(bashSuccessRate + bashErrorRate).toBeCloseTo(100, 1);
  await expect(
    statistics.getByRole("row", { name: /^process\s+\d+\s+100\.0%/ }),
  ).toContainText("100.0%");
  await expect(toolTable).toBeVisible();

  await statistics.getByRole("button", { name: "Refresh" }).click();
  await expect(
    statistics.getByRole("row", { name: /^status\s+\d+\s+100\.0%/ }),
  ).toBeVisible();
});
