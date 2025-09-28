import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

try:
    from netbox_connector.connector_cli import (  # type: ignore[import-not-found]
        _build_fake_netbox_api,
        _build_sample_inventory,
    )
    from netbox_connector.config_loader import load_app_config  # type: ignore[import-not-found]
    from netbox_connector.netbox_devices_full import NetboxDeviceBuilder  # type: ignore[import-not-found]
except ModuleNotFoundError:  # pragma: no cover - fallback for legacy layout
    from connector_cli import _build_fake_netbox_api, _build_sample_inventory
    from config_loader import load_app_config
    from netbox_devices_full import NetboxDeviceBuilder


def _with_token_env():
    original = os.environ.get("NETBOX_TOKEN")
    os.environ["NETBOX_TOKEN"] = original or "dummy-token"
    return original


def _restore_token_env(original):
    if original is None:
        os.environ.pop("NETBOX_TOKEN", None)
    else:
        os.environ["NETBOX_TOKEN"] = original


def test_fake_netbox_api_supports_basic_lifecycle():
    api = _build_fake_netbox_api()

    payload = {"name": "test-device"}
    created = api.dcim.devices.create(payload)

    assert created.id == 1
    assert created.serialize() == payload

    updated = created.update({"status": "active"})
    assert updated.serialize()["status"] == "active"

    # filter/get are no-ops in the fake implementation
    assert api.dcim.devices.filter(name="test-device") == []
    assert api.dcim.devices.get(name="test-device") is None
    assert api.http_session.verify is True


def test_build_sample_inventory_respects_rules():
    original_token = _with_token_env()
    try:
        config = load_app_config()
    finally:
        _restore_token_env(original_token)

    inventory = _build_sample_inventory(config)

    assert inventory.device.name == "SIM-SROS-01"
    assert inventory.device.site_slug == "sim"
    assert inventory.device.role_slug == "access-switch"
    assert inventory.device.manufacturer_slug == "nokia"
    assert inventory.device.device_type_slug == "nokia-7750-sr"

    lag_names = {lag.name for lag in inventory.lags}
    assert "LAG 1" in lag_names

    interface_types = {iface.name: iface.type_slug for iface in inventory.interfaces}
    assert interface_types["LAG 1"] == "lag"
    assert interface_types["1/1/2"] == "other"

    lag_member_lookup = {lag.name: set(lag.members) for lag in inventory.lags}
    assert lag_member_lookup["LAG 1"] == {"1/1/1"}


def test_load_app_config_allows_missing_token_when_flagged():
    original = os.environ.pop("NETBOX_TOKEN", None)
    try:
        config = load_app_config(allow_missing_token=True)
    finally:
        if original is None:
            os.environ.pop("NETBOX_TOKEN", None)
        else:
            os.environ["NETBOX_TOKEN"] = original

    assert config.netbox.token == "SIMULATED-TOKEN"


def test_device_type_slug_falls_back_to_slugify():
    original = _with_token_env()
    try:
        config = load_app_config()
    finally:
        _restore_token_env(original)

    fallback_value = config.rules.device_type_slug("Unknown Model 5000")
    assert fallback_value == "unknown-model-5000"


def test_dry_run_includes_preflight_report(tmp_path):
    original = _with_token_env()
    try:
        config = load_app_config()
    finally:
        _restore_token_env(original)

    config.netbox.proposals_dir = tmp_path
    builder = NetboxDeviceBuilder(config=config, nb_api=_build_fake_netbox_api())
    inventory = _build_sample_inventory(config)

    batch, output_path, summary = builder.dry_run(inventory, save_json=False)

    assert batch.device.identifier == inventory.device.name
    assert output_path is None
    assert "Preflight issues detected:" in summary
    assert "Manufacturer slug 'nokia'" in summary
    assert "Module types not found" in summary
