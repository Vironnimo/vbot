import { expect, test } from "@playwright/test";

import {
  chatTimeline,
  sendChatMessage,
  startIsolatedChat,
} from "./chat-run-support.js";

const PROMPT = "E2E_FALLBACK Exercise model fallback";
const NOTICE =
  "fake/e2e-primary::default was unavailable. Switched to fake/e2e-fallback::default for this Run.";

test("a retryable primary failure switches the Chat Run to its fallback model", async ({
  page,
}) => {
  test.setTimeout(60_000);
  const chat = await startIsolatedChat(page);
  await sendChatMessage(chat, PROMPT);

  await expect(
    chatTimeline(chat).getByText("Fallback provider response.", {
      exact: true,
    }),
  ).toBeVisible({ timeout: 45_000 });
  await expect(chat.getByText("· Running", { exact: true })).toHaveCount(0);
  // The switch notice stays with the completed Run.
  await expect(
    chatTimeline(chat).getByText(NOTICE, { exact: true }),
  ).toHaveCount(1);

  // A fresh page load rebuilds the Run from the Session History alone.
  await page.reload();
  const reloadedChat = page.getByRole("region", { name: "Chat" });
  await expect(
    chatTimeline(reloadedChat).getByText(PROMPT, { exact: true }),
  ).toHaveCount(1);
  await expect(
    chatTimeline(reloadedChat).getByText("Fallback provider response.", {
      exact: true,
    }),
  ).toBeVisible();
  await expect(
    chatTimeline(reloadedChat).getByText(NOTICE, { exact: true }),
  ).toHaveCount(1);
});
