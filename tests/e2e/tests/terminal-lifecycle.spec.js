import { spawnSync } from "node:child_process";
import { userInfo } from "node:os";

import { expect, test } from "@playwright/test";

import { environment } from "../environment.js";

const DEFAULT_SHELL_MARKER = "VBOT-DEFAULT-SHELL-READY";

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

async function cleanupTerminals(request) {
  const result = await rpc(request, "terminal.list");
  await Promise.all(
    result.terminals
      .filter((terminal) => !["exited", "error"].includes(terminal.state))
      .map((terminal) =>
        rpc(request, "terminal.kill", { terminal_id: terminal.terminal_id }),
      ),
  );
  const retained = await rpc(request, "terminal.list");
  await Promise.all(
    retained.terminals.map((terminal) =>
      rpc(request, "terminal.forget", { terminal_id: terminal.terminal_id }),
    ),
  );
}

async function openTerminals(page) {
  await page.goto("/#terminals");
  await expect(page.getByLabel("Connected")).toBeVisible();
  await expect(
    page.getByRole("main").getByRole("region", { name: "Terminals" }),
  ).toBeVisible();
}

async function startTerminal(page, { command, arguments: args = [] } = {}) {
  await page
    .getByRole("region", { name: "Terminals" })
    .getByRole("button", { exact: true, name: "New terminal" })
    .click();
  const dialog = page.getByRole("dialog", { name: "New terminal" });
  await expect(dialog).toBeVisible();
  if (command) {
    await dialog.locator("#terminal-start-command").fill(command);
  }
  if (args.length > 0) {
    await dialog.locator("#terminal-start-arguments").fill(args.join("\n"));
  }
  await dialog.getByRole("button", { name: "Start terminal" }).click();
  await expect(dialog).not.toBeVisible();
  await expect(
    selectedTerminal(page).getByRole("group", { name: /^Live terminal/ }),
  ).toBeVisible();
}

function selectedTerminal(page) {
  return page.locator(".terminals-view__tile--focused");
}

async function readSelectedTerminalSnapshot(page) {
  const terminalId = await selectedTerminal(page).getAttribute("data-terminal-id");
  if (!terminalId) {
    throw new Error("No selected terminal is available");
  }
  return page.evaluate(
    ({ terminalId }) =>
      new Promise((resolve, reject) => {
        const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
        const socket = new WebSocket(
          `${protocol}//${window.location.host}/ws/terminals/${encodeURIComponent(terminalId)}`,
        );
        const timeout = window.setTimeout(() => {
          socket.close();
          reject(new Error("Timed out waiting for a terminal snapshot"));
        }, 3000);
        socket.onmessage = (event) => {
          const payload = JSON.parse(event.data);
          if (typeof payload.ansi !== "string") {
            return;
          }
          window.clearTimeout(timeout);
          socket.close();
          resolve(payload.ansi);
        };
        socket.onerror = () => {
          window.clearTimeout(timeout);
          reject(new Error("Terminal snapshot WebSocket failed"));
        };
      }),
    { terminalId },
  );
}

async function expectSelectedTerminalOutput(page, text) {
  await expect
    .poll(async () => (await readSelectedTerminalSnapshot(page)).includes(text))
    .toBe(true);
}

async function expectSelectedTerminalWithoutOutput(page, text) {
  await expect
    .poll(async () => (await readSelectedTerminalSnapshot(page)).includes(text))
    .toBe(false);
}

async function sendToSelectedTerminal(page, text) {
  await selectedTerminal(page)
    .getByRole("group", { name: /^Live terminal/ })
    .click();
  await page.keyboard.type(text);
  await page.keyboard.press("Enter");
}

async function closeSelectedTerminal(page) {
  const tile = selectedTerminal(page);
  const terminalId = await tile.getAttribute("data-terminal-id");
  if (!terminalId) {
    throw new Error("No selected terminal is available");
  }
  const rpcResponse = (method) =>
    page.waitForResponse((response) => {
      if (
        !response.url().endsWith("/api/rpc") ||
        response.request().method() !== "POST"
      ) {
        return false;
      }
      const body = response.request().postDataJSON();
      return (
        body?.method === method && body?.params?.terminal_id === terminalId
      );
    });
  const killResponse = rpcResponse("terminal.kill");
  const forgetResponse = rpcResponse("terminal.forget");
  const expectSuccessfulRpc = async (responsePromise) => {
    const response = await responsePromise;
    expect(response.ok()).toBe(true);
    await expect(response.json()).resolves.toMatchObject({ ok: true });
  };
  await tile.getByRole("button", { name: "Close terminal" }).click();
  await expect(
    page.locator(`[data-terminal-id="${terminalId}"]`),
  ).toHaveCount(0);
  await expectSuccessfulRpc(killResponse);
  await expectSuccessfulRpc(forgetResponse);
}

function pythonHarness(label, backgroundDelaySeconds = null) {
  const setTitle = `lambda value:(sys.stdout.write('\\x1b]0;'+value+'\\x07'),sys.stdout.flush())`;
  const background =
    backgroundDelaySeconds === null
      ? ""
      : `threading.Timer(${backgroundDelaySeconds},lambda:(set_title('E2E-${label} active'),print('BACKGROUND-${label}',flush=True))).start();`;
  return `import sys,threading;set_title=${setTitle};set_title('E2E-${label}');print('READY-${label}',flush=True);${background}[print('${label}:'+line.rstrip(),flush=True) for line in sys.stdin]`;
}

function windowsCommandExists(command) {
  return (
    spawnSync("where.exe", [command], {
      encoding: "utf8",
      windowsHide: true,
    }).status === 0
  );
}

