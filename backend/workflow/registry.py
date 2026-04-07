from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple

from backend.services.prompts import function_call_tools


@dataclass(frozen=True)
class WorkflowDefinition:
    id: str
    title: str
    description: str
    trigger_keywords: List[str]
    tool_names: List[str]
    runbook: str


@dataclass(frozen=True)
class WorkflowPhase:
    id: str
    allowed_tools: List[str]
    next_phase: Optional[str] = None


GLOBAL_TOOL_NAMES: Set[str] = {"searchKnowledgeBase", "transferToAgent"}
WORKFLOW_SELECTOR_TOOL_NAME = "selectWorkflow"


WORKFLOW_REGISTRY: Dict[str, WorkflowDefinition] = {
    "general_banking_inquiry": WorkflowDefinition(
        id="general_banking_inquiry",
        title="General Banking Inquiry",
        description="Handle FAQs, product features, eligibility, and general banking guidance.",
        trigger_keywords=[
            "loan",
            "credit card",
            "account opening",
            "investment",
            "saving",
            "branch",
            "atm",
            "charges",
            "fees",
            "remittance",
            "bill payment",
        ],
        tool_names=["searchKnowledgeBase"],
        runbook=(
            "General Inquiry Workflow:\n"
            "1) Understand the customer's banking question.\n"
            "2) Call searchKnowledgeBase before answering factual banking questions.\n"
            "3) Answer only with UBL-relevant information.\n"
            "4) If user requests human support, use transferToAgent."
        ),
    ),
    "card_activation": WorkflowDefinition(
        id="card_activation",
        title="Debit Card Activation",
        description="End-to-end debit card activation with verification and handoff safeguards.",
        trigger_keywords=[
            "activate card",
            "card activation",
            "debit card active",
            "new card",
            "card pin",
            "tpin",
        ],
        tool_names=[
            "verifyCustomerByCnic",
            "confirmPhysicalCustody",
            "verifyTpin",
            "verifyCardDetails",
            "activateCard",
            "transferToIvrForPin",
            "updateCustomerTpin",
            "getCustomerStatus",
        ],
        runbook=(
            "Card Activation Workflow:\n"
            "1) Verify CNIC using verifyCustomerByCnic.\n"
            "2) Confirm physical card possession via confirmPhysicalCustody.\n"
            "3) Verify TPIN using verifyTpin.\n"
            "4) Verify card last 4 + expiry via verifyCardDetails.\n"
            "5) Activate card via activateCard.\n"
            "6) Offer PIN generation via transferToIvrForPin when appropriate.\n"
            "7) If verification repeatedly fails or user requests, transferToAgent."
        ),
    ),
    "balance_inquiry": WorkflowDefinition(
        id="balance_inquiry",
        title="Balance Inquiry",
        description="Secure balance access flow with verification and status checks.",
        trigger_keywords=[
            "balance",
            "account balance",
            "how much money",
            "transaction",
            "statement",
        ],
        tool_names=[
            "verifyCustomerByCnic",
            "verifyTpin",
            "getAccountBalance",
            "getCustomerStatus",
        ],
        runbook=(
            "Balance Inquiry Workflow:\n"
            "1) Verify customer with verifyCustomerByCnic.\n"
            "2) Verify TPIN with verifyTpin.\n"
            "3) Ask which account the customer wants (option number or account name).\n"
            "4) Fetch selected account balance with getAccountBalance.\n"
            "5) On repeated verification failures, transferToAgent."
        ),
    ),
}


def route_workflow(instructions: str = "", caller_context: str = "") -> str:
    source = f"{instructions} {caller_context}".lower()
    if not source.strip():
        return "general_banking_inquiry"

    for workflow in WORKFLOW_REGISTRY.values():
        if any(keyword in source for keyword in workflow.trigger_keywords):
            return workflow.id

    return "general_banking_inquiry"


def get_workflow_context(workflow_id: str) -> str:
    workflow = WORKFLOW_REGISTRY.get(workflow_id, WORKFLOW_REGISTRY["general_banking_inquiry"])
    return (
        f"Active workflow: {workflow.title}\n"
        f"Purpose: {workflow.description}\n"
        f"{workflow.runbook}"
    )


def get_tools_for_workflow(workflow_id: str) -> List[dict]:
    workflow = WORKFLOW_REGISTRY.get(workflow_id, WORKFLOW_REGISTRY["general_banking_inquiry"])
    allowed = set(workflow.tool_names) | GLOBAL_TOOL_NAMES
    return [tool for tool in function_call_tools if tool.get("name") in allowed]


def get_workflow_selector_tool() -> dict:
    workflow_ids = list(WORKFLOW_REGISTRY.keys())
    return {
        "type": "function",
        "name": WORKFLOW_SELECTOR_TOOL_NAME,
        "description": (
            "Select the single best workflow for this caller request based on current user intent. "
            "Call this before workflow-specific tools such as verification and card operations."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "workflowId": {
                    "type": "string",
                    "enum": workflow_ids,
                    "description": "The selected workflow identifier.",
                },
                "reason": {
                    "type": "string",
                    "description": "Short reason why this workflow matches user intent.",
                },
            },
            "required": ["workflowId", "reason"],
        },
    }


def get_all_tools_with_selector() -> List[dict]:
    return [get_workflow_selector_tool(), *function_call_tools]


