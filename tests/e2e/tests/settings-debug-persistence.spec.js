import { expect, test } from "@playwright/test";

async function openDebugSettings(page) {
  await page.goto("/#settings");
  const settings = page.getByRole("region", { name: "Settings" });
  await settings.getByRole("button", { exact: true, name: "Debug" }).click();
  return settings.getByRole("region", { name: "Debug" });
}

test("Debug settings are searchable, validated, persisted, and restorable", async ({
  page,
}) => {
  await page.goto("/#settings");
  const settings = page.getByRole("region", { name: "Settings" });
  await settings
    .getByRole("searchbox", { name: "Search settings" })
    .fill("trace limit");
  await expect(settings.getByRole("status")).toContainText(/Matches: [1-9]/);
  await expect(
    settings.getByRole("button", { exact: true, name: "Debug" }),
  ).toBeVisible();

  let debug = await openDebugSettings(page);
  await debug.getByRole("switch", { name: "Enable debug mode" }).click();
  await debug.getByRole("spinbutton", { name: "Trace limit" }).fill("73");
  await debug.getByRole("button", { exact: true, name: "Save" }).click();
  await expect(page.getByText("Debug", { exact: true }).last()).toBeVisible();

  await page.reload();
  debug = page.getByRole("region", { name: "Debug" });
  await expect(
    debug.getByRole("switch", { name: "Enable debug mode" }),
  ).toBeChecked();
  await expect(
    debug.getByRole("spinbutton", { name: "Trace limit" }),
  ).toHaveValue("73");

  await debug.getByRole("switch", { name: "Enable debug mode" }).click();
  await debug.getByRole("spinbutton", { name: "Trace limit" }).fill("50");
  await debug.getByRole("button", { exact: true, name: "Save" }).click();
  await expect(
    debug.getByRole("switch", { name: "Enable debug mode" }),
  ).not.toBeChecked();
});
