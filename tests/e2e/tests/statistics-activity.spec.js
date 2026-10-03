import { expect, test } from "@playwright/test";

import { startIsolatedChat } from "./chat-run-support.js";
import { runToolScenario } from "./chat-tool-support.js";

// Earlier specs share the E2E data directory, so counts are lower bounds.
// Statistics formats them with thousands separators (en-US browser locale).
const POSITIVE_COUNT = /^[1-9]\d{0,2}(?:,\d{3})*$/;

function tileValue(region, label) {
  return region
    .locator(".stats-tile")
    .filter({ has: region.page().getByText(label, { exact: true }) })
    .locator(".stats-tile__value");
}

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
  const overview = statistics.getByRole("tabpanel", { name: "Overview" });
  await expect(tileValue(overview, "Runs")).toHaveText(POSITIVE_COUNT);
  await expect(
    overview
      .getByRole("table", { name: "Top Models" })
      .getByRole("cell", { name: "fake/e2e-primary", exact: true }),
  ).toBeVisible();

  await statistics.getByRole("tab", { name: "Costs & tokens" }).click();
  const usage = statistics.getByRole("tabpanel", {
    name: "Costs & tokens",
  });
  await expect(tileValue(usage, "Model calls")).toHaveText(POSITIVE_COUNT);
  await usage.getByRole("tab", { name: "Model", exact: true }).click();
  const primaryModel = usage
    .getByRole("table", { name: "Cost and tokens by Model" })
    .getByRole("row", { name: /^fake\/e2e-primary\s/ })
    .getByRole("cell");
  await expect(primaryModel.nth(1)).toHaveText(POSITIVE_COUNT);
  expect(
    Number.parseFloat(await primaryModel.nth(2).innerText()),
  ).toBeGreaterThan(0);

  await statistics.getByRole("tab", { name: "Tools & skills" }).click();
  const tools = statistics
    .getByRole("tabpanel", { name: "Tools & skills" })
    .getByRole("table", { name: "Per Tool" });
  for (const toolName of ["status", "process"]) {
    const cells = tools
      .getByRole("row", { name: new RegExp(`^${toolName}\\s`) })
      .getByRole("cell");
    await expect(cells.nth(0)).toHaveText(toolName);
    await expect(cells.nth(1)).toHaveText(POSITIVE_COUNT);
    await expect(cells.nth(2)).toHaveText("0.0%");
  }
  const bashCells = tools
    .getByRole("row", { name: /^bash\s/ })
    .getByRole("cell");
  await expect(bashCells.nth(1)).toHaveText(POSITIVE_COUNT);
  // Other specs may reject shell calls; this journey supplies an accepted call.
  expect(Number.parseFloat(await bashCells.nth(2).innerText())).toBeLessThan(
    100,
  );
});
