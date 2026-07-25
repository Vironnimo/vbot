import { expect, test } from "@playwright/test";

import { sendChatMessage, startIsolatedChat } from "./chat-run-support.js";

test("slash command discovery, help, status, rename, and new Session work together", async ({
  page,
}) => {
  const chat = await startIsolatedChat(page, { agentName: "Main" });
  const message = chat.getByRole("textbox", { name: "Message" });

  await message.fill("/");
  const suggestions = page.getByRole("listbox", { name: "Skill suggestions" });
  await expect(suggestions).toBeVisible();
  await expect(
    suggestions.getByRole("option").filter({ hasText: /^help\b/ }),
  ).toBeVisible();
  await expect(
    suggestions.getByRole("option").filter({ hasText: /^rename\b/ }),
  ).toBeVisible();

  await message.fill("/sta");
  await suggestions
    .getByRole("option")
    .filter({ hasText: /^status\b/ })
    .click();
  let commandOutputs = chat.getByRole("note", { name: "Command output" });
  await expect(commandOutputs.last()).toContainText("Fake Primary");

  await sendChatMessage(chat, "/help");
  commandOutputs = chat.getByRole("note", { name: "Command output" });
  await expect(commandOutputs.last()).toContainText("Built-in slash commands:");
  await expect(commandOutputs.last()).toContainText("/handoff");
  await expect(commandOutputs.last()).not.toContainText("/continue");

  await sendChatMessage(chat, "/rename E2E Slash Command Session");
  await expect(
    chat.getByText("Session renamed to E2E Slash Command Session.", {
      exact: true,
    }),
  ).toBeVisible();
  const drawer = chat.getByRole("complementary", { name: "Sessions" });
  await expect(
    drawer.getByText("E2E Slash Command Session", { exact: true }),
  ).toBeVisible();
  const previousSessionCount = await drawer.getByRole("listitem").count();

  await sendChatMessage(chat, "/new");
  await expect(drawer.getByRole("listitem")).toHaveCount(
    previousSessionCount + 1,
  );
  await expect(
    chat.getByText("No messages yet", { exact: true }).first(),
  ).toBeVisible();
  await expect(
    drawer.getByText("E2E Slash Command Session", { exact: true }),
  ).toBeVisible();
});
