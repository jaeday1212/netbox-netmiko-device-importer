from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


__all__ = [
    "NormalizedDevice",
    "NormalizedModuleBay",
    "NormalizedModule",
    "NormalizedInterface",
    "NormalizedLag",
    "NormalizedInventory",
    "Proposal",
    "ProposalBatch",
    "proposal_to_json",
]


@dataclass(slots=True)
class NormalizedDevice:
    name: str
    site_slug: str
    role_slug: str
    manufacturer_slug: str
    device_type_slug: str
    status: str = "active"
    serial: Optional[str] = None
    asset_tag: Optional[str] = None
    custom_fields: Dict[str, Any] = field(default_factory=dict)
    tags: List[str] = field(default_factory=list)


@dataclass(slots=True)
class NormalizedModuleBay:
    name: str
    label: Optional[str] = None
    position: Optional[str] = None


@dataclass(slots=True)
class NormalizedModule:
    bay_name: str
    module_type_model: str
    status: str = "active"
    serial: Optional[str] = None
    custom_fields: Dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class NormalizedInterface:
    name: str
    type_slug: str
    enabled: bool = True
    description: Optional[str] = None
    lag: Optional[str] = None
    mtu: Optional[int] = None
    mac_address: Optional[str] = None
    tagged_vlans: List[str] = field(default_factory=list)
    untagged_vlan: Optional[str] = None
    custom_fields: Dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class NormalizedLag:
    name: str
    description: Optional[str] = None
    members: List[str] = field(default_factory=list)
    enabled: bool = True
    custom_fields: Dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class NormalizedInventory:
    device: NormalizedDevice
    module_bays: List[NormalizedModuleBay] = field(default_factory=list)
    modules: List[NormalizedModule] = field(default_factory=list)
    interfaces: List[NormalizedInterface] = field(default_factory=list)
    lags: List[NormalizedLag] = field(default_factory=list)


@dataclass(slots=True)
class Proposal:
    action: str  # create | update | noop
    model: str   # device | module_bay | module | interface | lag
    identifier: str
    desired: Dict[str, Any]
    current: Optional[Dict[str, Any]] = None
    diff: Optional[Dict[str, Any]] = None


@dataclass(slots=True)
class ProposalBatch:
    device: Proposal
    module_bays: List[Proposal] = field(default_factory=list)
    modules: List[Proposal] = field(default_factory=list)
    interfaces: List[Proposal] = field(default_factory=list)
    lags: List[Proposal] = field(default_factory=list)

    def actions(self) -> List[Proposal]:
        return [
            self.device,
            *self.module_bays,
            *self.modules,
            *self.interfaces,
            *self.lags,
        ]

    def to_json(self) -> Dict[str, Any]:
        return {
            "device": proposal_to_json(self.device),
            "module_bays": [proposal_to_json(p) for p in self.module_bays],
            "modules": [proposal_to_json(p) for p in self.modules],
            "interfaces": [proposal_to_json(p) for p in self.interfaces],
            "lags": [proposal_to_json(p) for p in self.lags],
        }


def proposal_to_json(proposal: Proposal) -> Dict[str, Any]:
    return {
        "action": proposal.action,
        "model": proposal.model,
        "identifier": proposal.identifier,
        "desired": proposal.desired,
        "current": proposal.current,
        "diff": proposal.diff,
    }
