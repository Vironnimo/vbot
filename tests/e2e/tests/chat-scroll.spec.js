import { expect, test } from "@playwright/test";

import { sendChatMessage, startIsolatedChat } from "./chat-run-support.js";

async function scrollMetrics(messages) {
  return messages.evaluate((element) => ({
    clientHeight: element.clientHeight,
    distanceFromBottom:
      element.scrollHeight - element.clientHeight - element.scrollTop,
    scrollHeight: element.scrollHeight,
    scrollTop: element.scrollTop,
  }));
}

test("Chat follow mode respects reading intent and resumes at the bottom", async ({
  page,
}) => {
  test.setTimeout(40_000);
  const chat = await startIsolatedChat(page);
  const messages = chat.locator(".messages");
  const tallSeed = [
    "E2E_STREAM Build enough real layout for scroll ownership.",
    ...Array.from(
      { length: 45 },
      (_, index) => `Seed line ${index + 1}: preserved viewport content.`,
    ),
  ].join("\n");

  await sendChatMessage(chat, tallSeed);
  await expect(
    chat.getByText("Fake provider streaming response.", { exact: true }),
  ).toBeVisible();
  await expect
    .poll(async () => (await scrollMetrics(messages)).scrollHeight)
    .toBeGreaterThan((await scrollMetrics(messages)).clientHeight);

  await sendChatMessage(chat, "E2E_SLOW Keep growing while I read above.");
  await expect(chat.getByText(/Slow response started\./)).toBeVisible();
  await messages.hover();
  await page.mouse.wheel(0, -650);
  await expect
    .poll(async () => (await scrollMetrics(messages)).distanceFromBottom)
    .toBeGreaterThan(100);

  const readingTop = (await scrollMetrics(messages)).scrollTop;
  await page.waitForTimeout(1_200);
  const afterGrowth = await scrollMetrics(messages);
  expect(Math.abs(afterGrowth.scrollTop - readingTop)).toBeLessThan(8);

  await messages.hover();
  await page.mouse.wheel(0, 10_000);
  await expect
    .poll(async () => (await scrollMetrics(messages)).distanceFromBottom)
    .toBeLessThanOrEqual(2);
  await page.waitForTimeout(1_200);
  await expect
    .poll(async () => (await scrollMetrics(messages)).distanceFromBottom)
    .toBeLessThanOrEqual(2);

  await expect(
    chat.getByRole("button", { exact: true, name: "New session" }),
  ).toBeEnabled({ timeout: 25_000 });
  await expect(chat.getByText(/Slow response started\./)).toBeVisible();
  await expect
    .poll(async () => (await scrollMetrics(messages)).distanceFromBottom)
    .toBeLessThanOrEqual(2);
});
