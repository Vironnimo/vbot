import http from "node:http";

const SERVICE_NAME = "vbot-e2e-fake-provider";
const host = process.env.VBOT_E2E_PROVIDER_HOST ?? "127.0.0.1";
const port = Number.parseInt(process.env.VBOT_E2E_PROVIDER_PORT ?? "8438", 10);
const activeResponses = new Set();
let nextToolCallId = 1;
const TINY_PNG_BASE64 =
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=";

if (!Number.isInteger(port) || port < 1 || port > 65_535) {
  throw new Error("VBOT_E2E_PROVIDER_PORT must be a valid TCP port");
}

function writeJson(response, statusCode, body, headers = {}) {
  response.writeHead(statusCode, {
    "content-type": "application/json; charset=utf-8",
    ...headers,
  });
  response.end(JSON.stringify(body));
}

function tinyMp3() {
  // Silent MPEG-1 Layer III frames (128 kbit/s, 44.1 kHz, mono, no CRC):
  // each frame is its 4-byte header followed by zeroed side information and
  // main data, which decodes as silence.
  const frameHeader = 0xff_fb_90_c0;
  const frameLength = 417;
  const frames = 8;
  const bytes = Buffer.alloc(frameLength * frames);
  for (let frame = 0; frame < frames; frame += 1) {
    bytes.writeUInt32BE(frameHeader, frame * frameLength);
  }
  return bytes;
}

function contentText(content) {
  if (typeof content === "string") {
    return content;
  }
  if (!Array.isArray(content)) {
    return "";
  }
  return content
    .map((part) => {
      if (typeof part === "string") {
        return part;
      }
      if (part && typeof part.text === "string") {
        return part.text;
      }
      return "";
    })
    .join("");
}

function latestUserText(messages) {
  if (!Array.isArray(messages)) {
    return "";
  }
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index];
    if (message?.role === "user") {
      return contentText(message.content);
    }
  }
  return "";
}

function messagesText(messages, role = null) {
  if (!Array.isArray(messages)) {
    return "";
  }
  return messages
    .filter((message) => role === null || message?.role === role)
    .map((message) => contentText(message?.content))
    .join("\n");
}

function parseJsonValue(value) {
  if (typeof value !== "string") {
    return value;
  }
  try {
    return JSON.parse(value);
  } catch {
    return value;
  }
}

function toolResults(messages) {
  if (!Array.isArray(messages)) {
    return [];
  }

  const toolNamesById = new Map();
  for (const message of messages) {
    if (message?.role !== "assistant" || !Array.isArray(message.tool_calls)) {
      continue;
    }
    for (const toolCall of message.tool_calls) {
      const name = toolCall?.function?.name;
      if (typeof toolCall?.id === "string" && typeof name === "string") {
        toolNamesById.set(toolCall.id, name);
      }
    }
  }

  return messages.flatMap((message) => {
    if (message?.role !== "tool") {
      return [];
    }
    return [
      {
        envelope: parseJsonValue(contentText(message.content)),
        name: toolNamesById.get(message.tool_call_id) ?? "",
      },
    ];
  });
}

function resultsFor(results, name) {
  return results.filter((result) => result.name === name);
}

function toolCall(name, args) {
  return { args, name };
}

function addFileCall(path, content) {
  return toolCall("apply_patch", {
    patch: `*** Begin Patch\n*** Add File: ${path}\n${content
      .replace(/\n$/, "")
      .split("\n")
      .map((line) => `+${line}`)
      .join("\n")}\n*** End Patch`,
  });
}

function skillManageCall(action, args) {
  return toolCall("skill_manage", { action, ...args });
}

