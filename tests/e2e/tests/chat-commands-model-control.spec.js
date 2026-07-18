import { expect, test } from "@playwright/test";

import { sendChatMessage, startIsolatedChat } from "./chat-run-support.js";

test("the model command changes and resets the active Agent Model", async ({
  page,
}) => {
  const chat = await startIsolatedChat(page, { agentName: "Main" });

  await sendChatMessage(chat, "/model fake/e2e-fallback::local");
  await expect(
    chat.getByText("Model set to fake/e2e-fallback::local.", { exact: true }),
  ).toBeVisible();

  await sendChatMessage(chat, "Confirm the temporary Model selection");
  await expect(
    chat.getByText("Fallback provider response.", { exact: true }),
  ).toBeVisible();

  await sendChatMessage(chat, "/model reset");
  await expect(chat.getByText("Model reset.", { exact: true })).toBeVisible();

  await sendChatMessage(chat, "E2E_STREAM Confirm the inherited Model");
  await expect(
    chat.getByText("Fake provider streaming response.", { exact: true }),
  ).toBeVisible();
});
