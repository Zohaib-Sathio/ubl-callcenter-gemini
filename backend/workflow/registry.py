from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Set

from backend.services.prompts import function_call_tools


@dataclass(frozen=True)
class WorkflowDefinition:
    id: str
    title: str
    description: str
    trigger_keywords: List[str]
    tool_names: List[str]
    runbook: str


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
            "getCustomerStatus",
        ],
        runbook=(
            "Balance Inquiry Workflow:\n"
            "1) Verify customer with verifyCustomerByCnic.\n"
            "2) Verify TPIN with verifyTpin.\n"
            "3) Only after successful verification, share permitted account insight.\n"
            "4) On repeated verification failures, transferToAgent."
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
