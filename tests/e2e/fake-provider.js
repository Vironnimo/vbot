import http from "node:http";

const SERVICE_NAME = "vbot-e2e-fake-provider";
const host = process.env.VBOT_E2E_PROVIDER_HOST ?? "127.0.0.1";
const port = Number.parseInt(process.env.VBOT_E2E_PROVIDER_PORT ?? "8438", 10);
const activeResponses = new Set();

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

function responseText(model, prompt) {
  if (model.includes("e2e-fallback")) {
    return "Fallback provider response.";
  }
  if (prompt.includes("E2E_STREAM")) {
    return "Fake provider streaming response.";
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
      completionBody(model, responseText(model, prompt)),
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

  const text = responseText(model, prompt);
  const chunks = text.match(/\S+\s*/g) ?? [text];
  await streamCompletion(response, model, chunks, 75);
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
