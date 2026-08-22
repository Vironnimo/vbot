import { expect, test } from "@playwright/test";

import { startIsolatedChat } from "./chat-run-support.js";

test("cancelling a streaming Chat Run retains its visible output", async ({
  page,
}) => {
  const chat = await startIsolatedChat(page);
  await chat
    .getByRole("textbox", { name: "Message" })
    .fill("E2E_SLOW Keep streaming until cancelled");
  await chat.getByRole("button", { name: "Send message" }).click();

  await expect(chat.getByText(/Slow response started\./)).toBeVisible();
  await chat.getByRole("button", { name: "Cancel run" }).click();

  await expect(chat.getByText("Cancelled", { exact: true })).toBeVisible();
  await expect(chat.getByText(/Slow response started\./)).toBeVisible();
  await expect(
    chat.getByText("Interrupted work retained", { exact: true }),
  ).toHaveCount(0);
  await expect(
    chat.getByRole("button", { exact: true, name: "Discard" }),
  ).toHaveCount(0);
  await expect(chat.getByRole("button", { name: "Cancel run" })).toHaveCount(0);
});
