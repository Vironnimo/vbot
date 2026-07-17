import { expect, test } from "@playwright/test";

test("an invalid Agent ID is rejected before creation", async ({ page }) => {
  await page.goto("/#agents");

  const agents = page.getByRole("region", { name: "Agents" });
  const agentList = agents.getByRole("complementary", { name: "Agents" });
  await agentList.getByRole("button", { exact: true, name: "Add" }).click();

  const createDialog = page.getByRole("dialog", { name: "Create agent" });
  const agentId = createDialog.getByLabel("Agent ID");
  await agentId.fill("Bad ID!");
  await createDialog.getByLabel("Name").fill("Invalid Agent");
  await createDialog.getByRole("button", { name: "Create agent" }).click();

  await expect(agentId).toHaveAttribute("aria-invalid", "true");
  await expect(
    createDialog
      .getByRole("alert")
      .filter({ hasText: "Check the highlighted fields" })
      .first(),
  ).toBeVisible();
  await expect(createDialog).toBeVisible();
  await expect(
    agentList.getByText("Invalid Agent", { exact: true }),
  ).toHaveCount(0);

  await createDialog.getByRole("button", { name: "Cancel" }).click();
});
