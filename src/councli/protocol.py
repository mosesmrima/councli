from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from jinja2 import Template

from councli.agents import AgentRunResult, AgentRunner
from councli.artifacts import write_json, write_text


PROPOSAL_PROMPT = Template(
    """You are one member of a council of coding CLI agents.

Task:
{{ task }}

Your job in this round:
- Propose the best approach.
- Identify risks and assumptions.
- Say whether you are a good executor for this task.
- Do not edit files.

Return concise markdown with these headings:
## Plan
## Risks
## Executor Fit
"""
)


CRITIQUE_PROMPT = Template(
    """You are one member of a council of coding CLI agents.

Task:
{{ task }}

Other agents' proposals:
{{ proposals }}

Your job in this round:
- Critique the proposals.
- Point out missing risks, weak assumptions, or unnecessary complexity.
- Revise your own position if another proposal is stronger.
- Do not edit files.

Return concise markdown with these headings:
## Critique
## Revised Position
"""
)


VOTE_PROMPT = Template(
    """You are one member of a council of coding CLI agents.

Task:
{{ task }}

Proposals:
{{ proposals }}

Critiques:
{{ critiques }}

Vote for the best plan and the best executor from these available agents:
{{ agent_names }}

Return ONLY JSON with this shape:
{
  "preferred_plan": "agent-name",
  "preferred_executor": "agent-name",
  "confidence": 0.0,
  "blocking_concerns": ["concern"],
  "reason": "short reason"
}
"""
)

IMPLEMENT_PROMPT = Template(
    """You are the selected executor for a councli agent council.

Task:
{{ task }}

Selected plan:
{{ selected_plan }}

Selected plan content:
{{ selected_plan_content }}

Revision concerns:
{{ revision_concerns }}

Council transcript:
{{ transcript }}

Your job:
- Implement the selected council plan above.
- If you must deviate, explicitly call out the deviation and why.
- Keep the change scoped to the task.
- Add or update tests when appropriate.
- Do not merge branches.

When finished, summarize what changed and what validation you ran.
"""
)


@dataclass(frozen=True)
class CouncilDecision:
    selected_plan: str | None
    selected_executor: str | None
    approved: bool
    reason: str
    votes: dict[str, dict]


def run_reason_protocol(
    *,
    task: str,
    runners: dict[str, AgentRunner],
    root: Path,
    run_dir: Path,
    dry_run: bool = False,
) -> CouncilDecision:
    active = {name: runner for name, runner in runners.items() if runner.health().available}
    write_text(run_dir / "task.md", task.strip() + "\n")
    write_json(run_dir / "agents.json", {name: runner.health() for name, runner in runners.items()})

    proposals: dict[str, AgentRunResult] = {}
    for name, runner in active.items():
        result = runner.run(PROPOSAL_PROMPT.render(task=task), cwd=root, dry_run=dry_run)
        proposals[name] = result
        write_text(run_dir / "proposals" / f"{name}.md", render_result(result))

    proposal_successes = {name: result for name, result in proposals.items() if result.ok}
    proposal_block = join_outputs(proposal_successes)

    critiques: dict[str, AgentRunResult] = {}
    for name, runner in active.items():
        if name not in proposal_successes:
            continue
        result = runner.run(
            CRITIQUE_PROMPT.render(task=task, proposals=proposal_block),
            cwd=root,
            dry_run=dry_run,
        )
        critiques[name] = result
        write_text(run_dir / "critiques" / f"{name}.md", render_result(result))

    critique_successes = {name: result for name, result in critiques.items() if result.ok}
    critique_block = join_outputs(critique_successes)

    votes: dict[str, dict] = {}
    voters = {name: runner for name, runner in active.items() if name in proposal_successes and name in critique_successes}
    dry_run_choice = next(iter(voters.keys()), None)
    for name, runner in voters.items():
        if dry_run:
            result = runner.run(
                VOTE_PROMPT.render(
                    task=task,
                    proposals=proposal_block,
                    critiques=critique_block,
                agent_names=", ".join(voters.keys()),
                ),
                cwd=root,
                dry_run=True,
            )
            votes[name] = {
                "preferred_plan": dry_run_choice,
                "preferred_executor": dry_run_choice,
                "confidence": 1.0,
                "blocking_concerns": [],
                "reason": "dry-run synthetic vote",
            }
        else:
            result = runner.run(
                VOTE_PROMPT.render(
                    task=task,
                    proposals=proposal_block,
                    critiques=critique_block,
                    agent_names=", ".join(voters.keys()),
                ),
                cwd=root,
                dry_run=False,
            )
            votes[name] = parse_vote(result.output) if result.ok else {
                "preferred_plan": None,
                "preferred_executor": None,
                "confidence": 0.0,
                "blocking_concerns": [result.error or "vote failed"],
                "reason": "vote failed",
            }
        write_text(run_dir / "votes" / f"{name}.raw.txt", render_result(result))
        write_json(run_dir / "votes" / f"{name}.json", votes[name])

    decision = decide(votes, list(voters.keys()))
    write_json(run_dir / "decision.json", decision)
    write_text(run_dir / "transcript.md", render_transcript(task, proposals, critiques, votes, decision))
    return decision


