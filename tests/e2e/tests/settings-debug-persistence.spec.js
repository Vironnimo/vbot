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

function debugSwitch(debug) {
  return debug.getByRole("switch", { name: "Enable debug mode" });
}

// The trace limit row stays mounted but hidden while Debug mode is off.
function traceLimit(debug) {
  return debug.getByRole("spinbutton", {
    includeHidden: true,
    name: "Trace limit",
  });
}

// The section autosaves after a short delay. Wait for the save the switch
// starts: the section can still read "Saved" from an earlier change.
async function setDebugMode(page, debug, enabled) {
  const saved = page.waitForResponse(
    (response) =>
      response.url().endsWith("/api/rpc") &&
      response.request().postDataJSON()?.method === "settings.update",
  );
  await debugSwitch(debug).click();
  expect(await (await saved).json()).toMatchObject({ ok: true });
  await expect(debugSwitch(debug)).toBeChecked({ checked: enabled });
}

// Number fields autosave on blur; the section's save status then settles.
async function setTraceLimit(debug, value) {
  await traceLimit(debug).fill(value);
  await traceLimit(debug).press("Tab");
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
  // Search finds the individual setting; opening it reveals its section.
  await settings.getByRole("button", { name: /^Trace limit\b/ }).click();

  let debug = settings.getByRole("region", { name: "Debug" });
  await expect(debug).toBeVisible();
  await setDebugMode(page, debug, true);
  await setTraceLimit(debug, "73");

  await page.reload();
  debug = await openDebugSettings(page);
  await expect(debugSwitch(debug)).toBeChecked();
  await expect(traceLimit(debug)).toHaveValue("73");

  // The trace limit is editable only while Debug mode is on.
  await setTraceLimit(debug, "50");
  await setDebugMode(page, debug, false);
  await expect(traceLimit(debug)).toBeHidden();
  await page.reload();
  debug = await openDebugSettings(page);
  await expect(debugSwitch(debug)).not.toBeChecked();
  await expect(traceLimit(debug)).toHaveValue("50");
});
