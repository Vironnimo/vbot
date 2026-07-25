import { readFileSync } from "node:fs";
import path from "node:path";

import { expect, test } from "@playwright/test";

import { environment } from "../environment.js";
import { sendChatMessage, startIsolatedChat } from "./chat-run-support.js";

const PROVIDER_ID = "e2e-custom";
const PROVIDER_NAME = "E2E Dynamic Provider";
const UPDATED_PROVIDER_NAME = "E2E Dynamic Provider Updated";
const MODEL_ID = "e2e-live";
const MODEL_NAME = "E2E Live Model";
const UPDATED_MODEL_NAME = "E2E Live Model Updated";
const MODEL_TARGET = `${PROVIDER_ID}/${MODEL_ID}::default`;
const API_KEY = "e2e-secret-key-9412";

async function rpc(request, method, params = {}) {
  const response = await request.post("/api/rpc", {
    data: { method, params },
  });
  const payload = await response.json();
  if (!response.ok() || payload?.ok !== true) {
    throw new Error(`RPC ${method} failed: ${JSON.stringify(payload)}`);
  }
  return payload.result;
}

async function openProviders(page) {
  await page.goto("/#settings");
  const settings = page.getByRole("region", { name: "Settings" });
  await settings
    .getByRole("button", { exact: true, name: "Providers" })
    .click();
  return settings.getByRole("region", { name: "Providers" });
}

function customProviderCard(providers, name) {
  return providers
    .locator(".s-provider-card")
    .filter({ hasText: new RegExp(`^${name}\\b`) });
}

function waitForRpcResponse(page, method) {
  return page.waitForResponse((response) => {
    if (
      !response.url().endsWith("/api/rpc") ||
      response.request().method() !== "POST"
    ) {
      return false;
    }
    return response.request().postDataJSON()?.method === method;
  });
}

async function expectSecretFreeResponse(response) {
  expect(response.ok()).toBe(true);
  const payload = await response.json();
  expect(payload?.ok).toBe(true);
  expect(JSON.stringify(payload)).not.toContain(API_KEY);
}

test("Custom Provider form rejects invalid ids and endpoint URLs", async ({
  page,
}) => {
  const providers = await openProviders(page);
  await providers.getByRole("button", { name: "Add custom" }).click();

  const dialog = page.getByRole("dialog", { name: "Add Custom Provider" });
  await dialog.getByLabel("Provider id").fill("Invalid ID");
  await dialog.getByLabel("Name").fill("Invalid Provider");
  await dialog.getByLabel("Endpoint URL").fill("relative-endpoint");
  await dialog.getByRole("button", { exact: true, name: "Save" }).click();
  await expect(dialog.getByRole("alert")).toHaveText(
    "Provider id must use lowercase letters and digits in hyphen-separated segments.",
  );

  await dialog.getByLabel("Provider id").fill("e2e-invalid");
  await dialog.getByRole("button", { exact: true, name: "Save" }).click();
  await expect(dialog.getByRole("alert")).toHaveText(
    "Enter an absolute HTTP(S) endpoint URL.",
  );

  await dialog.getByRole("button", { exact: true, name: "Cancel" }).click();
  await expect(dialog).toHaveCount(0);
});

