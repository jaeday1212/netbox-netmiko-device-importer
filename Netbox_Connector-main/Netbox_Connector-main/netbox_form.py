from extras.scripts import Script, IPAddressVar, ChoiceVar, StringVar, BooleanVar
from django.forms import PasswordInput

from config_loader import load_app_config
from Netmiko_ssh_handler import NetmikoDataCollector
from netbox_devices_full import NetboxDeviceBuilder


class CreateNetmikoTest(Script):
    class Meta:
        name = "Netmiko Device Sync"
        description = "Collect device data over SSH and sync with NetBox"
        commit_default = False
        field_order = ["ip", "device_os", "username", "password", "update_existing"]

    ip = IPAddressVar(description="Device IP address", label="IP")
    device_os = ChoiceVar(
        choices=[
            (netmiko_type, netmiko_type)
            for type_list in NetmikoDataCollector.device_type_alias.values()
            for netmiko_type in type_list
        ],
        label="Device OS",
    )
    username = StringVar(description="SSH username")
    password = StringVar(description="SSH password", widget=PasswordInput)
    update_existing = BooleanVar(
        description="Allow updates to existing NetBox objects",
        default=False,
    )

    def run(self, data, commit):
        ip = data["ip"].compressed if hasattr(data["ip"], "compressed") else data["ip"]
        username = data["username"]
        password = data["password"]
        device_os = data["device_os"]

        self.log_info("Connecting...")

        try:
            config = load_app_config()
        except Exception as failure:
            self.log_failure(f"Configuration error: {failure}")
            return

        collector = None
        try:
            ssh_connect = NetmikoDataCollector.build_ssh_config(ip, username, password, device_os)
            collector = NetmikoDataCollector(ssh_connect, rules=config.rules)
            collector.connect_or_fail()
            inventory = collector.harvest()
            self.log_success(f"Successfully harvested data for {inventory.device.name}")
        except Exception as failure:
            self.log_failure(f"Collection error: {failure}")
            return
        finally:
            if collector is not None:
                try:
                    collector.disconnect()
                except Exception:
                    pass

        builder = NetboxDeviceBuilder(config=config)

        try:
            dry_batch, proposal_path, summary = builder.dry_run(inventory)
        except Exception as failure:
            self.log_failure(f"Dry-run failed: {failure}")
            return

        self.log_info("Dry-run summary:")
        for line in summary.splitlines():
            self.log_info(line)
        if proposal_path:
            self.log_info(f"Proposal saved to {proposal_path}")

        if not commit:
            self.log_info("Commit is disabled. Proposals generated only.")
            return

        allow_updates = data.get("update_existing", False)
        updates_detected = any(action.action == "update" for action in dry_batch.actions())
        if updates_detected and not allow_updates:
            self.log_failure("Updates detected but 'Update existing' is disabled. Aborting apply.")
            return

        try:
            before_batch, after_batch, _ = builder.apply(inventory)
        except Exception as failure:
            self.log_failure(f"Apply failed: {failure}")
            return

        self.log_info("Applied changes:")
        for line in builder.summarize(before_batch).splitlines():
            self.log_info(line)

        self.log_info("Post-apply verification:")
        for line in builder.summarize(after_batch).splitlines():
            self.log_info(line)
        self.log_success("NetBox synchronization complete.")