function plannedToolResponse(prompt, results, offeredTools) {
  if (
    prompt.includes("E2E_HTML_PREVIEW") ||
    prompt.includes("Create a small website for the preview demo.")
  ) {
    const written = resultsFor(results, "apply_patch")
      .flatMap((result) => result.envelope?.data?.files ?? [])
      .filter((file) => file.path.includes("preview-demo-"));
    if (written.length > 0) {
      const entry = written.find((file) => file.path.endsWith("/index.html"));
      return {
        text: entry
          ? `Your website is ready. Open it here:\n\nfile:${entry.path}`
          : "The website files could not be completed.",
      };
    }
    const directory = `preview-demo-${Date.now()}`;
    const files = {
      "index.html":
        '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Studio Notes</title><link rel="stylesheet" href="style.css"><script src="main.js" defer></script><body><nav>STUDIO NOTES <a href="pages/about.html">About this website →</a></nav><main><p class="eyebrow">CREATED BY YOUR AGENT</p><h1>Made in this<br>conversation.</h1><p>A small website, ready to explore. Opened directly from the file in your Chat.</p><img src="landscape.svg" alt="A quiet green landscape"><section><h2>Try something.</h2><p>This interaction comes from a separate JavaScript file.</p><button id="counter">Try it · 0</button></section><a href="pages/about.html">Visit the second page →</a></main></body></html>',
      "style.css":
        "body{margin:0;background:#f2f0e8;color:#203d36;font:16px/1.6 system-ui,sans-serif}nav{display:flex;justify-content:space-between;gap:20px;padding:25px 32px;border-bottom:1px solid #d6d8cb;font-size:12px;font-weight:600}main{max-width:740px;padding:36px 32px;margin:auto}h1{font:normal clamp(36px,5vw,56px)/1.1 Georgia,serif;letter-spacing:-1.5px}h2{font:normal 26px Georgia,serif}a{color:inherit}.eyebrow{font-size:11px;letter-spacing:2px}img{display:block;width:100%;margin:30px 0;border-radius:8px}section{border:1px solid #d6d8cb;border-radius:8px;padding:20px;margin-bottom:24px}button{background:#426749;color:white;border:0;border-radius:5px;padding:12px 20px;cursor:pointer}",
      "main.js":
        'let count=0;document.querySelector("#counter").addEventListener("click",event=>{event.currentTarget.textContent=`Try it · ${++count}`});',
      "landscape.svg":
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 800 220"><rect width="800" height="220" fill="#dce3ba"/><circle cx="620" cy="90" r="45" fill="#fcf8d8"/><path d="M0 135Q200 60 400 130T800 130V220H0Z" fill="#7b9161"/><path d="M0 120Q220 190 410 140T800 170V220H0Z" fill="#426749"/></svg>',
      "pages/about.html":
        '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>About · Studio Notes</title><link rel="stylesheet" href="../style.css"><body><nav>STUDIO NOTES <a href="../index.html">← Back home</a></nav><main><p class="eyebrow">A SECOND PAGE</p><h1>Room for<br>the details.</h1><p id="live-copy">This page belongs to the same website.</p><img src="../landscape.svg" alt="A quiet green landscape"><section><h2>Still connected.</h2><p>Changes to the website files refresh this page automatically.</p></section></main></body></html>',
    };
    return {
      calls: Object.entries(files).map(([name, content]) =>
        addFileCall(`${directory}/${name}`, content),
      ),
    };
  }

  if (prompt.includes("E2E_MEMORY_PROMPT_SEED")) {
    if (resultsFor(results, "memory").length === 0) {
      return {
        calls: [
          toolCall("memory", {
            action: "add",
            scope: "agent",
            content: "E2E pinned memory marker 2741",
          }),
        ],
      };
    }
    return { text: "Memory prompt seed stored." };
  }

  if (prompt.includes("E2E_LEARN_COMMAND")) {
    const manageResults = resultsFor(results, "skill_manage");
    if (manageResults.length === 0) {
      return {
        calls: [
          skillManageCall("create", {
            name: "e2e-learned-command",
            content:
              "---\nname: e2e-learned-command\ndescription: Temporary Skill authored by the slash-command E2E scenario.\n---\n\n# Learned Command Skill\n\nDeterministic slash-command evidence.\n",
          }),
        ],
      };
    }
    return { text: "Created private Skill e2e-learned-command." };
  }

  if (prompt.includes("E2E_CLEAN_LEARNED_COMMAND")) {
    if (resultsFor(results, "skill_manage").length === 0) {
      return {
        calls: [
          skillManageCall("delete", {
            name: "e2e-learned-command",
          }),
        ],
      };
    }
    return { text: "Learned command Skill cleaned up." };
  }

  if (prompt.includes("E2E_TOOL_CATALOG")) {
    const catalogIsRestricted =
      offeredTools.includes("status") &&
      !offeredTools.includes("bash") &&
      !offeredTools.includes("apply_patch") &&
      !offeredTools.includes("search_files") &&
      !offeredTools.includes("skill_manage");
    if (!catalogIsRestricted) {
      return { text: "Unexpected Tool catalog." };
    }
    if (resultsFor(results, "status").length === 0) {
      return { calls: [toolCall("status", {})] };
    }
    return { text: "Restricted Tool catalog verified." };
  }

  if (prompt.includes("E2E_TOOL_CANCEL")) {
    if (resultsFor(results, "bash").length === 0) {
      return {
        calls: [
          toolCall("bash", {
            mode: "foreground",
            command:
              "node -e \"console.log('e2e-tool-started'); setTimeout(() => console.log('e2e-tool-ended'), 15000)\"",
          }),
        ],
      };
    }
    return { text: "Tool cancellation handled." };
  }

  if (prompt.includes("E2E_TOOL_RUNTIME")) {
    if (resultsFor(results, "status").length === 0) {
      return { calls: [toolCall("status", {})] };
    }
    if (resultsFor(results, "bash").length === 0) {
      return {
        calls: [
          toolCall("bash", {
            mode: "foreground",
            command: "echo e2e-shell-output",
          }),
        ],
      };
    }
    if (resultsFor(results, "process").length === 0) {
      return { calls: [toolCall("process", { action: "status" })] };
    }
    return { text: "Runtime tools completed." };
  }

  if (prompt.includes("E2E_TOOL_MISSING_FILE")) {
    if (resultsFor(results, "read").length === 0) {
      return {
        calls: [toolCall("read", { path: "tool-e2e/missing.txt" })],
      };
    }
    return { text: "Missing file error handled." };
  }

  if (prompt.includes("E2E_TOOL_HISTORY")) {
    if (resultsFor(results, "history").length === 0) {
      return {
        calls: [
          toolCall("history", {
            action: "search",
            query: "Archived obsidian record 8642",
            match: "phrase",
            limit: 10,
          }),
        ],
      };
    }
    return { text: "Compacted Session history recovered." };
  }

  if (prompt.includes("E2E_TOOL_MEDIA")) {
    if (resultsFor(results, "text_to_speech").length === 0) {
      return {
        calls: [toolCall("text_to_speech", { text: "E2E synthesized speech" })],
      };
    }
    const imageResults = resultsFor(results, "image_generation");
    if (imageResults.length === 0) {
      return {
        calls: [
          toolCall("image_generation", {
            prompt: "A deterministic blue square on a white background",
          }),
        ],
      };
    }
    const imagePath = imageResults[0]?.envelope?.data?.images?.[0]?.path ?? "";
    return {
      text: `Generated media tools completed.\n\nfile:${imagePath}`,
    };
  }

  if (prompt.includes("E2E_TOOL_SUBAGENT")) {
    const subagentResults = resultsFor(results, "subagent");
    if (subagentResults.length === 0) {
      return {
        calls: [
          toolCall("subagent", {
            action: "run",
            agent_id: "e2e-worker",
            content: "E2E_SUBAGENT_CHILD Return the deterministic child result",
          }),
        ],
      };
    }
    return { text: "Sub-agent started; awaiting automatic delivery." };
  }

  if (
    prompt.includes("Automatic completion delivery") &&
    prompt.includes("Fake sub-agent result.")
  ) {
    return { text: "Sub-agent tool completed." };
  }

  return null;
}

