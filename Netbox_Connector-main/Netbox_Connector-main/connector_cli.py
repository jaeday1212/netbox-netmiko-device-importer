from __future__ import annotations

import argparse
import getpass
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Iterable, List

from config_loader import load_app_config
from netbox_devices_full import NetboxDeviceBuilder
from Netmiko_ssh_handler import NetmikoDataCollector
from models import (
    NormalizedDevice,
    NormalizedInterface,
    NormalizedInventory,
    NormalizedLag,
    NormalizedModule,
    NormalizedModuleBay,
)


log = logging.getLogger(__name__)


def device_os_choices() -> Iterable[str]:
    choices = set()
    for aliases in NetmikoDataCollector.device_type_alias.values():
        choices.update(aliases)
    return sorted(choices)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect device facts with Netmiko and sync them with NetBox."
    )
    parser.add_argument("ip", nargs="?", help="Device IP address")
    parser.add_argument(
        "device_os",
        nargs="?",
        choices=device_os_choices(),
        help="Netmiko device type",
    )
    parser.add_argument("username", nargs="?", help="SSH username")
    parser.add_argument(
        "--password",
        help="SSH password (omit to prompt interactively)",
        default=None,
    )
    parser.add_argument(
        "--settings",
        type=Path,
        help="Path to settings YAML (defaults to settings.yaml next to this script)",
    )
    parser.add_argument(
        "--rules",
        type=Path,
        help="Path to rules YAML (defaults to rules.yaml next to this script)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply changes to NetBox after reviewing the dry-run",
    )
    parser.add_argument(
        "--update-existing",
        action="store_true",
        help="Allow updates to existing NetBox objects during apply",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity",
    )
    parser.add_argument(
        "--simulate",
        action="store_true",
        help="Generate sample inventory and offline proposals without connecting to devices or NetBox",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO))

    if not args.simulate:
        missing = [name for name, value in {"ip": args.ip, "device_os": args.device_os, "username": args.username}.items() if not value]
        if missing:
            log.error("Missing required arguments: %s", ", ".join(missing))
            return 1

    password = None
    if not args.simulate:
        password = args.password or getpass.getpass(prompt="SSH password: ")

    try:
        config = load_app_config(
            settings_path=args.settings,
            rules_path=args.rules,
            allow_missing_token=args.simulate,
        )
    except Exception as exc:
        log.error("Failed to load configuration: %s", exc)
        return 1

    if args.simulate:
        inventory = _build_sample_inventory(config)
        builder = NetboxDeviceBuilder(config=config, nb_api=_build_fake_netbox_api())
    else:
        ssh_config = NetmikoDataCollector.build_ssh_config(
            args.ip,
            args.username,
            password,
            args.device_os,
        )

        collector = NetmikoDataCollector(ssh_config, rules=config.rules)

        try:
            log.info("Connecting to %s...", args.ip)
            collector.connect_or_fail()
            inventory = collector.harvest()
            log.info("Harvest complete for %s", inventory.device.name)
        except Exception as exc:
            log.error("Data collection failed: %s", exc)
            return 1
        finally:
            try:
                collector.disconnect()
            except Exception:
                pass

        builder = NetboxDeviceBuilder(config=config)

    try:
        batch, proposal_path, summary = builder.dry_run(inventory)
    except Exception as exc:
        log.error("Dry-run failed: %s", exc)
        return 1

    print("\n=== Dry-run summary ===")
    for line in summary.splitlines():
        print(line)
    if proposal_path:
        print(f"Proposals saved to: {proposal_path}")

    if args.simulate:
        print("\nSimulation complete. No NetBox API calls were made.")
        return 0

    if not args.apply:
        print("\nDry-run only. Re-run with --apply to push changes.")
        return 0

    updates_present = any(action.action == "update" for action in batch.actions())
    if updates_present and not args.update_existing:
        print(
            "Updates detected but --update-existing not set. Aborting before applying changes.",
            file=sys.stderr,
        )
        return 1

    try:
        before_batch, after_batch, _ = builder.apply(inventory)
    except Exception as exc:
        log.error("Apply failed: %s", exc)
        return 1

    print("\n=== Applied changes ===")
    for line in builder.summarize(before_batch).splitlines():
        print(line)

    print("\n=== Post-apply verification ===")
    for line in builder.summarize(after_batch).splitlines():
        print(line)

    print("\nNetBox synchronization complete.")
    return 0


class _FakeEndpoint:
    def __init__(self) -> None:
        self._records: List[_FakeRecord] = []

    def get(self, **kwargs):
        return None

    def filter(self, **kwargs):
        return []

    def create(self, payload):
        record = _FakeRecord(payload)
        self._records.append(record)
        return record


class _FakeRecord:
    _next_id = 1

    def __init__(self, data: dict) -> None:
        self._data = data
        self.id = _FakeRecord._next_id
        _FakeRecord._next_id += 1

    def serialize(self):
        return self._data

    def update(self, payload):
        self._data.update(payload)
        return self


def _build_fake_netbox_api():
    endpoints = SimpleNamespace(
        devices=_FakeEndpoint(),
        module_bays=_FakeEndpoint(),
        modules=_FakeEndpoint(),
        interfaces=_FakeEndpoint(),
        manufacturers=_FakeEndpoint(),
        device_types=_FakeEndpoint(),
        module_types=_FakeEndpoint(),
        sites=_FakeEndpoint(),
        device_roles=_FakeEndpoint(),
    )
    api = SimpleNamespace(dcim=endpoints)
    api.http_session = SimpleNamespace(verify=True)
    return api


def _build_sample_inventory(config) -> NormalizedInventory:
    host_name = "SIM-SROS-01"
    device_type_raw = "Nokia 7750 SR-7"
    device = NormalizedDevice(
        name=host_name,
        site_slug=config.rules.site_slug(host_name),
        role_slug=config.rules.role_slug(host_name),
        manufacturer_slug=config.rules.manufacturer_slug(host_name),
        device_type_slug=config.rules.device_type_slug(device_type_raw),
        status="active",
    )

    module_bays = [
        NormalizedModuleBay(name="Card A", label="Card A", position="A"),
        NormalizedModuleBay(name="Card B", label="Card B", position="B"),
    ]

    modules = [
        NormalizedModule(bay_name="Card A", module_type_model="mda-imm-24"),
        NormalizedModule(bay_name="Card B", module_type_model="mda-xc-12"),
    ]

    lag_name = "LAG 1"
    interfaces = [
        NormalizedInterface(
            name=lag_name,
            type_slug=config.rules.interface_type(lag_name, is_lag=True),
            enabled=True,
            description="Simulated uplink", 
        ),
        NormalizedInterface(
            name="1/1/1",
            type_slug=config.rules.interface_type("1/1/1"),
            enabled=True,
            description="Simulated member",
            lag=lag_name,
        ),
        NormalizedInterface(
            name="1/1/2",
            type_slug=config.rules.interface_type("1/1/2"),
            enabled=True,
            description="Access port",
        ),
    ]

    lags = [
        NormalizedLag(name=lag_name, members=["1/1/1"], description="Simulated bundle"),
    ]

    return NormalizedInventory(
        device=device,
        module_bays=module_bays,
        modules=modules,
        interfaces=interfaces,
        lags=lags,
    )


if __name__ == "__main__":
    sys.exit(main())
