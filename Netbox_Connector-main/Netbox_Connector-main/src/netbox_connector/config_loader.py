from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Optional

import yaml


PACKAGE_ROOT = Path(__file__).parent
DEFAULT_SETTINGS_PATH = PACKAGE_ROOT / "settings.yaml"
DEFAULT_RULES_PATH = PACKAGE_ROOT / "rules.yaml"
EXAMPLE_SETTINGS_PATH = PACKAGE_ROOT / "settings.example.yaml"


__all__ = [
    "PACKAGE_ROOT",
    "DEFAULT_SETTINGS_PATH",
    "DEFAULT_RULES_PATH",
    "EXAMPLE_SETTINGS_PATH",
    "NetBoxConfig",
    "RegexRule",
    "RulesEngine",
    "AppConfig",
    "load_yaml",
    "resolve_settings_path",
    "load_app_config",
]


@dataclass(slots=True)
class NetBoxConfig:
    url: str
    token: str
    verify_ssl: bool = True
    device_name_suffix: str = ""
    proposals_dir: Path = Path("proposals")


class RegexRule:
    def __init__(
        self,
        pattern: str,
        value: Optional[str] = None,
        template: Optional[str] = None,
        transform: Optional[str] = None,
    ) -> None:
        self.pattern = re.compile(pattern)
        self.value = value
        self.template = template
        self.transform = transform

    def apply(self, candidate: str) -> Optional[str]:
        match = self.pattern.search(candidate)
        if not match:
            return None
        if self.value is not None:
            result = self.value
        elif self.template is not None:
            groups = match.groupdict()
            if groups:
                result = self.template.format(**groups)
            else:
                result = self.template.format(*match.groups())
        else:
            result = None

        if result is None:
            return None

        if self.transform == "lower":
            result = result.lower()
        elif self.transform == "upper":
            result = result.upper()

        return result


class RulesEngine:
    def __init__(
        self,
        role_rules: Iterable[RegexRule],
        site_rules: Iterable[RegexRule],
        manufacturer_rules: Iterable[RegexRule],
        device_type_rules: Iterable[RegexRule],
        interface_rules: Dict[str, str],
        device_type_suffix: Dict[str, str],
        defaults: Dict[str, Optional[str]],
    ) -> None:
        self.role_rules = list(role_rules)
        self.site_rules = list(site_rules)
        self.manufacturer_rules = list(manufacturer_rules)
        self.device_type_rules = list(device_type_rules)
        self.interface_rules = interface_rules
        self.device_type_suffix = device_type_suffix
        self.defaults = defaults

    def role_slug(self, host_name: str) -> str:
        return self._apply_rules(host_name, self.role_rules, "role_slug")

    def site_slug(self, host_name: str) -> str:
        return self._apply_rules(host_name, self.site_rules, "site_slug")

    def manufacturer_slug(self, hostname: str) -> str:
        return self._apply_rules(hostname, self.manufacturer_rules, "manufacturer_slug")

    def device_type_slug(self, device_type: str) -> str:
        for rule in self.device_type_rules:
            value = rule.apply(device_type)
            if value:
                return value
        default_value = self.defaults.get("device_type_slug")
        if default_value:
            return default_value
        return self._slugify(device_type)

    def device_type_suffix_value(self) -> str:
        enabled = self.device_type_suffix.get("enabled", False)
        if not enabled:
            return ""
        return self.device_type_suffix.get("value", "")

    def interface_type(self, interface_name: str, is_lag: bool = False) -> str:
        if is_lag:
            return self.interface_rules.get("lag_default", "lag")
        matches = self.interface_rules.get("matches", [])
        for matcher in matches:
            pattern = matcher.get("pattern")
            iface_type = matcher.get("type")
            if pattern and iface_type and re.search(pattern, interface_name):
                return iface_type
        return self.interface_rules.get("physical_default", "other")

    def _apply_rules(self, host_name: str, rules: Iterable[RegexRule], default_key: str) -> str:
        for rule in rules:
            value = rule.apply(host_name)
            if value:
                return value
        default_value = self.defaults.get(default_key)
        if default_value:
            return default_value
        raise ValueError(f"No rule matched for {default_key} and no default configured")

    @staticmethod
    def _slugify(value: str) -> str:
        value = value.strip().lower()
        value = re.sub(r"[^a-z0-9]+", "-", value)
        return value.strip("-")


