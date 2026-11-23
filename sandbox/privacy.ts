import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { opencodeGoProvider } from "/opt/pi/node_modules/@earendil-works/pi-ai/dist/providers/opencode-go.js";
import subagents from "/home/agent/.pi/agent/npm/node_modules/@tintinweb/pi-subagents/src/index.ts";
import { configurePrivateSession, privacyConfig } from "./privacy-runtime.ts";

export default function (pi: ExtensionAPI) {
  const { route, baseUrl } = privacyConfig();
  const model = opencodeGoProvider().getModels().find((candidate) => candidate.id === route.model);
  if (!model || model.api !== "openai-completions") throw new Error("Approved model is missing from the pinned Pi catalog");
  configurePrivateSession(pi);
  pi.registerProvider(route.provider, {
    baseUrl,
    api: model.api,
    apiKey: "$PI_PRIVACY_RELAY_TOKEN",
    models: [{ ...model, baseUrl }],
  });
  subagents(pi);
}
