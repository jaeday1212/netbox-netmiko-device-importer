from __future__ import annotations

import json
import logging
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

import pynetbox

from .config_loader import AppConfig, load_app_config
from .models import NormalizedInventory, Proposal, ProposalBatch


logger = logging.getLogger(__name__)

__all__ = [
    "slugify",
    "DeviceState",
    "ProposalEngine",
    "NetboxDeviceBuilder",
]


def slugify(value: str) -> str:
    cleaned = value.strip().lower()
    result = "".join(ch if ch.isalnum() else "-" for ch in cleaned)
    while "--" in result:
        result = result.replace("--", "-")
    return result.strip("-")


@dataclass(slots=True)
class DeviceState:
    module_bays: Dict[str, Dict]
    modules: Dict[str, Dict]
    interfaces: Dict[str, Dict]
    lag_membership: Dict[str, Set[str]]


class ProposalEngine:
    def __init__(self, nb: pynetbox.api.Api) -> None:
        self.nb = nb

    def build(self, inventory: NormalizedInventory) -> ProposalBatch:
        existing_device = self.nb.dcim.devices.get(name=inventory.device.name)
        state = self._load_state(existing_device) if existing_device else None

        device_proposal = self._device_proposal(inventory, existing_device)
        module_bay_proposals = [self._module_bay_proposal(bay, state) for bay in inventory.module_bays]
        module_proposals = [self._module_proposal(module, state) for module in inventory.modules]
        interface_proposals = self._interfaces_proposals(inventory.interfaces, state)
        lag_proposals = self._lag_proposals(inventory.lags, state)

        return ProposalBatch(
            device=device_proposal,
            module_bays=module_bay_proposals,
            modules=module_proposals,
            interfaces=interface_proposals,
            lags=lag_proposals,
        )

    def _device_proposal(self, inventory: NormalizedInventory, existing_device: Optional[pynetbox.core.response.Record]) -> Proposal:
        device = inventory.device
        desired = {
            "name": device.name,
            "status": device.status,
            "site": device.site_slug,
            "role": device.role_slug,
            "device_type": device.device_type_slug,
            "manufacturer": device.manufacturer_slug,
            "serial": device.serial,
            "asset_tag": device.asset_tag,
            "tags": device.tags,
            "custom_fields": device.custom_fields,
        }

        current = None
        if existing_device:
            data = existing_device.serialize()
            current = {
                "name": data.get("name"),
                "status": (data.get("status") or {}).get("value"),
                "site": (data.get("site") or {}).get("slug"),
                "role": (data.get("role") or {}).get("slug"),
                "device_type": (data.get("device_type") or {}).get("slug"),
                "manufacturer": (data.get("device_type") or {}).get("manufacturer", {}).get("slug"),
                "serial": data.get("serial"),
                "asset_tag": data.get("asset_tag"),
                "tags": [tag.get("slug") for tag in data.get("tags", [])],
                "custom_fields": data.get("custom_fields"),
            }

        action, diff = self._action_and_diff(desired, current, existing_device is not None)

        return Proposal(
            action=action,
            model="device",
            identifier=device.name,
            desired=desired,
            current=current,
            diff=diff,
        )

    def _module_bay_proposal(self, bay, state: Optional[DeviceState]) -> Proposal:
        desired = {
            "name": bay.name,
            "label": bay.label or bay.name,
            "position": bay.position,
        }
        existing = (state.module_bays.get(bay.name) if state else None)
        current = None
        if existing:
            current = {
                "name": existing.get("name"),
                "label": existing.get("label"),
                "position": existing.get("position"),
            }
        action, diff = self._action_and_diff(desired, current, existing is not None)
        return Proposal(
            action=action,
            model="module_bay",
            identifier=bay.name,
            desired=desired,
            current=current,
            diff=diff,
        )

    def _module_proposal(self, module, state: Optional[DeviceState]) -> Proposal:
        desired = {
            "bay_name": module.bay_name,
            "module_type_model": module.module_type_model,
            "status": module.status,
            "serial": module.serial,
        }
        existing = (state.modules.get(module.bay_name) if state else None)
        current = None
        if existing:
            current = {
                "bay_name": (existing.get("module_bay") or {}).get("name"),
                "module_type_model": (existing.get("module_type") or {}).get("model"),
                "status": (existing.get("status") or {}).get("value"),
                "serial": existing.get("serial"),
            }
        action, diff = self._action_and_diff(desired, current, existing is not None)
        identifier = f"{module.bay_name}:{module.module_type_model}"
        return Proposal(
            action=action,
            model="module",
            identifier=identifier,
            desired=desired,
            current=current,
            diff=diff,
        )

    def _interfaces_proposals(self, interfaces, state: Optional[DeviceState]) -> List[Proposal]:
        existing_map = state.interfaces if state else {}
        proposals: List[Proposal] = []
        sorted_interfaces = sorted(interfaces, key=lambda iface: (iface.type_slug != "lag", iface.name))
        for iface in sorted_interfaces:
            desired = {
                "type": iface.type_slug,
                "enabled": iface.enabled,
                "description": iface.description,
            }
            if iface.lag:
                desired["lag"] = iface.lag

            existing = existing_map.get(iface.name)
            current = None
            if existing:
                current = {
                    "type": (existing.get("type") or {}).get("value"),
                    "enabled": existing.get("enabled"),
                    "description": existing.get("description"),
                }
                lag = existing.get("lag")
                if lag and lag.get("name"):
                    current["lag"] = lag.get("name")

            action, diff = self._action_and_diff(desired, current, existing is not None)
            proposals.append(
                Proposal(
                    action=action,
                    model="interface",
                    identifier=iface.name,
                    desired=desired,
                    current=current,
                    diff=diff,
                )
            )
        return proposals

    def _lag_proposals(self, lags, state: Optional[DeviceState]) -> List[Proposal]:
        if not lags:
            return []
        membership = state.lag_membership if state else {}
        interfaces = state.interfaces if state else {}
        proposals: List[Proposal] = []
        for lag in lags:
            desired_members = sorted(set(lag.members))
            current_members = sorted(membership.get(lag.name, set()))
            current = {"members": current_members} if current_members else None
            exists = lag.name in interfaces
            action, diff = self._action_and_diff({"members": desired_members}, current, exists)
            proposals.append(
                Proposal(
                    action=action,
                    model="lag",
                    identifier=lag.name,
                    desired={"members": desired_members},
                    current=current,
                    diff=diff,
                )
            )
        return proposals

    def _load_state(self, device: pynetbox.core.response.Record) -> DeviceState:
        module_bays: Dict[str, Dict] = {}
        for record in self.nb.dcim.module_bays.filter(device_id=device.id, limit=0):
            data = record.serialize()
            module_bays[data["name"]] = data

        modules: Dict[str, Dict] = {}
        for record in self.nb.dcim.modules.filter(device_id=device.id, limit=0):
            data = record.serialize()
            bay = (data.get("module_bay") or {}).get("name")
            if bay:
                modules[bay] = data

        interfaces: Dict[str, Dict] = {}
        lag_membership: Dict[str, Set[str]] = {}
        for record in self.nb.dcim.interfaces.filter(device_id=device.id, limit=0):
            data = record.serialize()
            interfaces[data["name"]] = data
            lag = data.get("lag")
            if lag and lag.get("name"):
                lag_membership.setdefault(lag["name"], set()).add(data["name"])

        return DeviceState(
            module_bays=module_bays,
            modules=modules,
            interfaces=interfaces,
            lag_membership=lag_membership,
        )

    @staticmethod
    def _diff(desired: Dict, current: Optional[Dict]) -> Dict:
        if current is None:
            return {k: v for k, v in desired.items() if v is not None}
        diff: Dict = {}
        for key, value in desired.items():
            current_value = current.get(key) if current else None
            if current_value != value:
                diff[key] = value
        return diff

    def _action_and_diff(self, desired: Dict, current: Optional[Dict], exists: bool) -> Tuple[str, Optional[Dict]]:
        if not exists:
            diff = self._diff(desired, None)
            return "create", diff or desired
        diff = self._diff(desired, current)
        if diff:
            return "update", diff
        return "noop", None