@dataclass(slots=True)
class AppConfig:
    settings_path: Path
    rules_path: Path
    netbox: NetBoxConfig
    rules: RulesEngine


def load_yaml(path: Path) -> Dict:
    if not path.exists():
        raise FileNotFoundError(f"Configuration file not found: {path}")
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def resolve_settings_path(candidate: Optional[Path]) -> Path:
    if candidate:
        return candidate
    if DEFAULT_SETTINGS_PATH.exists():
        return DEFAULT_SETTINGS_PATH
    if EXAMPLE_SETTINGS_PATH.exists():
        return EXAMPLE_SETTINGS_PATH
    raise FileNotFoundError(
        "No settings.yaml found. Copy settings.example.yaml and customise it for your environment."
    )


def load_app_config(
    settings_path: Optional[Path] = None,
    rules_path: Optional[Path] = None,
    allow_missing_token: bool = False,
) -> AppConfig:
    settings_path = resolve_settings_path(settings_path)
    rules_path = rules_path or DEFAULT_RULES_PATH

    settings_data = load_yaml(settings_path)
    rules_data = load_yaml(rules_path)

    netbox_section = settings_data.get("netbox", {})

    token_env = netbox_section.get("token_env") or "NETBOX_TOKEN"
    token = os.environ.get(token_env)
    if not token:
        token = netbox_section.get("token")
    if not token:
        if allow_missing_token:
            token = "SIMULATED-TOKEN"
        else:
            raise ValueError(
                "NetBox API token must be provided via environment variable or settings file"
            )

    proposals_dir = Path(netbox_section.get("proposals_dir", "proposals"))
    proposals_dir.mkdir(parents=True, exist_ok=True)

    netbox_config = NetBoxConfig(
        url=netbox_section.get("url", "https://netbox.example.com"),
        token=token,
        verify_ssl=netbox_section.get("verify_ssl", True),
        device_name_suffix=netbox_section.get("device_name_suffix", ""),
        proposals_dir=proposals_dir,
    )

    defaults = {
        "role_slug": rules_data.get("defaults", {}).get("role_slug"),
        "site_slug": rules_data.get("defaults", {}).get("site_slug"),
        "manufacturer_slug": rules_data.get("defaults", {}).get("manufacturer_slug"),
        "device_type_slug": rules_data.get("defaults", {}).get("device_type_slug"),
    }

    role_rules = _build_regex_rules(rules_data.get("roles", []))
    site_rules = _build_regex_rules(rules_data.get("sites", []))
    manufacturer_rules = _build_regex_rules(rules_data.get("manufacturers", []))
    device_type_rules = _build_regex_rules(rules_data.get("device_types", []))

    interface_rules = rules_data.get(
        "interface_types",
        {
            "physical_default": "other",
            "lag_default": "lag",
            "matches": [],
        },
    )

    device_type_suffix = rules_data.get(
        "device_type_suffix", {"enabled": False, "value": ""}
    )

    rule_engine = RulesEngine(
        role_rules=role_rules,
        site_rules=site_rules,
        manufacturer_rules=manufacturer_rules,
        device_type_rules=device_type_rules,
        interface_rules=interface_rules,
        device_type_suffix=device_type_suffix,
        defaults=defaults,
    )

    return AppConfig(
        settings_path=settings_path,
        rules_path=rules_path,
        netbox=netbox_config,
        rules=rule_engine,
    )


def _build_regex_rules(data: Iterable[Dict]) -> Iterable[RegexRule]:
    for entry in data:
        pattern = entry.get("pattern")
        value = entry.get("slug") or entry.get("value")
        template = entry.get("slug_format")
        transform = entry.get("transform")
        if not pattern:
            continue
        yield RegexRule(pattern=pattern, value=value, template=template, transform=transform)