function responseText(model, prompt, messages = []) {
  if (model.includes("e2e-fallback")) {
    return "Fallback provider response.";
  }
  if (model === "e2e-live" && prompt.includes("E2E_CUSTOM_PROVIDER_LIVE")) {
    return "Dynamic Custom Provider response.";
  }
  if (prompt.includes("E2E_PROMPT_SCOPE_CHECK")) {
    return messagesText(messages, "system").includes(
      "Agent-only E2E prompt 3307",
    )
      ? "Agent-scoped System Prompt reached the Provider."
      : "Agent-scoped System Prompt stayed out of the Provider prompt.";
  }
  if (
    messagesText(messages, "system").includes(
      "E2E custom provider context 5821",
    )
  ) {
    return "Custom System Prompt reached the Provider.";
  }
  if (prompt.includes("E2E_MEMORY_PROMPT_CHECK")) {
    return messagesText(messages, "system").includes(
      "E2E pinned memory marker 2741",
    )
      ? "Pinned Memory reached the Provider."
      : "Pinned Memory stayed out of the Provider prompt.";
  }
  if (prompt.includes("E2E_PROJECT_AGENT_CONTEXT")) {
    const systemPrompt = messagesText(messages, "system");
    if (systemPrompt.includes("E2E_OPEN_CODE_AGENT_PROMPT_4172")) {
      return "OpenCode Project Agent context reached the Provider.";
    }
    if (systemPrompt.includes("E2E_CLAUDE_AGENT_PROMPT_6385")) {
      return "Claude Project Agent context reached the Provider.";
    }
    return "Project Agent context was missing.";
  }
  if (
    prompt.includes("You are handing off this conversation to another agent")
  ) {
    return "E2E handoff brief 9182.";
  }
  if (prompt.includes("E2E handoff brief 9182")) {
    return "E2E handoff received by target Agent.";
  }
  if (
    prompt.includes("Review this Session") &&
    prompt.includes("E2E reflection focus")
  ) {
    return "E2E reflection completed.";
  }
  if (prompt.includes("E2E_QUEUE_FIRST")) {
    return "First queued response.";
  }
  if (prompt.includes("E2E_QUEUE_REMOVED")) {
    return "Removed queued message unexpectedly ran.";
  }
  if (prompt.includes("E2E_QUEUE_THIRD")) {
    return "Third queued response.";
  }
  if (prompt.includes("E2E_SCHEDULED_RUN")) {
    return "Scheduled Run delivered 4471.";
  }
  if (prompt.includes("E2E_STREAM")) {
    return "Fake provider streaming response.";
  }
  if (prompt.includes("E2E_HISTORY_SEED")) {
    return "Archived obsidian record 8642.";
  }
  if (prompt.includes("E2E_SUBAGENT_CHILD")) {
    return "Fake sub-agent result.";
  }
  if (prompt.includes("E2E_ATTACHMENT")) {
    return prompt.includes("attachment payload 7319")
      ? "Fake provider received the attachment content."
      : "Fake provider did not receive the attachment content.";
  }
  return "Fake provider response.";
}