def get_workflow_policy_context() -> str:
    lines = [
        "Workflow Selection Policy:",
        f"- First call {WORKFLOW_SELECTOR_TOOL_NAME} once user intent is clear.",
        "- After selection, use only tools relevant to that workflow.",
        "- If intent changes materially, re-select workflow using selectWorkflow.",
        "- If uncertain, select general_banking_inquiry and ask a clarifying question.",
        "",
        "Available workflows:",
    ]
    for workflow in WORKFLOW_REGISTRY.values():
        lines.append(f"- {workflow.id}: {workflow.description}")
    return "\n".join(lines)


def get_allowed_tool_names(workflow_id: str) -> Set[str]:
    workflow = WORKFLOW_REGISTRY.get(workflow_id, WORKFLOW_REGISTRY["general_banking_inquiry"])
    return set(workflow.tool_names) | GLOBAL_TOOL_NAMES | {WORKFLOW_SELECTOR_TOOL_NAME}


def is_tool_allowed_for_workflow(tool_name: str, workflow_id: str | None) -> bool:
    if not workflow_id:
        return tool_name in GLOBAL_TOOL_NAMES or tool_name == WORKFLOW_SELECTOR_TOOL_NAME
    return tool_name in get_allowed_tool_names(workflow_id)


def is_valid_workflow(workflow_id: str) -> bool:
    return workflow_id in WORKFLOW_REGISTRY


CARD_ACTIVATION_PHASES: Dict[str, WorkflowPhase] = {
    "identity": WorkflowPhase(
        id="identity",
        allowed_tools=["verifyCustomerByCnic", "getCustomerStatus"],
        next_phase="custody",
    ),
    "custody": WorkflowPhase(
        id="custody",
        allowed_tools=["confirmPhysicalCustody", "getCustomerStatus"],
        next_phase="tpin",
    ),
    "tpin": WorkflowPhase(
        id="tpin",
        allowed_tools=["verifyTpin", "getCustomerStatus"],
        next_phase="card_details",
    ),
    "card_details": WorkflowPhase(
        id="card_details",
        allowed_tools=["verifyCardDetails", "getCustomerStatus"],
        next_phase="activation",
    ),
    "activation": WorkflowPhase(
        id="activation",
        allowed_tools=["activateCard", "getCustomerStatus"],
        next_phase="post_activation",
    ),
    "post_activation": WorkflowPhase(
        id="post_activation",
        allowed_tools=["transferToIvrForPin", "updateCustomerTpin", "getCustomerStatus"],
        next_phase=None,
    ),
}

BALANCE_INQUIRY_PHASES: Dict[str, WorkflowPhase] = {
    "identity": WorkflowPhase(
        id="identity",
        allowed_tools=["verifyCustomerByCnic", "getCustomerStatus"],
        next_phase="tpin",
    ),
    "tpin": WorkflowPhase(
        id="tpin",
        allowed_tools=["verifyTpin", "getCustomerStatus"],
        next_phase="balance_response",
    ),
    "balance_response": WorkflowPhase(
        id="balance_response",
        allowed_tools=["getAccountBalance", "getCustomerStatus"],
        next_phase=None,
    ),
}


def get_initial_phase_for_workflow(workflow_id: str) -> Optional[str]:
    if workflow_id == "card_activation":
        return "identity"
    if workflow_id == "balance_inquiry":
        return "identity"
    return None


def get_required_tool_for_phase(workflow_id: str, phase_id: Optional[str]) -> Optional[str]:
    if not phase_id:
        return None
    phase_map = None
    if workflow_id == "card_activation":
        phase_map = CARD_ACTIVATION_PHASES
    elif workflow_id == "balance_inquiry":
        phase_map = BALANCE_INQUIRY_PHASES
    if not phase_map:
        return None

    phase = phase_map.get(phase_id)
    if not phase:
        return None
    for tool_name in phase.allowed_tools:
        if tool_name != "getCustomerStatus":
            return tool_name
    return None


def is_tool_allowed_in_phase(
    workflow_id: str,
    phase_id: Optional[str],
    tool_name: str,
) -> Tuple[bool, Optional[str]]:
    phase_map = None
    if workflow_id == "card_activation":
        phase_map = CARD_ACTIVATION_PHASES
    elif workflow_id == "balance_inquiry":
        phase_map = BALANCE_INQUIRY_PHASES

    if not phase_map:
        return True, None
    if not phase_id:
        return False, "Workflow phase is not initialized."

    phase = phase_map.get(phase_id)
    if not phase:
        return False, f"Unknown phase '{phase_id}'."

    if tool_name in GLOBAL_TOOL_NAMES or tool_name == WORKFLOW_SELECTOR_TOOL_NAME:
        return True, None

    if tool_name in phase.allowed_tools:
        return True, None

    required_tool = get_required_tool_for_phase(workflow_id, phase_id) or "the next required step"
    return False, f"Out-of-order tool call. Complete '{required_tool}' first."


def get_next_phase_for_tool(
    workflow_id: str,
    phase_id: Optional[str],
    tool_name: str,
    tool_result: Optional[dict],
) -> Optional[str]:
    if not phase_id:
        return phase_id

    phase_map = None
    if workflow_id == "card_activation":
        phase_map = CARD_ACTIVATION_PHASES
    elif workflow_id == "balance_inquiry":
        phase_map = BALANCE_INQUIRY_PHASES
    if not phase_map:
        return phase_id

    phase = phase_map.get(phase_id)
    if not phase:
        return phase_id

    if tool_name == "getCustomerStatus":
        return phase_id

    if tool_name not in phase.allowed_tools:
        return phase_id

    if not (tool_result or {}).get("success", False):
        return phase_id

    return phase.next_phase or phase_id
