import { expect, test } from "@playwright/test";

async function selectPromptScope(page, systemPrompt, name) {
  const promptScope = systemPrompt.getByRole("button", {
    name: "Prompt scope",
  });
  const option = page.getByRole("option", { name });

  await expect(async () => {
    if (!(await option.isVisible())) {
      await promptScope.click();
    }
    await option.click({ timeout: 1_000 });
    await expect(promptScope).toContainText(name, { timeout: 1_000 });
  }).toPass({ timeout: 7_500 });
}

test("an Agent-specific System Prompt stays isolated from the Default scope", async ({
  page,
}) => {
  await page.goto("/#agents");

  const agents = page.getByRole("region", { name: "Agents" });
  const agentList = agents.getByRole("complementary", { name: "Agents" });
  await agentList.getByRole("button", { exact: true, name: "Add" }).click();

  const createDialog = page.getByRole("dialog", { name: "Create agent" });
  await createDialog.getByLabel("Agent ID").fill("prompt-agent");
  await createDialog.getByLabel("Name").fill("Prompt Agent");
  await createDialog.getByRole("button", { name: "Create agent" }).click();
  await expect(page.getByText("Agent created.", { exact: true })).toBeVisible();
  await expect(
    agentList.getByRole("button", { name: /^Prompt Agent(?:\s|$)/ }),
  ).toHaveClass(/active/);

  await agents.getByRole("switch", { name: "Custom system prompt" }).click();
  await agents.getByRole("button", { name: "Save changes" }).click();
  await expect(page.getByText("Agent updated.", { exact: true })).toBeVisible();

  await page.getByRole("button", { name: "System Prompt" }).click();
  const systemPrompt = page.getByRole("region", { name: "System Prompt" });
  const promptScope = systemPrompt.getByRole("button", {
    name: "Prompt scope",
  });
  await selectPromptScope(page, systemPrompt, "Prompt Agent");
  await expect(promptScope).toContainText("Prompt Agent");

  page.once("dialog", (dialog) => dialog.accept("agent_only"));
  await systemPrompt.getByRole("button", { name: "New block" }).click();
  let agentBlock = systemPrompt
    .getByRole("listitem")
    .filter({ hasText: "user:agent_only" });
  await agentBlock.getByRole("textbox").fill("Agent-only E2E prompt");
  await systemPrompt.getByRole("button", { exact: true, name: "Save" }).click();
  const savedToast = page.getByText("Saved", { exact: true });
  await expect(savedToast).toBeVisible();
  await expect(savedToast).toBeHidden();

  await selectPromptScope(page, systemPrompt, "Default");
  await expect(promptScope).toContainText("Default");
  await expect(
    systemPrompt.getByText("user:agent_only", { exact: true }),
  ).toHaveCount(0);

  await selectPromptScope(page, systemPrompt, "Prompt Agent");
  agentBlock = systemPrompt
    .getByRole("listitem")
    .filter({ hasText: "user:agent_only" });
  await expect(agentBlock.getByRole("textbox")).toHaveValue(
    "Agent-only E2E prompt",
  );

  await agentBlock.getByRole("button", { name: "Remove" }).click();
  const removeDialog = page.getByRole("dialog", { name: "Remove block" });
  await removeDialog
    .getByRole("button", { exact: true, name: "Remove" })
    .click();
  await expect(
    systemPrompt.getByText("user:agent_only", { exact: true }),
  ).toHaveCount(0);

  await page.getByRole("button", { name: "Agents" }).click();
  await agentList
    .getByRole("button", { name: /^Prompt Agent(?:\s|$)/ })
    .click();
  await agents.getByRole("button", { name: "Delete agent" }).click();
  await expect(page.getByText("Agent deleted.", { exact: true })).toBeVisible();
});
