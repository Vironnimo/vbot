import { expect, test } from "@playwright/test";

import { deleteAgentIfPresent } from "./rpc-support.js";

test("an agent can be created, renamed, and deleted", async ({
  page,
  request,
}) => {
  try {
    await page.goto("/#agents");

    const agents = page.getByRole("region", { name: "Agents" });
    const agentList = agents.getByRole("complementary", { name: "Agents" });

    await agentList.getByRole("button", { name: "Create agent" }).click();

    const createDialog = page.getByRole("dialog", { name: "Create agent" });
    await createDialog.getByLabel("Agent ID").fill("e2e-agent");
    await createDialog.getByLabel("Name").fill("E2E Agent");
    await createDialog.getByRole("button", { name: "Create agent" }).click();

    await expect(
      page.getByText("Agent created.", { exact: true }),
    ).toBeVisible();
    const createdAgent = agentList.getByRole("button", {
      name: /^E2E Agent(?:\s|$)/,
    });
    await expect(createdAgent).toBeVisible();
    await expect(createdAgent).toHaveClass(/active/);

    const identity = agents.getByRole("region", { name: "Identity" });
    await identity
      .getByRole("textbox", { exact: true, name: "Name" })
      .fill("E2E Agent Updated");

    // The editor autosaves; reload only after the saved state is confirmed.
    await expect(
      agentList.getByRole("button", { name: /^E2E Agent Updated(?:\s|$)/ }),
    ).toBeVisible();
    await expect(
      agents.getByRole("button", { exact: true, name: "Saved" }),
    ).toBeVisible();
    await page.reload();
    await expect(
      agentList.getByRole("button", { name: /^E2E Agent Updated(?:\s|$)/ }),
    ).toBeVisible();

    await agentList
      .getByRole("button", { name: /^E2E Agent Updated(?:\s|$)/ })
      .click();
    await agents.getByText("Workspace & advanced", { exact: true }).click();
    await agents.getByRole("button", { name: "Delete agent" }).click();

    await expect(
      page.getByText("Agent deleted.", { exact: true }),
    ).toBeVisible();
    await expect(
      agentList.getByRole("button", { name: /^E2E Agent Updated(?:\s|$)/ }),
    ).toHaveCount(0);
  } finally {
    await deleteAgentIfPresent(request, "e2e-agent");
  }
});
