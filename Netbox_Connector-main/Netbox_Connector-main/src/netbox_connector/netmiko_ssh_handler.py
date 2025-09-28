from __future__ import annotations

import re
from typing import Dict, List, Optional, Set

from netmiko import ConnectHandler, NetmikoAuthenticationException, NetmikoTimeoutException

from .config_loader import RulesEngine
from .models import (
    NormalizedDevice,
    NormalizedInterface,
    NormalizedInventory,
    NormalizedLag,
    NormalizedModule,
    NormalizedModuleBay,
)


__all__ = ["NetmikoDataCollector"]


class NetmikoDataCollector:
    """Collects device data over SSH using Netmiko."""

    device_type_alias = {
        # Nokia - SR OS (SROS)
        "NokiaSrosSSH": ["nokia_sros"],

        # Fortinet - FortiOS
        "FortinetSSH": ["fortinet"],

        # HPE / Aruba - Various OSes
        "HpeComwareSSH": ["hp_comware"],
        "HpeProcurveSSH": ["hp_procurve"],
        "ArubaOsSSH": ["aruba_os"],
        "ArubaCxSSH": ["aruba_os_cx"],

        # Adtran - AOS
        "AdtranOsSSH": ["adtran_os", "adtran_aos"],

        # MikroTik - RouterOS / SwOS
        "MikrotikRouterOsSSH": ["mikrotik_routeros"],
        "MikrotikSwOsSSH": ["mikrotik_swos"],

        # Ubiquiti - EdgeOS / UniFi
        "UbiquitiEdgeSSH": ["ubiquiti_edge", "ubiquiti_edgeswitch", "ubiquiti_edgemax"],
        "UbiquitiUnifiSSH": ["ubiquiti_unifi", "unifi_os"],
    }

    @classmethod
    def build_ssh_config(cls, ip: str, username: str, password: str, device_os: str) -> Dict[str, str]:
        """Return a Netmiko connection dictionary."""
        return {
            "device_type": device_os,
            "ip": ip,
            "username": username,
            "password": password,
        }

    def __init__(self, ssh_connect: Dict[str, str], rules: Optional[RulesEngine] = None) -> None:
        self.ssh_connect = ssh_connect
        self.conn = None
        self.device_alias = self.ssh_connect["device_type"]
        self.host_name: Optional[str] = None
        self.device_type: Optional[str] = None
        self.rules = rules

    def _ensure_connect(self) -> None:
        if not self.conn:
            raise ConnectionError("Device not yet connected")

    def connect_or_fail(self) -> None:
        try:
            self.conn = ConnectHandler(**self.ssh_connect)
        except (NetmikoTimeoutException, NetmikoAuthenticationException) as exc:
            raise ConnectionError(
                f"[ERROR] Could not connect to device {self.ssh_connect['ip']}: {exc}"
            ) from exc

    def disconnect(self) -> None:
        if self.conn:
            self.conn.disconnect()
            self.conn = None
            self.host_name = None
            self.device_type = None

    def harvest(self) -> NormalizedInventory:
        """Collect device facts and return a NormalizedInventory model."""
        self._ensure_connect()
        if self.rules is None:
            raise ValueError("RulesEngine is required to build normalized inventory")

        if self.device_alias == "nokia_sros":
            return self._harvest_nokia_sros()

        raise ValueError(f"Unsupported OS: {self.device_alias}")

    def _harvest_nokia_sros(self) -> NormalizedInventory:
        assert self.conn is not None

        chassis_detail = self.conn.send_command("show system information")
        ports_raw = self.conn.send_command("show port description")
        card_detail = self.conn.send_command("show card detail")
        cli_output_mda = self.conn.send_command("show mda")
        show_card = self.conn.send_command("show card")
        find_lag = self.conn.send_command("show port")

        host_name = self._extract_first(r".*System Name.*: (.*)", chassis_detail)
        device_type_raw = self._extract_first(r".*System Type.*: (.*)", chassis_detail)

        self.host_name = host_name
        self.device_type = device_type_raw

        serial_numbers = re.findall(
            r"CLEI code\s+\S+\s+\S+\s+\S+\s+\S+\s+\S+\s+(\S+)",
            card_detail,
        )

        device = NormalizedDevice(
            name=host_name,
            site_slug=self.rules.site_slug(host_name),
            role_slug=self.rules.role_slug(host_name),
            manufacturer_slug=self.rules.manufacturer_slug(host_name),
            device_type_slug=self.rules.device_type_slug(device_type_raw),
            serial=serial_numbers[0] if serial_numbers else None,
        )

        mda_matches = re.findall(r"(?m)^\s*(\d+)\s+\d+\s+([^\s:]+)?", cli_output_mda)
        mda_bays: List[str] = []
        mda_modules: List[tuple[str, str]] = []
        for slot, descriptor in mda_matches:
            bay_name = f"MDA {slot}"
            mda_bays.append(bay_name)
            module_model = descriptor or f"mda-slot-{slot}"
            mda_modules.append((bay_name, module_model))

        card_names = re.findall(r"(Card\s+[A-Fa-f1-9])", card_detail)
        card_types = re.findall(r"[1234ABCD]\s+(\S+)", show_card)
        card_modules = list(zip(card_names, card_types))

        module_bay_matches = re.findall(r"(Card\s+[A-Fa-f1-4])", card_detail)
        card_pattern = re.compile(
            r"(^[\d \w.]+ (\w+-\d*\S*) *up|^[\d \w.]+.not provisioned.*\n\W*(\S*))",
            re.MULTILINE,
        )
        provisioned_type: List[str] = []
        for match in card_pattern.finditer(card_detail):
            provisioned_type.append(match.group(3) if match.group(3) else match.group(2))
        module_pairs = list(zip(module_bay_matches, provisioned_type))

        ports_up = re.findall(r"(?i)(\d/\d/.*/\d)\s{4,}(to[\s\-_].*)", ports_raw)
        regex_lag = re.findall(
            r"(\d\S+)\s+(?:Up)\s+(?:Yes)\s+(?:Up|Link\s+Up)\s+\d+\s+\d+\s+(\d+)",
            find_lag,
        )

        module_bay_names = set()
        module_bay_names.update(name for name, _ in module_pairs)
        module_bay_names.update(name for name, _ in card_modules)
        module_bay_names.update(mda_bays)

        module_bays = [
            NormalizedModuleBay(name=name, label=name, position=self._bay_position(name))
            for name in sorted(module_bay_names)
        ]

        module_entries: Dict[tuple[str, str], NormalizedModule] = {}

        def add_module(bay_name: str, model: Optional[str]) -> None:
            if not bay_name or not model:
                return
            key = (bay_name, model)
            if key not in module_entries:
                module_entries[key] = NormalizedModule(bay_name=bay_name, module_type_model=model)

        for bay_name, model in module_pairs:
            add_module(bay_name, model)
        for bay_name, model in card_modules:
            add_module(bay_name, model)
        for bay_name, model in mda_modules:
            add_module(bay_name, model)

        modules = list(module_entries.values())

        desc_map: Dict[str, Optional[str]] = {
            port: desc.strip() if desc and desc.strip() else None for port, desc in ports_up
        }

        lag_members: Dict[str, Set[str]] = {}
        for port, lag_id in regex_lag:
            lag_name = f"LAG {lag_id}"
            lag_members.setdefault(lag_name, set()).add(port)

        interfaces: List[NormalizedInterface] = []
        for lag_name in sorted(lag_members.keys()):
            interfaces.append(
                NormalizedInterface(
                    name=lag_name,
                    type_slug=self.rules.interface_type(lag_name, is_lag=True),
                    enabled=True,
                    description=None,
                )
            )

        ports_seen = set(desc_map.keys())
        for members in lag_members.values():
            ports_seen.update(members)

        for port_name in sorted(ports_seen):
            lag_name = self._find_lag_for_port(port_name, lag_members)
            interfaces.append(
                NormalizedInterface(
                    name=port_name,
                    type_slug=self.rules.interface_type(port_name, is_lag=False),
                    enabled=True,
                    description=desc_map.get(port_name),
                    lag=lag_name,
                )
            )
            if lag_name:
                lag_members[lag_name].add(port_name)

        lags = [
            NormalizedLag(name=lag_name, description=None, members=sorted(members))
            for lag_name, members in sorted(lag_members.items())
        ]

        return NormalizedInventory(
            device=device,
            module_bays=module_bays,
            modules=modules,
            interfaces=interfaces,
            lags=lags,
        )

    @staticmethod
    def _find_lag_for_port(port_name: str, lag_members: Dict[str, Set[str]]) -> Optional[str]:
        for lag_name, members in lag_members.items():
            if port_name in members:
                return lag_name
        return None

    @staticmethod
    def _extract_first(pattern: str, text: str) -> str:
        match = re.search(pattern, text, re.MULTILINE)
        if not match:
            raise ValueError(f"Pattern '{pattern}' not found in device output")
        return match.group(1).strip()

    @staticmethod
    def _bay_position(name: str) -> Optional[str]:
        if not name:
            return None
        return name.split()[-1]