function expectedDefaultShell() {
  if (process.platform !== "win32") {
    return process.env.SHELL || userInfo().shell || "/bin/sh";
  }
  if (windowsCommandExists("pwsh.exe")) {
    return "pwsh.exe";
  }
  if (windowsCommandExists("powershell.exe")) {
    return "powershell.exe";
  }
  return process.env.COMSPEC || "cmd.exe";
}

function executableName(command) {
  return command.split(/[\\/]/).pop();
}

function expectedShellTitle(command) {
  const executable = executableName(command).toLowerCase();
  if (executable === "pwsh.exe" || executable === "pwsh") {
    return "PowerShell";
  }
  if (executable === "powershell.exe" || executable === "powershell") {
    return "Windows PowerShell";
  }
  if (executable === "cmd.exe" || executable === "cmd") {
    return "Command Prompt";
  }
  if (executable === "bash.exe" || executable === "bash") {
    return "Bash";
  }
  if (executable === "zsh.exe" || executable === "zsh") {
    return "Zsh";
  }
  if (executable === "fish.exe" || executable === "fish") {
    return "Fish";
  }
  return executableName(command);
}

function defaultShellProbe(command) {
  if (process.platform !== "win32") {
    return `printf '${DEFAULT_SHELL_MARKER}\\n'`;
  }
  return command.toLowerCase().endsWith("cmd.exe")
    ? `echo ${DEFAULT_SHELL_MARKER}`
    : `Write-Output ${DEFAULT_SHELL_MARKER}`;
}

test.beforeEach(async ({ request }) => {
  await cleanupTerminals(request);
});

test.afterEach(async ({ request }) => {
  await cleanupTerminals(request);
});

test("the platform default shell starts as a native interactive terminal", async ({
  page,
}) => {
  await openTerminals(page);
  await startTerminal(page);

  const expectedCommand = expectedDefaultShell();
  await expect(
    page.locator(".terminals-view__tile-title").first(),
  ).toHaveText(expectedShellTitle(expectedCommand));
  await sendToSelectedTerminal(page, defaultShellProbe(expectedCommand));
  await expectSelectedTerminalOutput(page, DEFAULT_SHELL_MARKER);
});

test("a started command runs inside the shell and the terminal stays usable", async ({
  page,
}) => {
  await openTerminals(page);
  await startTerminal(page, {
    command: environment.python,
    arguments: ["-u", "-c", pythonHarness("INNER")],
  });
  await expectSelectedTerminalOutput(page, "READY-INNER");
  await expect(
    page.locator(".terminals-view__tile-target").first(),
  ).toHaveText("Manual");

  await sendToSelectedTerminal(page, "message-for-inner");
  await expectSelectedTerminalOutput(page, "INNER:message-for-inner");

  await page.keyboard.press("Control+c");
  await page.waitForTimeout(400);
  await sendToSelectedTerminal(page, defaultShellProbe(expectedDefaultShell()));
  await expectSelectedTerminalOutput(page, DEFAULT_SHELL_MARKER);
});

test("multiple terminals keep output, input, reconnect, and stop lifecycle isolated", async ({
  page,
}) => {
  await openTerminals(page);
  const terminalItems = page.locator(".terminals-view__tile-bar");

  await startTerminal(page, {
    command: environment.python,
    arguments: ["-u", "-c", pythonHarness("ALPHA", 1)],
  });
  await expect(terminalItems).toHaveCount(1);
  await expect(terminalItems.nth(0)).toContainText("E2E-ALPHA");
  await expectSelectedTerminalOutput(page, "READY-ALPHA");

  await startTerminal(page, {
    command: environment.python,
    arguments: ["-u", "-c", pythonHarness("BRAVO")],
  });
  await expect(terminalItems).toHaveCount(2);
  await expect(
    page.locator(".terminals-view__tile--focused .terminals-view__tile-title"),
  ).toHaveText("E2E-BRAVO");
  await expect(terminalItems.nth(0)).toContainText("E2E-BRAVO");
  await expectSelectedTerminalOutput(page, "READY-BRAVO");
  await sendToSelectedTerminal(page, "message-for-bravo");
  await expectSelectedTerminalOutput(page, "BRAVO:message-for-bravo");

  await terminalItems.nth(1).click();
  await expect(terminalItems.nth(1)).toContainText("E2E-ALPHA active");
  await expectSelectedTerminalOutput(page, "BACKGROUND-ALPHA");
  await expectSelectedTerminalWithoutOutput(page, "BRAVO:message-for-bravo");
  await sendToSelectedTerminal(page, "message-for-alpha");
  await expectSelectedTerminalOutput(page, "ALPHA:message-for-alpha");

  await terminalItems.nth(0).click();
  await expectSelectedTerminalOutput(page, "BRAVO:message-for-bravo");
  await expectSelectedTerminalWithoutOutput(page, "ALPHA:message-for-alpha");

  const navigation = page.getByRole("navigation", { name: "Sections" });
  await navigation.getByRole("button", { name: "Projects" }).click();
  await navigation.getByRole("button", { name: "Terminals" }).click();
  await expect(terminalItems).toHaveCount(2);
  await expectSelectedTerminalOutput(page, "BRAVO:message-for-bravo");

  await closeSelectedTerminal(page);
  await expect(terminalItems).toHaveCount(1);
  await expectSelectedTerminalOutput(page, "ALPHA:message-for-alpha");
  await sendToSelectedTerminal(page, "alpha-survived-bravo-stop");
  await expectSelectedTerminalOutput(page, "ALPHA:alpha-survived-bravo-stop");

  await closeSelectedTerminal(page);
  await expect(terminalItems).toHaveCount(0);
});
