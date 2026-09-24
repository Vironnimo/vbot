import { expect, test } from "@playwright/test";

import { sendChatMessage, startIsolatedChat } from "./chat-run-support.js";
import { createAgent, deleteAgentIfPresent } from "./rpc-support.js";

const AGENT_ID = "prompt-agent";
const AGENT_NAME = "Prompt Agent";
const AGENT_BLOCK = "user:agent_only";
const AGENT_BLOCK_TEXT = "Agent-only E2E prompt 3307";

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

test("an Agent-scoped System Prompt block reaches only that Agent's Provider prompt", async ({
  page,
  request,
}) => {
  try {
    await createAgent(request, {
      id: AGENT_ID,
      name: AGENT_NAME,
      custom_system_prompt_enabled: true,
    });

    await page.goto("/#system-prompt");
    const systemPrompt = page.getByRole("region", { name: "System Prompt" });
    await systemPrompt
      .getByRole("tab", { name: "Edit blocks", exact: true })
      .click();
    await selectPromptScope(page, systemPrompt, AGENT_NAME);

    page.once("dialog", (dialog) => dialog.accept("agent_only"));
    await systemPrompt.getByRole("button", { name: "New block" }).click();
    const agentBlock = systemPrompt
      .getByRole("listitem")
      .filter({ hasText: AGENT_BLOCK });
    await agentBlock.getByRole("textbox").fill(AGENT_BLOCK_TEXT);
    await systemPrompt
      .getByRole("button", { exact: true, name: "Save" })
      .click();
    await expect(page.getByText("Saved", { exact: true })).toBeVisible();

    await selectPromptScope(page, systemPrompt, "Default");
    await expect(
      systemPrompt.getByText(AGENT_BLOCK, { exact: true }),
    ).toHaveCount(0);

    let chat = await startIsolatedChat(page, { agentName: AGENT_NAME });
    await sendChatMessage(chat, "E2E_PROMPT_SCOPE_CHECK Inspect this Agent");
    await expect(
      chat.getByText("Agent-scoped System Prompt reached the Provider.", {
        exact: true,
      }),
    ).toBeVisible();

    chat = await startIsolatedChat(page, { agentName: "Main" });
    await sendChatMessage(chat, "E2E_PROMPT_SCOPE_CHECK Inspect Main");
    await expect(
      chat.getByText(
        "Agent-scoped System Prompt stayed out of the Provider prompt.",
        { exact: true },
      ),
    ).toBeVisible();
  } finally {
    await deleteAgentIfPresent(request, AGENT_ID);
  }
});
