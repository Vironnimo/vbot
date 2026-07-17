import { expect, test } from "@playwright/test";

test("a custom System Prompt block persists and can be removed", async ({
  page,
}) => {
  await page.goto("/#system-prompt");

  const systemPrompt = page.getByRole("region", { name: "System Prompt" });
  page.once("dialog", (dialog) => dialog.accept("e2e_notes"));
  await systemPrompt.getByRole("button", { name: "New block" }).click();

  let customBlock = systemPrompt
    .getByRole("listitem")
    .filter({ hasText: "user:e2e_notes" });
  await expect(customBlock).toBeVisible();
  await customBlock.getByRole("textbox").fill("E2E custom prompt guidance");
  await systemPrompt.getByRole("button", { exact: true, name: "Save" }).click();
  await expect(page.getByText("Saved", { exact: true })).toBeVisible();

  await page.reload();
  customBlock = page
    .getByRole("region", { name: "System Prompt" })
    .getByRole("listitem")
    .filter({ hasText: "user:e2e_notes" });
  await expect(customBlock.getByRole("textbox")).toHaveValue(
    "E2E custom prompt guidance",
  );

  await customBlock.getByRole("button", { name: "Remove" }).click();
  const removeDialog = page.getByRole("dialog", { name: "Remove block" });
  await expect(removeDialog).toContainText("This cannot be undone.");
  await removeDialog
    .getByRole("button", { exact: true, name: "Remove" })
    .click();

  await expect(
    page
      .getByRole("region", { name: "System Prompt" })
      .getByText("user:e2e_notes", { exact: true }),
  ).toHaveCount(0);
});
