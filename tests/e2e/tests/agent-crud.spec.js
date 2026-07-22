import { expect, test } from "@playwright/test";

test("an agent can be created, renamed, and deleted", async ({ page }) => {
  await page.goto("/#agents");

  const agents = page.getByRole("region", { name: "Agents" });
  const agentList = agents.getByRole("complementary", { name: "Agents" });

  await agentList.getByRole("button", { exact: true, name: "Add" }).click();

  const createDialog = page.getByRole("dialog", { name: "Create agent" });
  await createDialog.getByLabel("Agent ID").fill("e2e-agent");
  await createDialog.getByLabel("Name").fill("E2E Agent");
  await createDialog.getByRole("button", { name: "Create agent" }).click();

  await expect(page.getByText("Agent created.", { exact: true })).toBeVisible();
  const createdAgent = agentList.getByRole("button", {
    name: /^E2E Agent(?:\s|$)/,
  });
  await expect(createdAgent).toBeVisible();
  await expect(createdAgent).toHaveClass(/active/);

  await agents
    .getByRole("textbox", { exact: true, name: "Name" })
    .fill("E2E Agent Updated");
  await agents.getByRole("button", { name: "Save changes" }).click();

  await expect(
    agentList.getByRole("button", { name: /^E2E Agent Updated(?:\s|$)/ }),
  ).toBeVisible();

  await agents.getByRole("button", { name: "Delete agent" }).click();

  await expect(page.getByText("Agent deleted.", { exact: true })).toBeVisible();
  await expect(
    agentList.getByRole("button", { name: /^E2E Agent Updated(?:\s|$)/ }),
  ).toHaveCount(0);
  await expect(
    agents.getByText("The last remaining agent cannot be deleted."),
  ).toBeVisible();
});
