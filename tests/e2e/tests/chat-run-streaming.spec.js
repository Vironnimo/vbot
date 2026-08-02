import { expect, test } from "@playwright/test";

import { startIsolatedChat } from "./chat-run-support.js";

test("a Chat Run streams a provider response to completion", async ({
  page,
}) => {
  const chat = await startIsolatedChat(page);
  await chat
    .getByRole("textbox", { name: "Message" })
    .fill("E2E_STREAM Please demonstrate streaming");
  await chat.getByRole("button", { name: "Send message" }).click();

  await expect(
    chat.getByText("Fake provider streaming response.", { exact: true }),
  ).toBeVisible();
  await expect(chat.getByText("· Running", { exact: true })).toHaveCount(0);
  await expect(
    chat.getByRole("button", { exact: true, name: "New session" }),
  ).toBeEnabled();
});

test("completed History replaces stale live replay after Chat navigation", async ({
  context,
  page,
}) => {
  test.setTimeout(60_000);
  const chat = await startIsolatedChat(page);
  const prompt = "E2E_SLOW Preserve this completed turn exactly once";
  await chat.getByRole("textbox", { name: "Message" }).fill(prompt);
  await chat.getByRole("button", { name: "Send message" }).click();

  await expect(
    chat.getByText("Slow response started.", { exact: false }),
  ).toBeVisible();
  await context.setOffline(true);
  await page.getByRole("button", { exact: true, name: "Agents" }).click();

  // The server-owned Run continues after the browser loses both SSE and WS.
  // Returning online therefore reconciles a locally stale Running projection
  // against durable History with no active Run.
  await page.waitForTimeout(18_000);
  await context.setOffline(false);
  await expect(page.getByText("Connected", { exact: true })).toBeVisible({
    timeout: 15_000,
  });
  await page.getByRole("button", { exact: true, name: "Chat" }).click();

  await expect(chat.getByText(prompt, { exact: true })).toHaveCount(1);
  await expect(chat.locator("article.msg.assistant")).toHaveCount(1);
  await expect(chat.getByText("· Running", { exact: true })).toHaveCount(0);
  await expect(
    chat.getByRole("button", { exact: true, name: "New session" }),
  ).toBeEnabled();
});
