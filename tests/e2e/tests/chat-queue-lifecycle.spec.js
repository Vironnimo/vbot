import { expect, test } from "@playwright/test";

import { startIsolatedChat } from "./chat-run-support.js";

test("queued Chat messages can be edited, removed, and run in FIFO order", async ({
  page,
}) => {
  const chat = await startIsolatedChat(page);
  const messageInput = chat.getByRole("textbox", { name: "Message" });

  await messageInput.fill("E2E_QUEUE_ACTIVE_LONG Keep this Run active");
  await chat.getByRole("button", { name: "Send message" }).click();
  await expect(chat.getByText(/Queue run started\./)).toBeVisible();

  await messageInput.fill("E2E_QUEUE_FIRST Original first queued message");
  await chat.getByRole("button", { name: "Queue message" }).click();

  await messageInput.fill("E2E_QUEUE_REMOVED This message must never run");
  await chat.getByRole("button", { name: "Queue message" }).click();

  await messageInput.fill("E2E_QUEUE_THIRD Final queued message");
  await chat.getByRole("button", { name: "Queue message" }).click();

  const queue = chat.getByRole("complementary", {
    name: "Queued messages",
  });
  await expect(queue.getByText("3 queued", { exact: true })).toBeVisible();
  await expect(
    queue.getByText("E2E_QUEUE_FIRST Original first queued message", {
      exact: true,
    }),
  ).toBeVisible();

  const firstQueuedItem = queue
    .getByRole("listitem")
    .filter({ hasText: "E2E_QUEUE_FIRST" });
  await firstQueuedItem
    .getByRole("button", { name: "Edit queued message" })
    .click();
  await queue
    .getByRole("textbox")
    .fill("E2E_QUEUE_FIRST Edited first queued message");
  await queue.getByRole("button", { exact: true, name: "Save" }).click();

  await expect(
    queue.getByText("E2E_QUEUE_FIRST Edited first queued message", {
      exact: true,
    }),
  ).toBeVisible();

  const removedQueuedItem = queue
    .getByRole("listitem")
    .filter({ hasText: "E2E_QUEUE_REMOVED" });
  await removedQueuedItem
    .getByRole("button", { name: "Remove queued message" })
    .click();
  await expect(queue.getByText("2 queued", { exact: true })).toBeVisible();
  await expect(queue.getByText(/E2E_QUEUE_REMOVED/)).toHaveCount(0);

  await expect(queue).toHaveCount(0, { timeout: 20_000 });
  await expect(
    chat.getByText("E2E_QUEUE_FIRST Edited first queued message", {
      exact: true,
    }),
  ).toBeVisible();
  await expect(
    chat.getByText("First queued response.", { exact: true }),
  ).toBeVisible();
  await expect(
    chat.getByText("E2E_QUEUE_THIRD Final queued message", { exact: true }),
  ).toBeVisible();
  await expect(
    chat.getByText("Third queued response.", { exact: true }),
  ).toBeVisible();
  await expect(chat.getByText(/E2E_QUEUE_REMOVED/)).toHaveCount(0);
  await expect(
    chat.getByText("Removed queued message unexpectedly ran.", { exact: true }),
  ).toHaveCount(0);

  const userMessages = await chat
    .locator(".msg.user .msg-body-text--user")
    .allTextContents();
  const firstIndex = userMessages.findIndex((text) =>
    text.includes("E2E_QUEUE_FIRST"),
  );
  const thirdIndex = userMessages.findIndex((text) =>
    text.includes("E2E_QUEUE_THIRD"),
  );
  expect(firstIndex).toBeGreaterThan(-1);
  expect(thirdIndex).toBeGreaterThan(firstIndex);
  await expect(chat.getByText("· Running", { exact: true })).toHaveCount(0);
});
