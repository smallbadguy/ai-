// Reviewer agent for a replay result. It has exactly one tool: review_verdict (pass / send back with suggestions).
// Everything it needs is already in its system prompt; it cannot read files or query data.
// The run may only end after review_verdict has been called (soft prompt + hard guard below).
import { Type } from "typebox";
import fs from "node:fs";

const out = process.env.REPLAY_REVIEW_OUT!;
const MAX_REMINDERS = 3;

export default function(pi: any) {
  let decided = false;
  let reminders = 0;

  pi.on("tool_call", async (event: any) => {
    if (event.toolName !== "review_verdict") return {block: true, reason: "The only available tool is review_verdict."};
  });

  pi.registerTool({
    name: "review_verdict",
    label: "review_verdict",
    description: "Record the review decision. decision=pass when the replay can be shown; decision=reject to send it back. "
      + "For reject, list concrete suggestions: which item (moment / open item / summary / coverage), what is wrong, what to do. "
      + "Calling this ends the review.",
    parameters: Type.Object({
      decision: Type.Union([Type.Literal("pass"), Type.Literal("reject")]),
      summary: Type.String(),
      suggestions: Type.Array(Type.Object({
        target: Type.String(),
        problem: Type.String(),
        suggestion: Type.String(),
      })),
    }),
    async execute(_id: string, args: any) {
      if (args.decision === "reject" && !(args.suggestions || []).length) {
        return {content: [{type: "text", text: "A reject needs at least one concrete suggestion."}], details: {}, isError: true};
      }
      fs.writeFileSync(out, JSON.stringify({...args, at: new Date().toISOString()}, null, 2));
      decided = true;
      return {content: [{type: "text", text: "Verdict recorded."}], details: {}, terminate: true};
    },
  });

  // Hard guard: the review cannot finish without a recorded verdict.
  pi.on("agent_before_settle", async (event: any) => {
    if (decided || reminders >= MAX_REMINDERS) return;
    reminders += 1;
    return {
      entries: [...event.entries, {
        type: "custom_message", customType: "verdict-required", display: false,
        content: "You have not recorded a verdict. Call review_verdict now (pass, or reject with concrete suggestions).",
      }],
      continue: true,
    };
  });
}
