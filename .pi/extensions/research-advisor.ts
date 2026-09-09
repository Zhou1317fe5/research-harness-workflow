import type { BeforeAgentStartEvent, ExtensionAPI } from "@earendil-works/pi-coding-agent";

export const RESEARCH_ADVISOR_SNIPPET =
	"Ask advisor for a high-value research decision after gathering evidence and candidate explanations";

export const RESEARCH_ADVISOR_GUIDELINES = [
	"Use advisor only as optional decision support for the main research Executor. First read the relevant approved intent, CSV row, skills, references and real code, gather evidence, and state candidate approaches and unresolved questions.",
	"Design / Plan: call advisor when multiple reasonable technical or research approaches remain, a core hypothesis or architecture is unresolved, the decision commits substantial later work, or a major approach change is proposed.",
	"Implementation Alignment: call advisor when approved intent, CSV and actual implementation disagree, reasonable implementations compete, or a consequential scientific-behavior decision could contaminate later experiments. Clear ordinary implementation needs no advisor call.",
	"Conflict: call advisor for a material conflict among approved intent, CSV, code, evidence and interpretation after collecting the facts. Existing workflow hard-stops remain authoritative; advisor cannot waive a confirmed design error or authorize work outside the approved scope.",
	"Difficult Debug: reproduce the problem, inspect logs and the real code path, state root-cause hypotheses and run a minimal probe first. Call advisor only when plausible roots or fixes compete, evidence conflicts with the hypothesis, or the fix changes the approach or scientific behavior.",
	"Experiment Interpretation / Next: confirm run identity, extract metrics, compare the baseline and relevant folds/shots/ablations, list anomalies and candidate explanations first. Call advisor for conflicting hypotheses, competing explanations, a strong claim, a research-direction change, or the next most informative experiment.",
	"Do not call advisor mechanically before work, at completion, or once per stage. Reading, search, ordinary coding/debugging/tests, formatting, CSV/Git bookkeeping, rrctl lifecycle/polling, artifact transport, metric extraction, tables/documentation and normal evidence-close belong to the Executor. Low-risk work normally uses zero calls; one or two calls across a research task is an efficiency target, not a hard limit.",
	"After advisor returns Plan, Correction or Stop, explain its material guidance to the user and continue within the existing workflow. Advisor opinions are not scientific evidence. Formal PRERUN and its independent Scientific Reviewer remain separate: advisor never substitutes for review, overrides a verdict or gate, or supplies conclusions to the Reviewer.",
];

/** 只替换工具自己贡献的提示，保留其他扩展、用户提示和项目上下文。 */
export function researchAdvisorPrompt(
	event: Pick<BeforeAgentStartEvent, "systemPrompt" | "systemPromptOptions">,
	advisorGuidelines: readonly string[],
): string {
	const { systemPromptOptions: options } = event;
	let prompt = event.systemPrompt;
	if (!options.customPrompt) {
		// 只处理 Pi 生成的工具区域；项目文件、用户追加提示和自定义 system prompt 不参与替换。
		const toolsStart = prompt.indexOf("\nAvailable tools:\n");
		const toolsEnd = prompt.indexOf("\n\nIn addition to the tools above", toolsStart);
		const snippet = options.toolSnippets?.advisor;
		if (toolsStart >= 0 && toolsEnd > toolsStart && snippet) {
			const section = prompt.slice(toolsStart, toolsEnd)
				.replace(`- advisor: ${snippet}`, `- advisor: ${RESEARCH_ADVISOR_SNIPPET}`);
			prompt = prompt.slice(0, toolsStart) + section + prompt.slice(toolsEnd);
		}
		const guidelinesStart = prompt.indexOf("\nGuidelines:\n");
		const guidelinesEnd = prompt.indexOf("\n\nPi documentation", guidelinesStart);
		if (guidelinesStart >= 0 && guidelinesEnd > guidelinesStart) {
			let section = `${prompt.slice(guidelinesStart, guidelinesEnd)}\n`;
			// 从已注册工具获取原文，不固化上游策略；整条匹配也支持多行 guidance。
			for (const value of new Set(advisorGuidelines.map((line) => line.trim()).filter(Boolean))) {
				section = section.replace(`\n- ${value}\n`, "\n");
			}
			prompt = prompt.slice(0, guidelinesStart) + section.slice(0, -1) + prompt.slice(guidelinesEnd);
		}
	}
	return `${prompt}\n\nResearch advisor policy:\n${RESEARCH_ADVISOR_GUIDELINES.map((value) => `- ${value}`).join("\n")}`;
}

export default function researchAdvisor(pi: ExtensionAPI): void {
	// Reviewer 的 CLI 工具白名单另行限制能力；此处不向任何子代理注入主 Executor 策略。
	if (Number.parseInt(process.env.PI_SUB_AGENT_DEPTH ?? "0", 10) > 0) return;

	pi.on("before_agent_start", (event) => {
		if (!pi.getActiveTools().includes("advisor")) return;
		const advisor = pi.getAllTools().find((tool) => tool.name === "advisor");
		if (!advisor) return;
		return { systemPrompt: researchAdvisorPrompt(event, advisor.promptGuidelines ?? []) };
	});
}
