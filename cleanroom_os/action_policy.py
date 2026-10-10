"""Deterministic cleanroom gates over persisted, revisioned evidence."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime

from cleanroom_os.action_models import (
    ActionProposal,
    DecisionContext,
    PolicyCheck,
    PolicyEvaluation,
)


def _timestamp(value: object) -> datetime | None:
    """Legacy records with absent/naive/invalid dates are not proof of validity."""
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else None
    except (ValueError, TypeError):
        return None


def finish(evaluation: PolicyEvaluation) -> PolicyEvaluation:
    """Apply the most restrictive check without dropping other findings."""
    decisions = {check.decision for check in evaluation.checks}
    evaluation.decision = (
        "block" if "block" in decisions else "escalate" if "escalate" in decisions else "allow"
    )
    evaluation.effective_decision = evaluation.decision
    if evaluation.decision == "block":
        evaluation.risk_level = "high"
    elif evaluation.decision == "escalate" and evaluation.risk_level in {"none", "low"}:
        evaluation.risk_level = "medium"
    return evaluation


def evaluate(proposal: ActionProposal, context: DecisionContext, now: datetime) -> PolicyEvaluation:
    """Fail closed on missing context; never accept agent-supplied gate results."""
    checks: list[PolicyCheck] = []
    policy = context.policy

    def add(code: str, decision: str, reason: str) -> None:
        checks.append(
            PolicyCheck(
                code=code,
                decision=decision,
                reason=reason,
                overridable=bool(
                    policy and decision == "escalate" and code in policy.overridable_checks
                ),
            )
        )

    actor, task = context.records.get("actor"), context.records.get("task")
    if actor is None or task is None:
        add("identity_missing", "block", "Actor and task must exist in this instance")
    elif actor.payload.get("status") not in {"active", "ready", "idle", "operational"}:
        add("actor_unavailable", "block", "Actor is not in an operational state")
    elif task.payload.get("status") not in {"queued", "active", "in_progress", "running"}:
        add("task_unavailable", "block", "Task is not open for action execution")
    elif task.payload.get("assigned_agent_id") not in {None, proposal.actor_id}:
        add("task_assignment", "block", "Task is assigned to a different actor")
    else:
        add("identity", "allow", "Actor and task are operational and belong to this instance")

    if policy is None:
        add("policy_missing", "block", "No governing policy revision exists in this instance")
    else:
        add(
            "zone",
            "allow" if proposal.zone_id in policy.allowed_zones else "block",
            "Target zone is permitted"
            if proposal.zone_id in policy.allowed_zones
            else "Target zone is forbidden by policy",
        )

    if proposal.risk_level == "high":
        add("risk_high", "block", "High-risk actions cannot be authorized by this ruleset")
    elif proposal.risk_level == "medium":
        add("risk_review", "escalate", "Medium-risk actions require operator review")
    else:
        add("risk", "allow", "Reported action risk is within the nominal range")

    observation = context.records.get("contamination")
    if observation is None:
        add(
            "contamination_missing",
            "escalate",
            "No persisted contamination observation is available",
        )
    else:
        data = observation.payload
        observed, expires = _timestamp(data.get("observed_at")), _timestamp(data.get("expires_at"))
        evidence = data.get("evidence") or []
        valid_evidence = bool(evidence) and all(
            isinstance(item, dict)
            and (captured := _timestamp(item.get("captured_at"))) is not None
            and observed is not None
            and captured <= observed
            for item in evidence
        )
        if data.get("status") == "contaminated":
            add("contamination_detected", "block", "Contamination was detected in the target zone")
        elif not observed or not expires or not valid_evidence or not observed <= now < expires:
            add(
                "contamination_stale",
                "escalate",
                "Contamination evidence is missing, stale, or future-dated",
            )
        elif data.get("status") == "suspected":
            add(
                "contamination_suspected",
                "escalate",
                "Suspected contamination requires operator review",
            )
        elif data.get("status") != "clean":
            add("contamination_unknown", "escalate", "Contamination state is unknown")
        else:
            add("contamination", "allow", "Current persisted observation reports the zone clean")

    for kind, required, valid_status, date_field, hard_statuses in (
        (
            "maintenance",
            policy.require_maintenance if policy else True,
            "current",
            "performed_at",
            {"overdue", "out_of_service"},
        ),
        (
            "calibration",
            policy.require_calibration if policy else True,
            "valid",
            "calibrated_at",
            {"expired"},
        ),
    ):
        record = context.records.get(kind)
        data = record.payload if record else {}
        performed, due = _timestamp(data.get(date_field)), _timestamp(data.get("due_at"))
        if kind == "maintenance" and (data.get("faults") or data.get("status") == "out_of_service"):
            add("equipment_fault", "block", "Equipment has a fault or is out of service")
        elif not required:
            add(kind, "allow", f"Governing policy does not require {kind}")
        elif data.get("status") in hard_statuses or (due is not None and due <= now):
            add(f"{kind}_overdue", "block", f"Required {kind} is expired or overdue")
        elif not record or not performed or not due or performed > now or due <= performed:
            add(
                f"{kind}_unknown",
                "escalate",
                f"Required {kind} lacks valid service and expiry timestamps",
            )
        elif data.get("status") != valid_status:
            add(
                f"{kind}_review", "escalate", f"Required {kind} is not in a valid operational state"
            )
        else:
            add(kind, "allow", f"Required {kind} is valid at decision time")

    return finish(
        PolicyEvaluation(
            evaluated_at=now,
            policy_id=proposal.policy_id,
            risk_level=proposal.risk_level,
            decision="allow",
            effective_decision="allow",
            checks=checks,
            context=context,
        )
    )


def context_fingerprint(evaluation: PolicyEvaluation) -> str:
    """Bind an override to exact evidence, policy, and gate results, not wall time."""
    data = evaluation.model_dump(
        mode="json", exclude={"evaluated_at", "effective_decision", "overridden_by"}
    )
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