function completionBody(model, text) {
  return {
    id: "chatcmpl-e2e",
    object: "chat.completion",
    created: Math.floor(Date.now() / 1_000),
    model,
    choices: [
      {
        index: 0,
        message: { role: "assistant", content: text },
        finish_reason: "stop",
      },
    ],
    usage: {
      prompt_tokens: 12,
      completion_tokens: Math.max(1, text.split(/\s+/).length),
      total_tokens: 12 + Math.max(1, text.split(/\s+/).length),
    },
  };
}

function writeStreamFrame(response, body) {
  response.write(`data: ${JSON.stringify(body)}\n\n`);
}

function wait(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

async function streamCompletion(response, model, chunks, delayMilliseconds) {
  response.writeHead(200, {
    "cache-control": "no-cache",
    connection: "keep-alive",
    "content-type": "text/event-stream; charset=utf-8",
  });
  response.flushHeaders();
  activeResponses.add(response);
  response.once("close", () => activeResponses.delete(response));

  for (const text of chunks) {
    if (response.destroyed || response.writableEnded) {
      return;
    }
    writeStreamFrame(response, {
      id: "chatcmpl-e2e",
      object: "chat.completion.chunk",
      created: Math.floor(Date.now() / 1_000),
      model,
      choices: [{ index: 0, delta: { content: text }, finish_reason: null }],
    });
    await wait(delayMilliseconds);
  }

  if (response.destroyed || response.writableEnded) {
    return;
  }
  writeStreamFrame(response, {
    id: "chatcmpl-e2e",
    object: "chat.completion.chunk",
    created: Math.floor(Date.now() / 1_000),
    model,
    choices: [{ index: 0, delta: {}, finish_reason: "stop" }],
    usage: {
      prompt_tokens: 12,
      completion_tokens: chunks.length,
      total_tokens: 12 + chunks.length,
    },
  });
  response.end("data: [DONE]\n\n");
}

function streamToolCalls(response, model, calls) {
  response.writeHead(200, {
    "cache-control": "no-cache",
    connection: "keep-alive",
    "content-type": "text/event-stream; charset=utf-8",
  });
  response.flushHeaders();
  activeResponses.add(response);
  response.once("close", () => activeResponses.delete(response));

  const wireCalls = calls.map((call, index) => ({
    index,
    id: `call_e2e_${nextToolCallId++}`,
    type: "function",
    function: {
      name: call.name,
      arguments: JSON.stringify(call.args),
    },
  }));
  writeStreamFrame(response, {
    id: "chatcmpl-e2e",
    object: "chat.completion.chunk",
    created: Math.floor(Date.now() / 1_000),
    model,
    choices: [
      {
        index: 0,
        delta: { tool_calls: wireCalls },
        finish_reason: null,
      },
    ],
  });
  writeStreamFrame(response, {
    id: "chatcmpl-e2e",
    object: "chat.completion.chunk",
    created: Math.floor(Date.now() / 1_000),
    model,
    choices: [{ index: 0, delta: {}, finish_reason: "tool_calls" }],
    usage: {
      prompt_tokens: 12,
      completion_tokens: calls.length,
      total_tokens: 12 + calls.length,
    },
  });
  response.end("data: [DONE]\n\n");
}

function readRequestJson(request) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    request.on("data", (chunk) => {
      size += chunk.length;
      if (size > 1_048_576) {
        reject(new Error("request body too large"));
        request.destroy();
        return;
      }
      chunks.push(chunk);
    });
    request.on("end", () => {
      try {
        resolve(JSON.parse(Buffer.concat(chunks).toString("utf8")));
      } catch (error) {
        reject(error);
      }
    });
    request.on("error", reject);
  });
}

