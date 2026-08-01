import { expect, test } from "@playwright/test";

import { startIsolatedChat } from "./chat-run-support.js";

test("Session actions rename, override Compaction Policy, and delete a Session", async ({
  page,
}) => {
  const chat = await startIsolatedChat(page, { agentName: "Main" });
  const drawer = chat.getByRole("complementary", { name: "Sessions" });
  const sessionItems = drawer.getByRole("listitem");
  const previousCount = await sessionItems.count();

  let selectedSession = drawer.locator(
    "li.session-row:has(button.session-row__select--active)",
  );
  await selectedSession
    .getByRole("button", { name: "Session actions" })
    .click();
  await page.getByRole("menuitem", { name: "Rename" }).click();
  const renameInput = drawer.getByRole("textbox", {
    name: "Rename session",
  });
  await renameInput.fill("E2E Managed Session");
  await renameInput.press("Enter");
  await expect(
    drawer.getByText("E2E Managed Session", { exact: true }),
  ).toBeVisible();

  selectedSession = sessionItems.filter({ hasText: "E2E Managed Session" });
  await selectedSession
    .getByRole("button", { name: "Session actions" })
    .click();
  await page.getByRole("menuitem", { name: "Compaction Policy" }).click();
  let policyDialog = page.getByRole("dialog", { name: "Compaction Policy" });
  const sessionOverride = policyDialog.getByRole("switch", {
    name: "Session override",
  });
  await expect(sessionOverride).not.toBeChecked();
  await sessionOverride.click();
  await policyDialog.getByRole("button", { exact: true, name: "Save" }).click();
  await expect(policyDialog).toHaveCount(0);

  await selectedSession
    .getByRole("button", { name: "Session actions" })
    .click();
  await page.getByRole("menuitem", { name: "Compaction Policy" }).click();
  policyDialog = page.getByRole("dialog", { name: "Compaction Policy" });
  await expect(
    policyDialog.getByRole("switch", { name: "Session override" }),
  ).toBeChecked();
  await policyDialog
    .getByRole("button", { exact: true, name: "Cancel" })
    .click();

  await selectedSession
    .getByRole("button", { name: "Session actions" })
    .click();
  await page.getByRole("menuitem", { name: "Delete" }).click();
  const deleteDialog = page.getByRole("dialog", { name: "Delete session" });
  await expect(deleteDialog).toContainText("E2E Managed Session");
  await deleteDialog
    .getByRole("button", { exact: true, name: "Delete" })
    .click();

  await expect(sessionItems).toHaveCount(previousCount - 1);
  await expect(
    drawer.getByText("E2E Managed Session", { exact: true }),
  ).toHaveCount(0);
});
