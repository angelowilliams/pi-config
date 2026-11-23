import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import type { ExtensionAPI, ExtensionToolContext } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

const roles = ["general-purpose", "Explore", "Plan"];
const mainSubject = "Verify shared tasks during main work";
const steerMessage = "Synthetic steering check: finish your shared-task check, then reply STEERED.";
const guidance = readFileSync("/home/agent/.pi/agent/AGENTS.md", "utf8").trimEnd();

function hasText(value: unknown, text: string): boolean {
  if (typeof value === "string") return value.includes(text);
  if (value === null || typeof value !== "object") return false;
  return Object.values(value).some((field) => hasText(field, text));
}

function requireCheck(value: unknown, message: string): asserts value {
  if (!value) throw new Error(message);
}

async function tool(ctx: ExtensionToolContext, name: string, args: object) {
  const outcome = await ctx.executeTool(name, args);
  requireCheck(!outcome.isError, `${name} failed`);
  return outcome.result;
}

function resultText(result: Awaited<ReturnType<typeof tool>>) {
  return result.content.map((item) => (item.type === "text" ? item.text : "")).join("\n");
}

export default function (pi: ExtensionAPI) {
  const activeChildren = new Map<string, string>();
  const requestSeen = new Set<string>();
  pi.events.on("subagents:started", (event) => {
    const child = event as { type: string; id: string };
    activeChildren.set(child.type, child.id);
  });

  pi.on("before_provider_request", async (event, ctx) => {
    const role = roles.find((type) => ctx.getSystemPrompt().includes(`<active_agent name="${type}"/>`));
    if (!role || hasText(event.payload, steerMessage)) {
      if (role) writeFileSync(join(ctx.cwd, "steer-seen"), "ok");
      return;
    }
    if (requestSeen.has(role)) return;
    requestSeen.add(role);
    writeFileSync(join(ctx.cwd, `prompt-${role}`), hasText(event.payload, guidance) ? "ok" : "missing");
    const release = join(ctx.cwd, "release-children");
    for (let attempt = 0; attempt < 300 && !existsSync(release); attempt++) await delay(100);
  });

  pi.registerTool({
    name: "check_shared_tasks",
    label: "Check Shared Tasks",
    description: "Verify that this test child can read and update its parent's task list.",
    parameters: Type.Object({ role: Type.String() }),
    async execute(_id, { role }, _signal, _update, ctx) {
      requireCheck(roles.includes(role), "Invalid test child");
      const tasks = resultText(await tool(ctx, "TaskList", {}));
      requireCheck(tasks.includes(mainSubject), "Child cannot read the main task");
      await tool(ctx, "TaskCreate", {
        subject: `Child saw shared tasks: ${role}`,
        description: "A synthetic result for the sandbox smoke test.",
      });
      return { content: [{ type: "text", text: "Shared task list checked" }], details: {} };
    },
  });

  pi.registerTool({
    name: "verify_pi_tools",
    label: "Verify Pi Tools",
    description: "Run the configured task, delegation, steering and web smoke checks once.",
    parameters: Type.Object({}),
    async execute(_id, _params, _signal, _update, ctx) {
      const report = join(ctx.cwd, "result.json");
      const release = join(ctx.cwd, "release-children");
      let checkpoint = "UI";
      try {
        requireCheck(ctx.hasUI, "Expected interactive mode");
        async function newTask(subject: string, description: string, agentType?: string) {
          const created = await tool(ctx, "TaskCreate", { subject, description, ...(agentType && { agentType }) });
          const match = resultText(created).match(/Task #(\d+)/);
          requireCheck(match, "TaskCreate did not return an ID");
          return match[1];
        }

        checkpoint = "main task";
        const mainId = await newTask(mainSubject, "Run the synthetic smoke test.");
        await tool(ctx, "TaskUpdate", { taskId: mainId, status: "in_progress" });
        const taskIds = await Promise.all(roles.map((role) => newTask(
          `Synthetic ${role} child`,
          `Call check_shared_tasks with role exactly ${role}, then reply CHECKED. If the parent has steered you, follow the steering message.`,
          role,
        )));
        checkpoint = "concurrent start and guidance";
        await tool(ctx, "TaskExecute", { task_ids: taskIds, max_turns: 6 });
        const promptFiles = roles.map((role) => join(ctx.cwd, `prompt-${role}`));
        for (let attempt = 0; attempt < 200 && !promptFiles.every(existsSync); attempt++) await delay(100);
        requireCheck(activeChildren.size === roles.length && promptFiles.every(existsSync), "Children did not run concurrently");
        requireCheck(promptFiles.every((file) => readFileSync(file, "utf8") === "ok"), "Child request omitted the packaged guidance");

        checkpoint = "steering";
        const steered = await tool(ctx, "steer_subagent", { agent_id: activeChildren.get(roles[0]), message: steerMessage });
        requireCheck(/Steering message (sent|queued)/.test(resultText(steered)), "Steer tool did not confirm delivery");
        writeFileSync(release, "ok");

        checkpoint = "child results and shared task updates";
        const results = await Promise.all(taskIds.map((task_id) => tool(ctx, "TaskOutput", { task_id, block: true, timeout: 120_000 })));
        requireCheck(results.every((result) => /\[completed\]/.test(resultText(result))), "A child did not complete");
        requireCheck(results.slice(1).every((result) => /CHECKED/.test(resultText(result))), "A child result was missing");
        requireCheck(existsSync(join(ctx.cwd, "steer-seen")) && /STEERED/.test(resultText(results[0])), "Child did not act on steering");
        const tasks = resultText(await tool(ctx, "TaskList", {}));
        requireCheck(roles.every((role) => tasks.includes(`Child saw shared tasks: ${role}`)), "Parent cannot see all child updates");

        checkpoint = "Exa search";
        const search = await tool(ctx, "web_search", {
          query: "OpenCode Go documentation plan models pricing", domainFilter: ["opencode.ai"], numResults: 4, type: "fast",
        });
        const url = resultText(search).split("\n").map((line) => line.trim()).find((line) => /^https:\/\/opencode\.ai\/(docs|console)/.test(line));
        requireCheck(url, "Exa did not return a source URL");
        checkpoint = "page fetch";
        const page = await tool(ctx, "fetch_content", { url });
        const details = page.details as { url?: string; chars?: number; renderer?: string };
        requireCheck(details.url === url && (details.chars ?? 0) > 50, "Fetch did not return readable source content");

        await tool(ctx, "TaskUpdate", { taskId: mainId, status: "completed" });
        writeFileSync(report, JSON.stringify({ url, renderer: details.renderer }));
        return { content: [{ type: "text", text: "Tool verification passed." }], details: {} };
      } catch (error) {
        writeFileSync(report, JSON.stringify({ failure: checkpoint, reason: (error as Error).message }));
        return { content: [{ type: "text", text: `Tool verification failed during ${checkpoint}.` }], details: {} };
      } finally {
        writeFileSync(release, "ok");
      }
    },
  });
}
