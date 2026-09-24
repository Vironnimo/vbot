import { expect, test } from "@playwright/test";

import { startIsolatedChat } from "./chat-run-support.js";
import { runToolScenario } from "./chat-tool-support.js";

// Earlier specs share the E2E data directory, so counts are lower bounds.
const POSITIVE_COUNT = /^[1-9]\d*$/;

function definitionFor(region, term) {
  return region
    .getByRole("term")
    .filter({ hasText: new RegExp(`^${term}$`) })
    .locator("xpath=following-sibling::dd[1]");
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
  await expect(definitionFor(overview, "Runs")).toHaveText(POSITIVE_COUNT);
  await expect(definitionFor(overview, "Recorded Model calls")).toHaveText(
    POSITIVE_COUNT,
  );
  const primaryModel = overview
    .getByRole("region", { name: "Models using the most tokens" })
    .getByRole("row", { name: /^fake\/e2e-primary\s/ })
    .getByRole("cell");
  await expect(primaryModel.nth(1)).toHaveText(POSITIVE_COUNT);

  await statistics.getByRole("tab", { name: "Tools" }).click();
  const tools = statistics.getByRole("tabpanel", { name: "Tools" });
  for (const toolName of ["status", "process"]) {
    const cells = tools
      .getByRole("row", { name: new RegExp(`^${toolName}\\s`) })
      .getByRole("cell");
    await expect(cells.nth(0)).toHaveText(toolName);
    await expect(cells.nth(1)).toHaveText(POSITIVE_COUNT);
    await expect(cells.nth(2)).toHaveText("100.0%");
  }
  const bashCells = tools
    .getByRole("row", { name: /^bash\s/ })
    .getByRole("cell");
  await expect(bashCells.nth(1)).toHaveText(POSITIVE_COUNT);
  // Cancelled bash calls from other specs may lower, but never zero, its rate.
  expect(Number.parseFloat(await bashCells.nth(2).innerText())).toBeGreaterThan(
    0,
  );
});
