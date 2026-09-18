"""Agent profiles for the AI Gateway.

An "agent" is just a named pairing of a model and a system prompt. A caller picks
an agent by name via POST /agent/{name}; the gateway owns the prompt so callers
stay clean.

The four agents below are worked EXAMPLES, tuned for infrastructure events (HPE
Compute Ops Management webhooks). They are meant to be edited: retune the
prompts, point them at different models, delete the ones you don't need, or add
your own. Nothing else in the gateway depends on these particular names.

Add or tune agents by editing AGENTS below. Keep prompts here, not in the caller.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AgentProfile:
    """A named model + system prompt the gateway can run."""

    name: str
    model: str
    description: str
    system_prompt: str
    provider: str = "copilot"


# Registry of available agents. `model` may be any id returned by GET /models
# (e.g. "auto", "claude-sonnet-5", "gpt-5.6-sol"). A request may override the
# model per call; the system prompt always comes from here.
#
# `provider` picks the backend this agent runs on: "copilot" (default), or any
# other id registered in providers/__init__.py ("openai", "anthropic"). E.g.:
#   AgentProfile(name="com-rca-oai", provider="openai", model="gpt-4.1", ...)
# See providers/__init__.py and README.md's "Providers: choosing a backend"
# section for the full `<provider>:<model>` routing this powers.
AGENTS: dict[str, AgentProfile] = {
    "com-triage": AgentProfile(
        name="com-triage",
        model="claude-sonnet-5",
        description="Fast first-pass classification of a COM event.",
        system_prompt=(
            "You are an HPE Compute Ops Management (COM) event triage agent.\n"
            "Given a COM webhook event, classify it quickly.\n"
            "Return concise JSON with fields:\n"
            "- category: one of hardware | connectivity | firmware | os | other\n"
            "- severity: one of info | warning | critical\n"
            "- needs_rca: boolean (true if deeper root-cause analysis is warranted)\n"
            "- one_line: a single-sentence summary\n"
            "Do not invent details that are not present in the event."
        ),
    ),
    "com-rca": AgentProfile(
        name="com-rca",
        model="gpt-5.6-sol",
        description="Root-cause analysis of a COM event, optionally with iLO data.",
        system_prompt=(
            "You are an HPE Compute Ops Management incident analysis agent.\n"
            "Analyze COM events together with any provided iLO telemetry and logs.\n"
            "Return a structured analysis with these fields:\n"
            "- summary: incident summary\n"
            "- likely_root_cause: the most probable root cause\n"
            "- evidence: the specific signals in the input that support it\n"
            "- confidence: a number between 0 and 1\n"
            "- recommended_actions: an ordered list of concrete remediation steps\n"
            "Base every conclusion on the supplied data; if data is missing, say so."
        ),
    ),
    "com-remediation": AgentProfile(
        name="com-remediation",
        model="claude-opus-5",
        description="Proposes remediation steps for a diagnosed COM incident.",
        system_prompt=(
            "You are an HPE Compute Ops Management remediation planning agent.\n"
            "Given an incident and (optionally) a root-cause analysis, propose a\n"
            "safe, ordered remediation plan.\n"
            "Return JSON with fields:\n"
            "- steps: ordered list of {action, rationale, risk}\n"
            "- requires_downtime: boolean\n"
            "- rollback: how to undo if a step fails\n"
            "Prefer least-disruptive actions first. Never suggest destructive\n"
            "operations without an explicit rollback."
        ),
    ),
    "com-summary": AgentProfile(
        name="com-summary",
        model="auto",
        description="Plain-language summary of a COM event for humans/chat.",
        system_prompt=(
            "You are an HPE Compute Ops Management summarization agent.\n"
            "Turn a COM event (and any related context) into a short, clear\n"
            "human-readable summary suitable for Slack/Teams.\n"
            "Keep it to a few sentences. Lead with what happened and why it\n"
            "matters. No JSON, no markdown headers."
        ),
    ),
}


def get_agent(name: str) -> AgentProfile:
    """Return the agent profile for `name`, or raise KeyError with a helpful list."""
    try:
        return AGENTS[name]
    except KeyError as exc:
        available = ", ".join(sorted(AGENTS)) or "(none)"
        raise KeyError(f"Unknown agent '{name}'. Available: {available}") from exc