async function handleChatCompletion(request, response) {
  let body;
  try {
    body = await readRequestJson(request);
  } catch {
    writeJson(response, 400, {
      error: { message: "Invalid JSON request", type: "invalid_request_error" },
    });
    return;
  }

  const model = typeof body?.model === "string" ? body.model : "e2e-primary";
  const prompt = latestUserText(body?.messages);
  const offeredTools = Array.isArray(body?.tools)
    ? body.tools
        .map((tool) => tool?.function?.name)
        .filter((name) => typeof name === "string")
    : [];
  const toolResponse = plannedToolResponse(
    prompt,
    toolResults(body?.messages),
    offeredTools,
  );

  const requestText = messagesText(body?.messages);
  if (
    requestText.includes("E2E_COMMAND_CONTINUE") &&
    requestText.includes("<continuation-checkpoint")
  ) {
    await streamCompletion(
      response,
      model,
      ["Continued interrupted work completed."],
      25,
    );
    return;
  }

  if (prompt.includes("E2E_ERROR")) {
    writeJson(response, 400, {
      error: {
        code: "e2e_failure",
        message: "E2E provider failure",
        type: "invalid_request_error",
      },
    });
    return;
  }

  if (model === "e2e-primary" && prompt.includes("E2E_FALLBACK")) {
    writeJson(
      response,
      429,
      {
        error: {
          code: "rate_limit",
          message: "E2E primary unavailable",
          type: "rate_limit_error",
        },
      },
      { "retry-after": "0" },
    );
    return;
  }

  if (body?.stream !== true) {
    writeJson(
      response,
      200,
      completionBody(model, responseText(model, prompt, body?.messages)),
    );
    return;
  }

  if (prompt.includes("E2E_SLOW")) {
    const chunks = [
      "Slow response started.",
      ...Array.from({ length: 80 }, () => " still running"),
    ];
    await streamCompletion(response, model, chunks, 200);
    return;
  }

  if (prompt.includes("E2E_QUEUE_ACTIVE")) {
    // Long enough for the browser to queue, edit, and remove messages before
    // the active Run finishes and the Queue starts draining.
    await streamCompletion(
      response,
      model,
      ["Queue run started.", " Still active.", " Almost finished.", " Done."],
      2_000,
    );
    return;
  }

  if (toolResponse?.calls) {
    streamToolCalls(response, model, toolResponse.calls);
    return;
  }

  const text =
    toolResponse?.text ?? responseText(model, prompt, body?.messages);
  const chunks = text.match(/\S+\s*/g) ?? [text];
  await streamCompletion(response, model, chunks, 75);
}

