// Loadable OpenCode plugin entry. OpenCode invokes EVERY exported function as a
// plugin, so this module must export only the plugin function; the bridge
// (runBridge, createHooks, textOf) lives in aos-bridge.mjs and is imported.
import { tool } from "@opencode-ai/plugin";
import { createHooks } from "./aos-bridge.mjs";

export const AosPlugin = async ({ client, directory }) => {
  return createHooks({ client, directory }, tool);
};
