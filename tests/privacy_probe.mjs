// Run inside the Pi guest using `node --input-type=module -` and the private
// launch environment. This SDK probe never sends an allowed inference request.
import assert from "node:assert/strict";
import {
  createAgentSessionFromServices, createAgentSessionServices, getAgentDir, SessionManager,
} from "/opt/pi/node_modules/@earendil-works/pi-coding-agent/dist/index.js";

const cwd = process.cwd();
const agentDir = getAgentDir();
const extensionPath = "/opt/pi/privacy.ts";
const services = await createAgentSessionServices({
  cwd,
  agentDir,
  resourceLoaderOptions: {
    projectTrusted: false,
    noExtensions: true,
    noSkills: true,
    noPromptTemplates: true,
    additionalExtensionPaths: [extensionPath],
  },
});
assert.deepEqual(services.diagnostics, []);
const loaded = services.resourceLoader.getExtensions();
assert.deepEqual(loaded.errors, []);
assert.deepEqual(loaded.extensions.map(({ path }) => path), [extensionPath]);

const { session, modelFallbackMessage } = await createAgentSessionFromServices({
  services,
  sessionManager: SessionManager.inMemory(cwd),
});
try {
  await session.bindExtensions({ onError: () => { throw new Error("Private extension error"); } });
  assert.equal(modelFallbackMessage, undefined);
  const runtime = session.modelRuntime;
  const approved = session.model;
  assert.equal(`${approved.provider}/${approved.id}`, "opencode-go/deepseek-v4.1-flash");
  assert.equal(approved.baseUrl, process.env.PI_PRIVACY_BASE_URL);

  const options = { sessionId: "private-probe" };
  const prepared = await runtime.prepareRequest(approved, options);
  assert.ok(prepared.options.apiKey === process.env.PI_PRIVACY_RELAY_TOKEN, "Relay bearer mismatch");
  assert.equal(prepared.options.headers["x-opencode-session"], options.sessionId);

  const tools = session.getAllTools().map(({ name }) => name);
  for (const name of ["Agent", "TaskCreate", "TaskList", "bash", "read"]) assert.ok(tools.includes(name), name);
  for (const name of ["web_search", "fetch_content"]) assert.ok(!tools.includes(name), name);

  for (const variable of ["PI_TASKS", "PI_CODING_AGENT_SESSION_DIR"]) {
    assert.match(process.env[variable] ?? "", /^\/tmp\//, `${variable} must stay in the ephemeral guest`);
  }
} finally {
  await session.dispose();
}
console.log("Private guest SDK checks passed.");