def run_executor(
    *,
    task: str,
    runner: AgentRunner,
    worktree: Path,
    transcript: str,
    run_dir: Path,
    selected_plan: str | None = None,
    selected_plan_content: str = "",
    revision_concerns: list[str] | None = None,
    dry_run: bool = False,
) -> AgentRunResult:
    result = runner.run(
        IMPLEMENT_PROMPT.render(
            task=task,
            selected_plan=selected_plan or "(none)",
            selected_plan_content=selected_plan_content or "(none)",
            revision_concerns="\n".join(f"- {item}" for item in revision_concerns or []) or "(none)",
            transcript=transcript,
        ),
        cwd=worktree,
        dry_run=dry_run,
    )
    write_text(run_dir / "implementation" / f"{runner.name}.md", render_result(result))
    return result


def decide(votes: dict[str, dict], agents: list[str]) -> CouncilDecision:
    if not agents:
        return CouncilDecision(
            selected_plan=None,
            selected_executor=None,
            approved=False,
            reason="No available agents.",
            votes=votes,
        )
    if len(agents) == 1:
        only = agents[0]
        return CouncilDecision(
            selected_plan=only,
            selected_executor=only,
            approved=True,
            reason="Only one agent available; consensus reduced to single-agent mode.",
            votes=votes,
        )

    plan_counts = count_votes(votes, "preferred_plan", agents)
    executor_counts = count_votes(votes, "preferred_executor", agents)
    threshold = (len(agents) // 2) + 1
    selected_plan, plan_count = winner(plan_counts)
    selected_executor, executor_count = winner(executor_counts)
    blocking = [
        concern
        for vote in votes.values()
        for concern in vote.get("blocking_concerns", []) or []
        if str(concern).strip()
    ]

    approved = bool(
        selected_plan
        and selected_executor
        and plan_count >= threshold
        and executor_count >= threshold
        and not blocking
    )
    if approved:
        reason = f"Majority consensus reached: plan={selected_plan}, executor={selected_executor}."
    elif blocking:
        reason = "Consensus blocked by one or more blocking concerns."
    else:
        reason = "No majority consensus; run another deliberation round or adjust agents."

    return CouncilDecision(
        selected_plan=selected_plan,
        selected_executor=selected_executor,
        approved=approved,
        reason=reason,
        votes=votes,
    )


def count_votes(votes: dict[str, dict], key: str, agents: list[str]) -> dict[str, int]:
    counts = {name: 0 for name in agents}
    for vote in votes.values():
        choice = vote.get(key)
        if choice in counts:
            counts[choice] += 1
    return counts


def winner(counts: dict[str, int]) -> tuple[str | None, int]:
    if not counts:
        return None, 0
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    if ranked[0][1] == 0:
        return None, 0
    return ranked[0]


def parse_vote(output: str) -> dict:
    output = output.strip()
    if not output:
        return empty_vote("empty vote output")
    try:
        raw = json.loads(output)
        return normalize_vote(raw)
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{.*\}", output, flags=re.DOTALL)
    if not match:
        return empty_vote("could not find JSON vote")
    try:
        return normalize_vote(json.loads(match.group(0)))
    except json.JSONDecodeError:
        return empty_vote("invalid JSON vote")


def normalize_vote(raw: object) -> dict:
    if not isinstance(raw, dict):
        return empty_vote("vote was not an object")
    confidence = raw.get("confidence", 0.0)
    try:
        confidence_float = float(confidence)
    except (TypeError, ValueError):
        confidence_float = 0.0
    concerns = raw.get("blocking_concerns", [])
    if not isinstance(concerns, list):
        concerns = [str(concerns)]
    return {
        "preferred_plan": raw.get("preferred_plan"),
        "preferred_executor": raw.get("preferred_executor"),
        "confidence": max(0.0, min(1.0, confidence_float)),
        "blocking_concerns": concerns,
        "reason": str(raw.get("reason", "")),
    }


def empty_vote(reason: str) -> dict:
    return {
        "preferred_plan": None,
        "preferred_executor": None,
        "confidence": 0.0,
        "blocking_concerns": [reason],
        "reason": reason,
    }


def join_outputs(results: dict[str, AgentRunResult]) -> str:
    blocks = []
    for name, result in results.items():
        body = result.output if result.ok else f"FAILED: {result.error}"
        blocks.append(f"## {name}\n\n{body}")
    return "\n\n".join(blocks)


def render_result(result: AgentRunResult) -> str:
    status = "ok" if result.ok else "failed"
    if result.skipped:
        status = "skipped"
    parts = [
        f"# {result.name}",
        "",
        f"- status: {status}",
        f"- exit_code: {result.exit_code}",
        f"- command: `{shell_join(result.command)}`",
        "",
        "## Output",
        "",
        result.output or "",
    ]
    if result.error:
        parts.extend(["", "## Error", "", result.error])
    return "\n".join(parts).rstrip() + "\n"


def render_transcript(
    task: str,
    proposals: dict[str, AgentRunResult],
    critiques: dict[str, AgentRunResult],
    votes: dict[str, dict],
    decision: CouncilDecision,
) -> str:
    lines = [
        "# councli transcript",
        "",
        "## Task",
        "",
        task,
        "",
        "## Decision",
        "",
        f"- approved: {decision.approved}",
        f"- selected_plan: {decision.selected_plan}",
        f"- selected_executor: {decision.selected_executor}",
        f"- reason: {decision.reason}",
        "",
        "## Proposals",
        "",
        join_outputs(proposals),
        "",
        "## Critiques",
        "",
        join_outputs(critiques),
        "",
        "## Votes",
        "",
        json.dumps(votes, indent=2, sort_keys=True),
    ]
    return "\n".join(lines).rstrip() + "\n"


def shell_join(command: list[str]) -> str:
    return " ".join(command)
