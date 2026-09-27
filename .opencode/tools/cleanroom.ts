import { tool } from "@opencode-ai/plugin"
import { spawn } from "node:child_process"
import { fileURLToPath } from "node:url"
import path from "node:path"

const root = fileURLToPath(new URL("../../", import.meta.url))
function run(args: Record<string, unknown>): Promise<string> {
  const windows = process.platform === "win32"
  const executable = windows ? "py" : "python3"
  const argv = [...(windows ? ["-3"] : []), path.join(root, "scripts", "interactive.py"), JSON.stringify(args)]
  return new Promise((resolve, reject) => {
    const child = spawn(executable, argv, { cwd: root, shell: false })
    let stdout = "", stderr = ""
    const timer = setTimeout(() => { child.kill(); reject(new Error("Controller setup/action timed out after 180 seconds.")) }, 180000)
    child.stdout.on("data", chunk => { stdout += chunk })
    child.stderr.on("data", chunk => { stderr += chunk })
    child.on("error", error => { clearTimeout(timer); reject(new Error(`Python 3.11+ is required: ${error.message}`)) })
    child.on("close", code => {
      clearTimeout(timer)
      if (code !== 0) reject(new Error(stderr || stdout || `Controller exited ${code}`))
      else resolve(stdout)
    })
  })
}

export const workflow = tool({
  description: "Run the persisted synthetic cleanroom workflow. Python validates every transition. No approvals through this tool. First use installs the local Python package automatically.",
  args: {
    action: tool.schema.enum(["status", "events", "notifications", "context", "propose", "collect", "results", "review"]),
    resolved: tool.schema.boolean().optional().describe("Only with explicit user instruction to use the synthetic Room B schedule update"),
    normal: tool.schema.boolean().optional().describe("Use normal synthetic LIMS results; false imports the anomaly fixture"),
    reason: tool.schema.string().optional(),
  },
  async execute(args, context) {
    if (context.agent !== "cleanroom") throw new Error("Only the interactive cleanroom agent can use this tool.")
    return run(args)
  },
})

export const decision = tool({
  description: "Record an explicit human manufacturing or QA decision for the displayed revision. Requires user confirmation; never call to manufacture approval.",
  args: {
    action: tool.schema.enum(["allow", "disallow", "qa-approve", "qa-reject", "qa-resolve"]),
    revision: tool.schema.number().int().positive(),
    package_revision: tool.schema.number().int().positive().optional(),
    reason: tool.schema.string().min(1),
  },
  async execute(args, context) {
    if (context.agent !== "cleanroom") throw new Error("Subagents cannot record human decisions.")
    await context.ask({ permission: "cleanroom_decision", patterns: [JSON.stringify(args)], always: [], metadata: args })
    return run(args)
  },
})
