import { expect, test } from "@playwright/test";

import { sendChatMessage, startIsolatedChat } from "./chat-run-support.js";

test("a normal message resumes interrupted work without recovery controls", async ({
  page,
}) => {
  const chat = await startIsolatedChat(page, { agentName: "Main" });

  await sendChatMessage(
    chat,
    "E2E_SLOW E2E_COMMAND_CONTINUE retain this interrupted work",
  );
  await expect(chat.getByText(/Slow response started\./)).toBeVisible();

  await chat.getByRole("textbox", { name: "Message" }).fill("/stop");
  await chat.getByRole("button", { name: "Queue message" }).click();
  await expect(chat.getByText("Run cancelled.", { exact: true })).toBeVisible();
  await expect(chat.getByText("Cancelled", { exact: true })).toBeVisible();
  await expect(
    chat.getByText("Interrupted work retained", { exact: true }),
  ).toHaveCount(0);

  await sendChatMessage(chat, "Resume after cancellation");
  await expect(
    chat.getByText("Continued interrupted work completed.", { exact: true }),
  ).toBeVisible();

  await sendChatMessage(
    chat,
    "E2E_ERROR E2E_COMMAND_CONTINUE simulate a Provider interruption",
  );
  await expect(
    chat.getByText("Provider error: 400 E2E provider failure", { exact: true }),
  ).toBeVisible();
  await expect(
    chat.getByText("Interrupted work retained", { exact: true }),
  ).toHaveCount(0);

  await sendChatMessage(chat, "Resume after provider interruption");
  await expect(
    chat.getByText("Continued interrupted work completed.", { exact: true }),
  ).toHaveCount(2);
  await expect(
    chat.getByRole("button", { exact: true, name: "Continue" }),
  ).toHaveCount(0);
  await expect(
    chat.getByRole("button", { exact: true, name: "Discard" }),
  ).toHaveCount(0);
  await expect(chat.getByText("· Running", { exact: true })).toHaveCount(0);
});
