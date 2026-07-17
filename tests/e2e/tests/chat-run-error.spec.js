import { expect, test } from "@playwright/test";

import { startIsolatedChat } from "./chat-run-support.js";

test("a provider failure is visible and leaves the Chat recoverable", async ({
  page,
}) => {
  const chat = await startIsolatedChat(page);
  await chat
    .getByRole("textbox", { name: "Message" })
    .fill("E2E_ERROR Return the configured failure");
  await chat.getByRole("button", { name: "Send message" }).click();

  await expect(
    chat.getByText("Provider error: 400 E2E provider failure", { exact: true }),
  ).toBeVisible();
  await expect(chat.getByText("ERROR", { exact: true })).toBeVisible();
  await expect(
    chat.getByText("Interrupted work retained", { exact: true }),
  ).toBeVisible();
  await expect(
    chat.getByRole("button", { exact: true, name: "Continue" }),
  ).toBeVisible();
});
