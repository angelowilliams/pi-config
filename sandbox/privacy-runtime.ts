import { readFileSync } from "node:fs";
import { getAgentDir, type ExtensionAPI } from "@earendil-works/pi-coding-agent";
import tasks from "/home/agent/.pi/agent/npm/node_modules/@tintinweb/pi-tasks/src/index.ts";

const configPath = "/opt/pi/privacy-models.json";
const privateAgent = "/opt/pi/private-agent";

type PrivateRoute = { provider: string; model: string; policy: string; validThrough: string };

let cachedConfig: { route: PrivateRoute; baseUrl: string } | undefined;

export function privacyConfig() {
  if (cachedConfig) return cachedConfig;
  const config = JSON.parse(readFileSync(configPath, "utf8")) as { default: string; routes: PrivateRoute[] };
  // The host relay validates the whole document and is the sole authority on which
  // routes are approved. The guest only needs the route this session was started
  // with, so it selects the document default instead of constraining the file shape.
  const route = (Array.isArray(config.routes) ? config.routes : []).find((candidate) =>
    candidate?.provider === "opencode-go" && config.default === `${candidate.provider}/${candidate.model}`);
  if (!route) throw new Error("Invalid private model configuration");
  const expiry = Date.parse(`${route.validThrough}T23:59:59.999Z`);
  if (!Number.isFinite(expiry) || Date.now() > expiry) throw new Error("Privacy model approval has expired");

  const baseUrl = process.env.PI_PRIVACY_BASE_URL ?? "";
  if (!/^http:\/\/host\.docker\.internal:\d{1,5}\/v1$/.test(baseUrl) || !process.env.PI_PRIVACY_RELAY_TOKEN) {
    throw new Error("Private inference relay is unavailable");
  }
  cachedConfig = { route, baseUrl };
  return cachedConfig;
}

export function configurePrivateSession(pi: ExtensionAPI) {
  if (process.env.PI_SANDBOX_PRIVACY !== "1" || getAgentDir() !== privateAgent) {
    throw new Error("Wrong guest runtime for privacy mode");
  }
  privacyConfig();
  tasks(pi);
  pi.on("session_start", (_event, ctx) => {
    if (!ctx.hasUI) return;
    ctx.ui.setStatus("pi-sandbox-privacy", "Privacy mode");
  });
}
