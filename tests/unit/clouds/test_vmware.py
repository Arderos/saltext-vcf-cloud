"""Unit tests for the VMware Salt Cloud driver."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from salt.exceptions import SaltCloudSystemExit

import saltext.vcf_cloud.clouds.vmware as cloud


@pytest.fixture(autouse=True)
def loader_globals(monkeypatch):
    """Provide the globals that Salt injects when loading a cloud module."""
    utils = {
        "cloud.bootstrap": MagicMock(return_value=True),
        "cloud.delete_minion_cachedir": MagicMock(),
        "cloud.filter_event": MagicMock(return_value={"name": "vm-01"}),
        "cloud.fire_event": MagicMock(),
    }
    opts = {
        "sock_dir": "/tmp/salt",
        "transport": "zeromq",
        "update_cachedir": False,
    }
    active_provider = SimpleNamespace(value=lambda: "lab-vcenter:vmware")
    monkeypatch.setattr(cloud, "__opts__", opts, raising=False)
    monkeypatch.setattr(cloud, "__utils__", utils, raising=False)
    monkeypatch.setattr(cloud, "__active_provider_name__", active_provider, raising=False)
    return SimpleNamespace(opts=opts, utils=utils)


def test_virtual_loads_when_provider_and_dependencies_are_available(monkeypatch):
    monkeypatch.setattr(cloud, "get_configured_provider", lambda: {"url": "vc.example"})
    monkeypatch.setattr(cloud, "get_dependencies", lambda: True)

    assert cloud.__virtual__() == "vmware"


@pytest.mark.parametrize("missing", ["provider", "dependencies"])
def test_virtual_refuses_missing_requirements(monkeypatch, missing):
    monkeypatch.setattr(
        cloud, "get_configured_provider", lambda: False if missing == "provider" else {}
    )
    monkeypatch.setattr(cloud, "get_dependencies", lambda: missing != "dependencies")

    assert cloud.__virtual__() is False


def test_active_provider_name_supports_salt_wrapper_and_plain_string(monkeypatch):
    assert cloud._get_active_provider_name() == "lab-vcenter:vmware"

    monkeypatch.setattr(cloud, "__active_provider_name__", "other:vmware")
    assert cloud._get_active_provider_name() == "other:vmware"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, True),
        (False, False),
        ("true", True),
        ("YES", True),
        ("off", False),
        (0, False),
        (1, True),
    ],
)
def test_as_bool(value, expected):
    assert cloud._as_bool(value) is expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, None), (4096, 4096), ("512MB", 512), ("1.5 GB", 1536), ("2048", 2048)],
)
def test_memory_mb(value, expected):
    assert cloud._memory_mb(value) == expected


def test_memory_mb_rejects_invalid_value():
    with pytest.raises(SaltCloudSystemExit, match="Invalid memory value"):
        cloud._memory_mb("four gigabytes")


def test_vcf_opts_translates_provider_configuration(monkeypatch):
    values = {
        "url": "https://vc.example.test/sdk",
        "protocol": "https",
        "port": 8443,
        "user": "svc-cloud",
        "password": "not-returned",
        "verify_ssl": "false",
        "vcf_timeout": "45",
    }
    monkeypatch.setattr(
        cloud, "_provider_value", lambda name, default=None: values.get(name, default)
    )

    assert cloud._vcf_opts() == {
        "saltext.vcf": {
            "vcenter": {
                "host": "vc.example.test:8443",
                "username": "svc-cloud",
                "password": "not-returned",
                "verify_ssl": False,
                "timeout": 45,
            }
        }
    }


def test_vcf_opts_rejects_non_https(monkeypatch):
    values = {"url": "vc.example.test", "protocol": "http"}
    monkeypatch.setattr(
        cloud, "_provider_value", lambda name, default=None: values.get(name, default)
    )

    with pytest.raises(SaltCloudSystemExit, match="requires an HTTPS"):
        cloud._vcf_opts()


def test_vcf_opts_rejects_invalid_url(monkeypatch):
    values = {"url": "https://", "protocol": "https"}
    monkeypatch.setattr(
        cloud, "_provider_value", lambda name, default=None: values.get(name, default)
    )

    with pytest.raises(SaltCloudSystemExit, match="Invalid vCenter URL"):
        cloud._vcf_opts()


def test_find_object_accepts_name_or_moid(monkeypatch):
    first = SimpleNamespace(_moId="vm-101")
    second = SimpleNamespace(_moId="vm-102")
    items = [
        {"object": first, "name": "first"},
        {"object": second, "name": "second"},
    ]
    monkeypatch.setattr(cloud, "_service_instance", object)
    monkeypatch.setattr(
        cloud.salt_vmware, "get_mors_with_properties", lambda *args, **kwargs: items
    )

    assert cloud._find_object(SimpleNamespace, "first") is first
    assert cloud._find_object(SimpleNamespace, "vm-102") is second
    assert cloud._find_object(SimpleNamespace, "absent", required=False) is None


def test_find_object_rejects_ambiguous_name(monkeypatch):
    items = [
        {"object": SimpleNamespace(_moId="vm-101"), "name": "duplicate"},
        {"object": SimpleNamespace(_moId="vm-102"), "name": "duplicate"},
    ]
    monkeypatch.setattr(cloud, "_service_instance", object)
    monkeypatch.setattr(
        cloud.salt_vmware, "get_mors_with_properties", lambda *args, **kwargs: items
    )

    with pytest.raises(SaltCloudSystemExit, match="More than one"):
        cloud._find_object(SimpleNamespace, "duplicate")


def test_customization_nics_builds_static_and_dhcp_entries():
    devices = {
        "Network adapter 1": {
            "ip": "192.0.2.25",
            "subnet_mask": "255.255.255.0",
            "gateway": "192.0.2.1",
            "dns_servers": ["192.0.2.53"],
        },
        "Network adapter 2": {},
    }

    assert cloud._customization_nics(devices) == [
        {
            "ip": "192.0.2.25",
            "subnet": "255.255.255.0",
            "gateway": ["192.0.2.1"],
            "dns_servers": ["192.0.2.53"],
        },
        {"dhcp": True},
    ]


@pytest.mark.parametrize(
    ("address", "valid"),
    [
        ("192.0.2.10", True),
        ("127.0.0.1", False),
        ("169.254.1.1", False),
        ("2001:db8::1", False),
        ("not-an-address", False),
    ],
)
def test_valid_ipv4(address, valid):
    assert cloud._valid_ipv4(address) is valid


def test_ip_lists_classifies_and_deduplicates_addresses():
    vm_ref = SimpleNamespace(
        guest=SimpleNamespace(
            net=[
                SimpleNamespace(
                    ipAddress=[
                        "10.0.0.10",
                        "10.0.0.10",
                        "8.8.8.8",
                        "127.0.0.1",
                        "fe80::1",
                    ]
                )
            ]
        )
    )

    private_ips, public_ips = cloud._ip_lists(vm_ref)

    assert private_ips == ["10.0.0.10"]
    assert public_ips == ["8.8.8.8"]


def test_configure_disks_resizes_existing_disk(monkeypatch):
    resize = MagicMock(return_value="task-resize")
    wait = MagicMock()
    monkeypatch.setattr(cloud, "_vcf_opts", lambda: {"connection": "opts"})
    monkeypatch.setattr(
        cloud.vim_vm_disk,
        "list_",
        lambda *args: [{"label": "Hard disk 2", "key": 2001, "capacity_bytes": 20 * 1024**3}],
    )
    monkeypatch.setattr(cloud.vim_vm_disk, "resize", resize)
    monkeypatch.setattr(cloud, "_wait_task", wait)

    cloud._configure_disks("vm-01", {"Hard disk 2": {"size": 50}}, timeout=30)

    resize.assert_called_once_with({"connection": "opts"}, "vm-01", 2001, 50.0)
    wait.assert_called_once_with("task-resize", timeout=30)


def test_configure_disks_refuses_to_shrink(monkeypatch):
    monkeypatch.setattr(cloud, "_vcf_opts", dict)
    monkeypatch.setattr(
        cloud.vim_vm_disk,
        "list_",
        lambda *args: [{"label": "Hard disk 2", "key": 2001, "capacity_bytes": 50 * 1024**3}],
    )

    with pytest.raises(SaltCloudSystemExit, match="cannot shrink"):
        cloud._configure_disks("vm-01", {"Hard disk 2": {"size": 20}}, timeout=30)


def test_configure_disks_adds_missing_disk(monkeypatch):
    add = MagicMock(return_value="task-add")
    wait = MagicMock()
    monkeypatch.setattr(cloud, "_vcf_opts", lambda: {"connection": "opts"})
    monkeypatch.setattr(cloud.vim_vm_disk, "list_", lambda *args: [])
    monkeypatch.setattr(cloud.vim_vm_disk, "add", add)
    monkeypatch.setattr(cloud, "_datastore_moid", lambda name: "datastore-42")
    monkeypatch.setattr(cloud, "_wait_task", wait)

    cloud._configure_disks(
        "vm-01",
        {"Hard disk 2": {"size": 50, "datastore": "fast", "thin_provision": "yes"}},
        timeout=30,
    )

    add.assert_called_once_with(
        {"connection": "opts"},
        "vm-01",
        50,
        datastore_moid="datastore-42",
        controller_key=None,
        unit_number=None,
        disk_mode="persistent",
        thin=True,
        eager_scrub=False,
    )
    wait.assert_called_once_with("task-add", timeout=30)


def test_script_uses_native_salt_cloud_renderer(monkeypatch):
    monkeypatch.setattr(
        cloud.config, "get_cloud_config_value", lambda *args, **kwargs: "bootstrap-salt"
    )
    monkeypatch.setattr(
        cloud.salt.utils.cloud, "minion_config", lambda *args: {"master": "master.example"}
    )
    monkeypatch.setattr(
        cloud.salt.utils.cloud, "salt_config_to_yaml", lambda data: "master: master.example"
    )
    os_script = MagicMock(return_value="rendered script")
    monkeypatch.setattr(cloud.salt.utils.cloud, "os_script", os_script)

    assert cloud.script({"name": "vm-01"}) == "rendered script"
    os_script.assert_called_once_with(
        "bootstrap-salt", {"name": "vm-01"}, cloud.__opts__, "master: master.example"
    )


def test_create_clones_configures_and_calls_native_bootstrap(monkeypatch, loader_globals):
    settings = {
        "name": "vm-01.example.test",
        "clonefrom": "ubuntu-template",
        "num_cpus": 4,
        "memory": "8GB",
        "devices": {"network": {"Network adapter 1": {"name": "server-vlan"}}},
        "private_key": "/root/.ssh/salt-cloud",
    }
    source = SimpleNamespace(
        parent=SimpleNamespace(_moId="group-v42"),
        datastore=[SimpleNamespace(_moId="datastore-17")],
    )

    def cfg(name, _vm_definition, default=None, **_kwargs):
        return settings.get(name, default)

    def vm_lookup(name, **_kwargs):
        return source if name == "ubuntu-template" else None

    clone = MagicMock(return_value="task-clone")
    power_on = MagicMock(return_value="task-power")
    wait = MagicMock()
    monkeypatch.setattr(cloud, "_cfg", cfg)
    monkeypatch.setattr(cloud, "_vm", vm_lookup)
    monkeypatch.setattr(cloud, "_vcf_opts", lambda: {"connection": "opts"})
    monkeypatch.setattr(cloud.vim_vm, "clone", clone)
    monkeypatch.setattr(cloud.vim_vm_power, "power_on", power_on)
    monkeypatch.setattr(cloud, "_wait_task", wait)
    monkeypatch.setattr(cloud, "_reconfigure", MagicMock())
    monkeypatch.setattr(cloud, "_configure_networks", MagicMock())
    monkeypatch.setattr(cloud, "_configure_disks", MagicMock())
    monkeypatch.setattr(cloud, "_apply_customization", MagicMock())
    monkeypatch.setattr(cloud, "_wait_for_ip", lambda *args: "192.0.2.25")
    monkeypatch.setattr(
        cloud,
        "show_instance",
        lambda *args, **kwargs: {"id": "vm-123", "name": "vm-01.example.test"},
    )

    vm_definition = {"name": "vm-01.example.test", "provider": "lab-vcenter"}
    result = cloud.create(vm_definition)

    assert result == {"id": "vm-123", "name": "vm-01.example.test"}
    clone.assert_called_once_with(
        {"connection": "opts"},
        "ubuntu-template",
        "vm-01.example.test",
        folder="group-v42",
        datastore="datastore-17",
        host=None,
        resource_pool=None,
        cluster=None,
        template=False,
        power_on=False,
        cpu_count=4,
        memory_mb=8192,
        annotation=None,
    )
    power_on.assert_called_once_with({"connection": "opts"}, "vm-01.example.test", host=None)
    assert vm_definition["key_filename"] == "/root/.ssh/salt-cloud"
    assert vm_definition["ssh_host"] == "192.0.2.25"
    loader_globals.utils["cloud.bootstrap"].assert_called_once_with(
        vm_definition, loader_globals.opts
    )


def test_create_rejects_duplicate_vm(monkeypatch):
    monkeypatch.setattr(
        cloud, "_cfg", lambda name, vm_, default=None, **kwargs: vm_.get(name, default)
    )
    monkeypatch.setattr(cloud, "_vm", lambda *args, **kwargs: object())

    assert cloud.create({"name": "vm-01"}) == {"Error": "VM 'vm-01' already exists"}


def test_destroy_powers_off_destroys_and_clears_cache(monkeypatch, loader_globals):
    loader_globals.opts["update_cachedir"] = True
    vm_ref = SimpleNamespace(runtime=SimpleNamespace(powerState="poweredOn"))
    power_off = MagicMock(return_value="task-off")
    destroy = MagicMock(return_value="task-destroy")
    wait = MagicMock()
    monkeypatch.setattr(cloud, "_vm", lambda *args, **kwargs: vm_ref)
    monkeypatch.setattr(cloud, "_vcf_opts", lambda: {"connection": "opts"})
    monkeypatch.setattr(cloud, "_task_timeout", lambda *args: 60)
    monkeypatch.setattr(cloud.vim_vm_power, "power_off", power_off)
    monkeypatch.setattr(cloud.vim_vm, "destroy", destroy)
    monkeypatch.setattr(cloud, "_wait_task", wait)

    assert cloud.destroy("vm-01") is True
    power_off.assert_called_once_with({"connection": "opts"}, "vm-01")
    destroy.assert_called_once_with({"connection": "opts"}, "vm-01")
    assert wait.call_count == 2
    loader_globals.utils["cloud.delete_minion_cachedir"].assert_called_once_with(
        "vm-01", "lab-vcenter", loader_globals.opts
    )


def test_list_node_views(monkeypatch):
    instances = [
        {
            "id": "vm-101",
            "name": "vm-01",
            "state": "poweredOn",
            "image": "Ubuntu Linux",
            "size": {"cpu": 2, "memory_mb": 4096},
            "private_ips": ["10.0.0.10"],
            "public_ips": [],
            "template": False,
        }
    ]
    monkeypatch.setattr(cloud, "_summary_instances", lambda: instances)

    assert cloud.list_nodes_min() == {"vm-01": "poweredOn"}
    assert cloud.list_nodes()["vm-01"]["id"] == "vm-101"
    assert cloud.list_nodes_full() == {"vm-01": instances[0]}
