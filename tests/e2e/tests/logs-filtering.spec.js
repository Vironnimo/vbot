import { expect, test } from "@playwright/test";

test("Logs reads the daily server log and applies search, level, and order filters", async ({
  page,
}) => {
  await page.goto("/#logs");
  const logs = page.getByRole("region", { name: "Logs" });

  await expect(logs.getByText(/Current file: /)).toBeVisible();
  await expect(logs.getByRole("list", { name: "Log entries" })).toBeVisible();

  await logs.getByRole("searchbox", { name: "Search" }).fill("vbot.server.app");
  const entries = logs.getByRole("listitem");
  await expect(entries).not.toHaveCount(0);
  await expect(entries.first()).toContainText("vbot.server.app");

  await logs.getByRole("button", { name: "Level" }).click();
  await page.getByRole("option", { name: "INFO" }).click();
  await expect(entries.first()).toContainText("INFO");

  await logs.getByRole("button", { name: "Order" }).click();
  await page.getByRole("option", { name: "Oldest first" }).click();
  await expect(logs.getByRole("button", { name: "Order" })).toContainText(
    "Oldest first",
  );

  await logs
    .getByRole("searchbox", { name: "Search" })
    .fill("no-e2e-log-entry-can-match-5821");
  await expect(
    logs.getByText("No entries match the current filters", { exact: true }),
  ).toBeVisible();
});