class NetboxDeviceBuilder:
    def __init__(self, config: Optional[AppConfig] = None, nb_api: Optional[pynetbox.api.Api] = None) -> None:
        self.config = config or load_app_config()
        self.nb = nb_api or pynetbox.api(self.config.netbox.url, token=self.config.netbox.token)
        self.nb.http_session.verify = self.config.netbox.verify_ssl
        self.proposal_engine = ProposalEngine(self.nb)

    def plan(self, inventory: NormalizedInventory) -> Tuple[ProposalBatch, NormalizedInventory]:
        resolved_inventory = self._apply_suffixes(inventory)
        batch = self.proposal_engine.build(resolved_inventory)
        return batch, resolved_inventory

    def dry_run(self, inventory: NormalizedInventory, save_json: bool = True) -> Tuple[ProposalBatch, Optional[Path], str]:
        batch, resolved_inventory = self.plan(inventory)
        output_path = self._write_proposals(batch, resolved_inventory.device.name) if save_json else None
        summary = self._summarize(batch)
        preflight = self._build_preflight_report(resolved_inventory)
        if preflight:
            summary = f"{summary}\n\n{preflight}"
        logger.info("Dry-run complete for %s", resolved_inventory.device.name)
        return batch, output_path, summary

    def apply(
        self, inventory: NormalizedInventory
    ) -> Tuple[ProposalBatch, ProposalBatch, NormalizedInventory]:
        batch, resolved_inventory = self.plan(inventory)

        dependencies = self._resolve_device_dependencies(resolved_inventory.device)
        device_record = self._apply_device(batch.device, dependencies)

        module_bay_cache: Dict[str, Any] = {}
        for proposal in batch.module_bays:
            record = self._apply_module_bay(device_record, proposal)
            if record:
                module_bay_cache[proposal.identifier] = record

        module_cache: Dict[str, Any] = {}
        for proposal in batch.modules:
            record = self._apply_module(device_record, proposal, dependencies, module_bay_cache)
            if record:
                module_cache[proposal.identifier] = record

        interface_cache: Dict[str, Any] = {}
        for proposal in batch.interfaces:
            record = self._apply_interface(device_record, proposal, interface_cache)
            if record:
                interface_cache[proposal.identifier] = record

        self._apply_lag_membership(device_record, batch.lags)

        post_batch = self.proposal_engine.build(resolved_inventory)
        return batch, post_batch, resolved_inventory

    def summarize(self, batch: ProposalBatch) -> str:
        return self._summarize(batch)

    def _apply_suffixes(self, inventory: NormalizedInventory) -> NormalizedInventory:
        device = inventory.device
        suffix = self.config.netbox.device_name_suffix
        device_name = device.name
        if suffix and not device.name.endswith(suffix):
            device_name = f"{device.name}{suffix}"

        device_type_slug = device.device_type_slug
        type_suffix = self.config.rules.device_type_suffix_value()
        if type_suffix and not device_type_slug.endswith(type_suffix):
            device_type_slug = f"{device_type_slug}{type_suffix}"

        if device_name == device.name and device_type_slug == device.device_type_slug:
            return inventory

        updated_device = replace(device, name=device_name, device_type_slug=device_type_slug)
        return replace(inventory, device=updated_device)

    def _write_proposals(self, batch: ProposalBatch, device_name: str) -> Path:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        filename = f"{slugify(device_name)}_{timestamp}.json"
        output_path = self.config.netbox.proposals_dir / filename
        payload = {
            "device": device_name,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "proposals": batch.to_json(),
        }
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
        return output_path

    def _summarize(self, batch: ProposalBatch) -> str:
        lines = [f"Device -> {batch.device.action.upper()}: {batch.device.identifier}"]
        lines.append(self._summarize_group("Module bays", batch.module_bays))
        lines.append(self._summarize_group("Modules", batch.modules))
        lines.append(self._summarize_group("Interfaces", batch.interfaces))
        lines.append(self._summarize_group("LAGs", batch.lags))
        return "\n".join(lines)

    def _build_preflight_report(self, inventory: NormalizedInventory) -> str:
        issues: List[str] = []

        manufacturer_slug = inventory.device.manufacturer_slug
        manufacturer_endpoint = getattr(self.nb.dcim, "manufacturers", None)
        manufacturer_record = None
        if manufacturer_slug:
            if manufacturer_endpoint:
                manufacturer_record = manufacturer_endpoint.get(slug=manufacturer_slug)
            if not manufacturer_record:
                issues.append(
                    f"Manufacturer slug '{manufacturer_slug}' not found in NetBox."
                )
        else:
            issues.append("Device manufacturer slug is missing; update rules defaults.")

        device_type_slug = inventory.device.device_type_slug
        device_type_endpoint = getattr(self.nb.dcim, "device_types", None)
        device_type = device_type_endpoint.get(slug=device_type_slug) if device_type_endpoint else None
        if not device_type:
            issues.append(f"Device type slug '{device_type_slug}' not found in NetBox.")

        module_models = {
            module.module_type_model
            for module in inventory.modules
            if module.module_type_model
        }
        missing_modules: List[str] = []
        for model in sorted(module_models):
            try:
                self._find_module_type(model, manufacturer_record)
            except ValueError:
                missing_modules.append(model)

        if missing_modules:
            formatted = ", ".join(missing_modules)
            issues.append(f"Module types not found: {formatted}.")

        if not issues:
            return "Preflight: manufacturers, device type, and modules are present."

        lines = ["Preflight issues detected:"]
        lines.extend(f"- {issue}" for issue in issues)
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_device_dependencies(self, device) -> Dict[str, Any]:
        site = self.nb.dcim.sites.get(slug=device.site_slug)
        if not site:
            raise ValueError(f"Site with slug '{device.site_slug}' not found in NetBox")

        role = self.nb.dcim.device_roles.get(slug=device.role_slug)
        if not role:
            raise ValueError(f"Device role with slug '{device.role_slug}' not found in NetBox")

        device_type = self.nb.dcim.device_types.get(slug=device.device_type_slug)
        if not device_type:
            raise ValueError(f"Device type with slug '{device.device_type_slug}' not found in NetBox")

        manufacturer_slug = device.manufacturer_slug
        manufacturer = None
        if manufacturer_slug:
            manufacturer = self.nb.dcim.manufacturers.get(slug=manufacturer_slug)
            if not manufacturer:
                raise ValueError(
                    f"Manufacturer with slug '{manufacturer_slug}' not found in NetBox"
                )
            dt_manufacturer = getattr(device_type, "manufacturer", None)
            if dt_manufacturer and getattr(dt_manufacturer, "slug", None) != manufacturer_slug:
                raise ValueError(
                    "Configured manufacturer slug does not match the device type manufacturer"
                )

        return {
            "site": site,
            "role": role,
            "device_type": device_type,
            "manufacturer": manufacturer,
        }

    def _apply_device(self, proposal: Proposal, dependencies: Dict[str, Any]):
        existing = self.nb.dcim.devices.get(name=proposal.identifier)

        if proposal.action == "create":
            payload = self._build_device_payload(proposal.desired, dependencies)
            device = self.nb.dcim.devices.create(payload)
            logger.info("Created device %s", proposal.identifier)
            return device

        if not existing:
            raise ValueError(f"Device '{proposal.identifier}' expected to exist but was not found")

        if proposal.action == "update":
            payload = self._build_device_payload(proposal.diff or {}, dependencies)
            if payload:
                existing = existing.update(payload)
                logger.info("Updated device %s", proposal.identifier)
        return existing

    def _apply_module_bay(self, device, proposal: Proposal):
        existing = self.nb.dcim.module_bays.get(device_id=device.id, name=proposal.identifier)
        if proposal.action == "create":
            create_payload = {
                key: value
                for key, value in proposal.desired.items()
                if key in {"name", "label", "position"} and value is not None
            }
            data = {"device": device.id, **create_payload}
            record = self.nb.dcim.module_bays.create(data)
            logger.info("Created module bay %s", proposal.identifier)
            return record

        if not existing:
            if proposal.action == "noop":
                return None
            raise ValueError(f"Module bay '{proposal.identifier}' not found for device {device.name}")

        if proposal.action == "update":
            diff = proposal.diff or {}
            update_payload = {key: diff[key] for key in diff if key in {"name", "label", "position"}}
            if update_payload:
                existing = existing.update(update_payload)
                logger.info("Updated module bay %s", proposal.identifier)
        return existing

    def _apply_module(
        self,
        device,
        proposal: Proposal,
        dependencies: Dict[str, Any],
        module_bays: Dict[str, Any],
    ):
        bay_name = proposal.desired.get("bay_name")
        module_type_model = proposal.desired.get("module_type_model")
        module_bay = module_bays.get(bay_name)
        if not module_bay:
            module_bay = self.nb.dcim.module_bays.get(device_id=device.id, name=bay_name)
            if not module_bay:
                raise ValueError(f"Module bay '{bay_name}' not found when applying modules")
            module_bays[bay_name] = module_bay

        manufacturer = dependencies.get("manufacturer")
        module_type = None
        needs_module_type = proposal.action == "create" or (
            (proposal.diff or {}).get("module_type_model") is not None
        )
        if needs_module_type:
            module_type = self._find_module_type(module_type_model, manufacturer)

        existing = None
        module_identifier = proposal.identifier
        existing_modules = list(
            self.nb.dcim.modules.filter(device_id=device.id, module_bay_id=module_bay.id, limit=1)
        )
        if existing_modules:
            existing = existing_modules[0]

        if proposal.action == "create":
            if not module_type:
                module_type = self._find_module_type(module_type_model, manufacturer)
            payload = {
                "device": device.id,
                "module_bay": module_bay.id,
                "module_type": module_type.id,
            }
            if proposal.desired.get("status"):
                payload["status"] = proposal.desired["status"]
            if proposal.desired.get("serial") is not None:
                payload["serial"] = proposal.desired.get("serial")
            record = self.nb.dcim.modules.create(payload)
            logger.info("Created module %s", module_identifier)
            return record

        if not existing:
            if proposal.action == "noop":
                return None
            raise ValueError(f"Module '{module_identifier}' not found for device {device.name}")

        if proposal.action == "update":
            update_payload = {}
            for key, value in (proposal.diff or {}).items():
                if key == "module_type_model":
                    if not module_type:
                        module_type = self._find_module_type(module_type_model, manufacturer)
                    update_payload["module_type"] = module_type.id
                elif key == "status":
                    update_payload["status"] = value
                elif key == "serial":
                    update_payload["serial"] = value
            if update_payload:
                existing = existing.update(update_payload)
                logger.info("Updated module %s", module_identifier)
        return existing

    def _apply_interface(self, device, proposal: Proposal, cache: Dict[str, Any]):
        desired = proposal.desired
        existing = self.nb.dcim.interfaces.get(device_id=device.id, name=proposal.identifier)

        def to_payload(source: Dict[str, Any], include_identity: bool) -> Dict[str, Any]:
            payload: Dict[str, Any] = {}
            if include_identity:
                payload["device"] = device.id
                payload["name"] = proposal.identifier
            if "type" in source:
                payload["type"] = source["type"]
            if "enabled" in source:
                payload["enabled"] = source["enabled"]
            if "description" in source:
                payload["description"] = source["description"]
            if "lag" in source:
                lag_name = source["lag"]
                if lag_name:
                    lag_iface = cache.get(lag_name) or self.nb.dcim.interfaces.get(
                        device_id=device.id, name=lag_name
                    )
                    if not lag_iface:
                        raise ValueError(f"Referenced LAG '{lag_name}' not found for {proposal.identifier}")
                    cache[lag_name] = lag_iface
                    payload["lag"] = lag_iface.id
                else:
                    payload["lag"] = None
            return payload

        if proposal.action == "create":
            payload = to_payload(desired, include_identity=True)
            record = self.nb.dcim.interfaces.create(payload)
            logger.info("Created interface %s", proposal.identifier)
            return record

        if not existing:
            if proposal.action == "noop":
                return None
            raise ValueError(f"Interface '{proposal.identifier}' not found on device {device.name}")

        if proposal.action == "update":
            payload = to_payload(proposal.diff or {}, include_identity=False)
            if payload:
                existing = existing.update(payload)
                logger.info("Updated interface %s", proposal.identifier)
        return existing

    def _apply_lag_membership(self, device, lag_proposals: Iterable[Proposal]):
        for proposal in lag_proposals:
            if proposal.action == "noop":
                continue
            desired_members = proposal.desired.get("members", [])
            lag_name = proposal.identifier
            lag_iface = self.nb.dcim.interfaces.get(device_id=device.id, name=lag_name)
            if not lag_iface:
                raise ValueError(f"LAG interface '{lag_name}' not found when setting membership")
            lag_id = lag_iface.id

            current_members = {
                record.name
                for record in self.nb.dcim.interfaces.filter(
                    device_id=device.id, lag_id=lag_id, limit=0
                )
            }

            desired_set = set(desired_members)

            to_add = desired_set - current_members
            to_remove = current_members - desired_set

            for member in sorted(to_add):
                iface = self.nb.dcim.interfaces.get(device_id=device.id, name=member)
                if not iface:
                    raise ValueError(f"Interface '{member}' not found while adding to {lag_name}")
                iface.update({"lag": lag_id})
                logger.info("Added %s to %s", member, lag_name)

            for member in sorted(to_remove):
                iface = self.nb.dcim.interfaces.get(device_id=device.id, name=member)
                if not iface:
                    continue
                iface.update({"lag": None})
                logger.info("Removed %s from %s", member, lag_name)

    def _find_module_type(self, model: Optional[str], manufacturer):
        if not model:
            raise ValueError("Module type model is required")
        candidates = list(self.nb.dcim.module_types.filter(model=model, limit=5))
        if manufacturer:
            manufacturer_id = getattr(manufacturer, "id", None)
            candidates = [c for c in candidates if getattr(getattr(c, "manufacturer", None), "id", None) == manufacturer_id]
        if not candidates:
            raise ValueError(f"Module type with model '{model}' not found in NetBox")
        if len(candidates) > 1:
            logger.warning("Multiple module types matched model '%s', using first match", model)
        return candidates[0]

    def _build_device_payload(self, data: Dict[str, Any], dependencies: Dict[str, Any]) -> Dict[str, Any]:
        payload: Dict[str, Any] = {}
        if "name" in data:
            payload["name"] = data["name"]
        if "status" in data:
            payload["status"] = data["status"]
        if "site" in data:
            payload["site"] = dependencies["site"].id
        if "role" in data:
            payload["role"] = dependencies["role"].id
        if "device_type" in data:
            payload["device_type"] = dependencies["device_type"].id
        if "serial" in data:
            payload["serial"] = data["serial"]
        if "asset_tag" in data:
            payload["asset_tag"] = data["asset_tag"]
        if "tags" in data:
            payload["tags"] = data["tags"]
        if "custom_fields" in data:
            payload["custom_fields"] = data["custom_fields"]
        return payload

    @staticmethod
    def _summarize_group(label: str, proposals: Iterable[Proposal]) -> str:
        proposals = list(proposals)
        if not proposals:
            return f"{label}: none"
        counts = NetboxDeviceBuilder._count_actions(proposals)
        summary = ", ".join(f"{action}={count}" for action, count in counts.items())
        created = [p.identifier for p in proposals if p.action == "create"]
        if created:
            preview = ", ".join(created[:3])
            if len(created) > 3:
                preview += ", …"
            summary = f"{summary} | create: {preview}"
        return f"{label}: {summary}"

    @staticmethod
    def _count_actions(proposals: Iterable[Proposal]) -> Dict[str, int]:
        counts: Dict[str, int] = {"create": 0, "update": 0, "noop": 0}
        for proposal in proposals:
            counts[proposal.action] = counts.get(proposal.action, 0) + 1
        return {k: v for k, v in counts.items() if v}
