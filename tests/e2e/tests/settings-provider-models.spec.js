import { expect, test } from "@playwright/test";

test("Settings exposes the fake Provider connections and seeded Model bindings", async ({
  page,
}) => {
  await page.goto("/#settings");
  const settings = page.getByRole("region", { name: "Settings" });

  await settings
    .getByRole("button", { exact: true, name: "Providers" })
    .click();
  const providers = settings.getByRole("region", { name: "Providers" });
  await expect(
    providers.getByText("Fake Provider", { exact: true }),
  ).toBeVisible();
  const fakeProvider = providers
    .locator(".s-provider-card")
    .filter({ hasText: /^Fake Provider\b/ });
  await fakeProvider.getByRole("button", { name: "Details for fake" }).click();
  await expect(
    fakeProvider.getByText("Default", { exact: true }),
  ).toBeVisible();
  await expect(
    fakeProvider.getByRole("button", {
      name: "Disable connection fake:default",
    }),
  ).toBeVisible();

  await settings
    .getByRole("button", { exact: true, name: "Agent defaults" })
    .click();
  const defaults = settings.getByRole("region", { name: "Agent defaults" });
  await expect(
    defaults.getByRole("button", { exact: true, name: "Model" }),
  ).toContainText("fake/e2e-primary");
  await expect(
    defaults.getByRole("button", { exact: true, name: "Fallback model" }),
  ).toContainText("fake/e2e-fallback");

  await settings
    .getByRole("button", { exact: true, name: "Specialized Models" })
    .click();
  const specialized = settings.getByRole("region", {
    name: "Specialized Models",
  });
  await expect(
    specialized.getByRole("button", { exact: true, name: "Text to speech" }),
  ).toContainText("Fake Text to Speech");
  await expect(
    specialized.getByRole("button", {
      exact: true,
      name: "Image generation",
    }),
  ).toContainText("Fake Image");
});