test("a Custom Provider and manual Model work live and keep their key secret", async ({
  page,
  request,
}) => {
  try {
    let providers = await openProviders(page);
    await providers.getByRole("button", { name: "Add custom" }).click();

    let dialog = page.getByRole("dialog", { name: "Add Custom Provider" });
    await dialog.getByLabel("Provider id").fill(PROVIDER_ID);
    await dialog.getByLabel("Name").fill(PROVIDER_NAME);
    await dialog
      .getByLabel("Endpoint URL")
      .fill(
        `http://${environment.fakeProviderHost}:${environment.providerPort}/v1`,
      );
    await dialog.getByLabel("API key (optional)").fill(API_KEY);
    await dialog.getByLabel("Model discovery path").fill("");
    await dialog.getByRole("button", { name: "Add Model" }).click();
    await dialog.getByLabel("Wire id").fill(MODEL_ID);
    await dialog.getByLabel("Display name").fill(MODEL_NAME);
    await dialog.getByLabel("Context window").fill("65536");
    await dialog.getByLabel("Max output tokens").fill("2048");
    await dialog.getByLabel("Task types").fill("chat, text_output");
    await dialog.getByLabel("Supported parameters").fill("temperature");
    await expect(
      dialog.getByRole("switch", { name: `Tools for ${MODEL_ID}` }),
    ).toBeChecked();
    await dialog
      .getByRole("switch", { name: `JSON mode for ${MODEL_ID}` })
      .click();
    await dialog
      .getByRole("switch", { name: `Reasoning for ${MODEL_ID}` })
      .click();

    const saveResponse = waitForRpcResponse(page, "provider.custom_save");
    await dialog.getByRole("button", { exact: true, name: "Save" }).click();
    await expectSecretFreeResponse(await saveResponse);
    await expect(
      page.getByText("Custom Provider saved.", { exact: true }),
    ).toBeVisible();

    let providerCard = customProviderCard(providers, PROVIDER_NAME);
    await expect(providerCard).toBeVisible();
    await providerCard
      .getByRole("button", { name: `Details for ${PROVIDER_ID}` })
      .click();
    await expect(
      providerCard.getByRole("button", {
        name: `Disable connection ${PROVIDER_ID}:default`,
      }),
    ).toBeVisible();

    const customProviders = await rpc(request, "provider.custom_list");
    expect(JSON.stringify(customProviders)).not.toContain(API_KEY);
    const settings = JSON.parse(
      readFileSync(path.join(environment.dataDir, "settings.json"), "utf8"),
    );
    expect(settings.providers.custom[PROVIDER_ID]).toMatchObject({
      adapter: "openai_compatible",
      auth: "api_key",
      base_url: `http://${environment.fakeProviderHost}:${environment.providerPort}/v1`,
      models_endpoint: null,
    });
    expect(JSON.stringify(settings)).not.toContain(API_KEY);
    const environmentFile = readFileSync(
      path.join(environment.dataDir, ".env"),
      "utf8",
    );
    expect(environmentFile).toContain("VBOT_CUSTOM_E2E_CUSTOM_API_KEY");
    expect(environmentFile).toContain(API_KEY);

    let modelList = await rpc(request, "model.list");
    let liveModel = modelList.models.find(
      (model) => model.id === `${PROVIDER_ID}/${MODEL_ID}`,
    );
    expect(liveModel).toMatchObject({
      name: MODEL_NAME,
      context_window: 65536,
      max_output_tokens: 2048,
      connections: ["default"],
      capabilities: {
        tools: true,
        json_mode: true,
        reasoning: { supported: true },
        input_modalities: ["text"],
        output_modalities: ["text"],
        supported_parameters: ["temperature"],
        task_types: ["chat", "text_output"],
      },
    });

    let chat = await startIsolatedChat(page, { agentName: "Main" });
    await sendChatMessage(chat, `/model ${MODEL_TARGET}`);
    await expect(
      chat.getByText(`Model set to ${MODEL_TARGET}.`, { exact: true }),
    ).toBeVisible();
    await sendChatMessage(
      chat,
      "E2E_CUSTOM_PROVIDER_LIVE Exercise the newly created Provider",
    );
    await expect(
      chat.getByText("Dynamic Custom Provider response.", { exact: true }),
    ).toBeVisible();

    providers = await openProviders(page);
    providerCard = customProviderCard(providers, PROVIDER_NAME);
    await providerCard
      .getByRole("button", { name: `Details for ${PROVIDER_ID}` })
      .click();
    await providerCard
      .getByRole("button", { exact: true, name: "Edit" })
      .click();

    dialog = page.getByRole("dialog", { name: "Edit Custom Provider" });
    await expect(dialog.getByLabel("Provider id")).toBeDisabled();
    await expect(dialog.getByLabel("Replace API key (optional)")).toBeEmpty();
    await dialog.getByLabel("Name").fill(UPDATED_PROVIDER_NAME);
    await dialog.getByLabel("Display name").fill(UPDATED_MODEL_NAME);

    const updateResponse = waitForRpcResponse(page, "provider.custom_save");
    await dialog.getByRole("button", { exact: true, name: "Save" }).click();
    await expectSecretFreeResponse(await updateResponse);
    await expect(
      customProviderCard(providers, UPDATED_PROVIDER_NAME),
    ).toBeVisible();

    modelList = await rpc(request, "model.list");
    liveModel = modelList.models.find(
      (model) => model.id === `${PROVIDER_ID}/${MODEL_ID}`,
    );
    expect(liveModel?.name).toBe(UPDATED_MODEL_NAME);

    chat = await startIsolatedChat(page, { agentName: "Main" });
    await sendChatMessage(chat, "/status");
    await expect(
      chat.getByRole("note", { name: "Command output" }).last(),
    ).toContainText(`Model display name: ${UPDATED_MODEL_NAME}`);
    await sendChatMessage(
      chat,
      "E2E_CUSTOM_PROVIDER_LIVE Confirm the edited Provider remains active",
    );
    await expect(
      chat.getByText("Dynamic Custom Provider response.", { exact: true }),
    ).toBeVisible();
    await sendChatMessage(chat, "/model reset");
    await expect(chat.getByText("Model reset.", { exact: true })).toBeVisible();

    providers = await openProviders(page);
    providerCard = customProviderCard(providers, UPDATED_PROVIDER_NAME);
    await providerCard
      .getByRole("button", { name: `Details for ${PROVIDER_ID}` })
      .click();
    await providerCard
      .getByRole("button", { exact: true, name: "Delete" })
      .click();
    const deleteDialog = page.getByRole("dialog", {
      name: "Delete Custom Provider?",
    });
    await expect(deleteDialog).toContainText(
      "Existing Model references are kept and become unavailable.",
    );
    const deleteResponse = waitForRpcResponse(page, "provider.custom_delete");
    await deleteDialog
      .getByRole("button", { exact: true, name: "Delete" })
      .click();
    await expectSecretFreeResponse(await deleteResponse);
    await expect(
      page.getByText("Custom Provider deleted.", { exact: true }),
    ).toBeVisible();
    await expect(providerCard).toHaveCount(0);

    const settingsAfterDelete = JSON.parse(
      readFileSync(path.join(environment.dataDir, "settings.json"), "utf8"),
    );
    expect(settingsAfterDelete.providers.custom[PROVIDER_ID]).toBeUndefined();
    const environmentAfterDelete = readFileSync(
      path.join(environment.dataDir, ".env"),
      "utf8",
    );
    expect(environmentAfterDelete).not.toContain(
      "VBOT_CUSTOM_E2E_CUSTOM_API_KEY",
    );
    expect(environmentAfterDelete).not.toContain(API_KEY);
  } finally {
    await rpc(request, "agent.update", { id: "main", model: "" });
    const remainingProviders = await rpc(request, "provider.custom_list");
    if (
      remainingProviders.providers.some(
        (provider) => provider.id === PROVIDER_ID,
      )
    ) {
      await rpc(request, "provider.custom_delete", {
        provider_id: PROVIDER_ID,
      });
    }
  }
});
