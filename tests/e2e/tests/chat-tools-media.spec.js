import { expect, test } from "@playwright/test";

import { startIsolatedChat } from "./chat-run-support.js";
import {
  expectToolSucceeded,
  openToolRow,
  runToolScenario,
} from "./chat-tool-support.js";

test("speech and image Tools persist and serve fake Provider artifacts", async ({
  page,
}) => {
  const chat = await startIsolatedChat(page);

  await runToolScenario(chat, {
    prompt: "E2E_TOOL_MEDIA Generate deterministic local media artifacts",
    finalText: "Generated media tools completed.",
  });

  const speech = await expectToolSucceeded(page, chat, "text_to_speech");
  await openToolRow(speech);
  await expect(speech).toContainText("E2E synthesized speech");
  const audio = chat.locator("audio.speech-audio-player");
  await expect(audio).toHaveCount(1);
  const audioUrl = await audio.getAttribute("src");
  expect(audioUrl).toMatch(/^\/api\/speech\/artifacts\/[a-f0-9]{32}$/);
  const audioResponse = await page.request.get(audioUrl);
  expect(audioResponse.ok()).toBe(true);
  expect(audioResponse.headers()["content-type"]).toContain("audio/wav");
  expect((await audioResponse.body()).length).toBeGreaterThan(44);

  const generation = await expectToolSucceeded(page, chat, "image_generation");
  await openToolRow(generation);
  await expect(generation).toContainText(
    "A deterministic blue square on a white background",
  );
  const image = chat.getByRole("img", { name: /^[a-f0-9]{32}\.png$/ });
  await expect(image).toBeVisible();
  await expect.poll(() => image.evaluate((node) => node.naturalWidth)).toBe(1);
  const imageUrl = await image.getAttribute("src");
  expect(imageUrl).toMatch(/^\/api\/files\/[^\s]+$/);
  const imageResponse = await page.request.get(imageUrl);
  expect(imageResponse.ok()).toBe(true);
  expect(imageResponse.headers()["content-type"]).toContain("image/png");
});
