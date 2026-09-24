import { expect, test } from "@playwright/test";

async function openDebugSettings(page) {
  await page.goto("/#settings");
  const settings = page.getByRole("region", { name: "Settings" });
  await settings
    .getByRole("navigation", { name: "Settings sections" })
    .getByRole("button", { exact: true, name: "System" })
    .click();
  return settings.getByRole("region", { name: "Debug" });
}

// Number fields autosave on blur; the section's save status then settles.
async function setTraceLimit(debug, value) {
  const traceLimit = debug.getByRole("spinbutton", { name: "Trace limit" });
  await traceLimit.fill(value);
  await traceLimit.press("Tab");
  await expect(
    debug.getByRole("button", { exact: true, name: "Saved" }),
  ).toBeVisible();
}

test("Debug settings are searchable, persisted, and restorable", async ({
  page,
}) => {
  await page.goto("/#settings");
  const settings = page.getByRole("region", { name: "Settings" });
  await settings
    .getByRole("searchbox", { name: "Search settings" })
    .fill("trace limit");
  await expect(
    settings.getByRole("heading", { name: "Search results" }),
  ).toBeVisible();
  await settings.getByRole("button", { name: /^Debug\b/ }).click();

  let debug = settings.getByRole("region", { name: "Debug" });
  await expect(debug).toBeVisible();
  await debug.getByRole("switch", { name: "Enable debug mode" }).click();
  await setTraceLimit(debug, "73");

  await page.reload();
  debug = await openDebugSettings(page);
  await expect(
    debug.getByRole("switch", { name: "Enable debug mode" }),
  ).toBeChecked();
  await expect(
    debug.getByRole("spinbutton", { name: "Trace limit" }),
  ).toHaveValue("73");

  await debug.getByRole("switch", { name: "Enable debug mode" }).click();
  await setTraceLimit(debug, "50");
  await page.reload();
  debug = await openDebugSettings(page);
  await expect(
    debug.getByRole("switch", { name: "Enable debug mode" }),
  ).not.toBeChecked();
  await expect(
    debug.getByRole("spinbutton", { name: "Trace limit" }),
  ).toHaveValue("50");
});
