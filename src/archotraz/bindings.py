from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class RuntimeBinding:
    name: str
    repository: str
    revision: str
    role: str
    integration_surface: str
    provider_type: str
    status: str
    execution_authorized: bool
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


BOPO = RuntimeBinding(
    name="Bopo",
    repository="bopodev/bopo",
    revision="d9d105353837930966aacc635d1df8364ecb4035",
    role="local runtime and control shell",
    integration_surface="packages/adapters/shell server.execute + testEnvironment",
    provider_type="shell",
    status="pinned_not_preflighted",
    execution_authorized=False,
    notes=(
        "Shell adapter is the custom-worker seam.",
        "Bopo orchestration is not a security sandbox.",
        "Enable execution only after local preflight and the sandbox boundary are verified.",
    ),
)

SANDBOX_CORE = (
    {
        "name": "Shepherd",
        "repository": "shepherd-agents/shepherd",
        "role": "capability-bound execution and OS-enforced resource grants",
        "status": "recovered_core_not_bound",
    },
    {
        "name": "Agent Workspace Guard",
        "repository": "dmonsta86/agent-workspace-guard",
        "role": "exact-result persistence authority, quarantine, rollback",
        "status": "recovered_core_not_bound",
    },
    {
        "name": "TaskForge",
        "repository": "romanklis/openclaw-contained",
        "role": "hardened runtime/container isolation and image governance",
        "status": "recovered_core_not_bound",
    },
)

CELL_FAMILIES = {
    "A": "lightweight, highly extensible, speed/extensibility favored over maximum power",
    "B": "highly capable, balanced, partially extensible middleweight",
    "C": "highest capability, heavy/dependent, difficult pairing with potentially high payoff",
    "GEN-POP": "standard candidate retained for possible improvement through combinations",
    "AD-SEG": "edge or hyper-niche candidate with high potential, high risk, or many unknowns",
}


def binding_status() -> dict[str, Any]:
    return {
        "runtime": BOPO.to_dict(),
        "sandbox_core": list(SANDBOX_CORE),
        "cell_families": CELL_FAMILIES,
        "cell_threshold_policy": "unresolved",
        "untrusted_execution_allowed": False,
    }