async function handleSpeech(request, response) {
  let body;
  try {
    body = await readRequestJson(request);
  } catch {
    writeJson(response, 400, {
      error: {
        message: "Invalid speech request",
        type: "invalid_request_error",
      },
    });
    return;
  }
  if (
    body?.model !== "e2e-tts" ||
    body?.input !== "E2E synthesized speech" ||
    body?.response_format !== "mp3"
  ) {
    writeJson(response, 400, {
      error: {
        message: "Unexpected speech request",
        type: "invalid_request_error",
      },
    });
    return;
  }
  const audio = tinyMp3();
  response.writeHead(200, {
    "content-length": String(audio.length),
    "content-type": "audio/mpeg",
    "x-generation-id": "speech-e2e",
  });
  response.end(audio);
}

async function handleImageGeneration(request, response) {
  let body;
  try {
    body = await readRequestJson(request);
  } catch {
    writeJson(response, 400, {
      error: {
        message: "Invalid image request",
        type: "invalid_request_error",
      },
    });
    return;
  }
  if (
    body?.model !== "e2e-image" ||
    body?.prompt !== "A deterministic blue square on a white background"
  ) {
    writeJson(response, 400, {
      error: {
        message: "Unexpected image request",
        type: "invalid_request_error",
      },
    });
    return;
  }
  writeJson(response, 200, {
    created: Math.floor(Date.now() / 1_000),
    data: [{ b64_json: TINY_PNG_BASE64 }],
  });
}

const server = http.createServer((request, response) => {
  const url = new URL(request.url ?? "/", `http://${host}:${port}`);

  if (request.method === "GET" && url.pathname === "/health") {
    writeJson(response, 200, { ok: true, service: SERVICE_NAME });
    return;
  }

  if (request.method === "GET" && url.pathname === "/v1/models") {
    writeJson(response, 200, {
      object: "list",
      data: [
        { id: "e2e-primary", object: "model", owned_by: SERVICE_NAME },
        { id: "e2e-fallback", object: "model", owned_by: SERVICE_NAME },
      ],
    });
    return;
  }

  if (request.method === "POST" && url.pathname === "/v1/chat/completions") {
    void handleChatCompletion(request, response);
    return;
  }

  if (request.method === "POST" && url.pathname === "/v1/audio/speech") {
    void handleSpeech(request, response);
    return;
  }

  if (request.method === "POST" && url.pathname === "/v1/images/generations") {
    void handleImageGeneration(request, response);
    return;
  }

  writeJson(response, 404, {
    error: { message: "Not found", type: "not_found" },
  });
});

function shutdown() {
  for (const response of activeResponses) {
    response.destroy();
  }
  server.close(() => process.exit(0));
  setTimeout(() => process.exit(1), 2_000).unref();
}

process.on("SIGINT", shutdown);
process.on("SIGTERM", shutdown);
server.listen(port, host);
