# SPDX-License-Identifier: Apache-2.0
# Salt Cloud calls the public API with compatibility arguments such as
# ``call`` and ``kwargs`` even when a provider does not need their values.
# pylint: disable=unused-argument
"""Salt Cloud VMware driver backed by :mod:`saltext.vcf`.

Salt 3008 no longer ships the community VMware cloud driver.  This external
driver keeps the native Salt Cloud provider/profile/map lifecycle and delegates
vSphere operations to ``saltext-vcf``.  Salt Cloud itself remains responsible
for generating, pre-accepting, transporting, and removing minion keys.
"""

import ipaddress
import logging
import re
import socket
import time
from urllib.parse import urlparse

import salt.utils.cloud
import salt.utils.vmware as salt_vmware
from salt import config
from salt.exceptions import SaltCloudSystemExit

log = logging.getLogger(__name__)

__virtualname__ = "vmware"

try:
    from pyVmomi import vim
    from saltext.vcf.clients import vim_vm
    from saltext.vcf.clients import vim_vm_customization
    from saltext.vcf.clients import vim_vm_disk
    from saltext.vcf.clients import vim_vm_nic
    from saltext.vcf.clients import vim_vm_power
    from saltext.vcf.utils import vim as vcf_vim

    HAS_VCF = True
except ImportError:
    HAS_VCF = False


def __virtual__():
    """Load for configured ``vmware`` providers when saltext-vcf is present."""
    if get_configured_provider() is False:
        return False
    if get_dependencies() is False:
        return False
    return __virtualname__


def _get_active_provider_name():
    try:
        return __active_provider_name__.value()
    except AttributeError:
        return __active_provider_name__
    except NameError:
        return None


def get_configured_provider():
    """Return the active, configured VMware provider."""
    return config.is_provider_configured(
        __opts__,
        _get_active_provider_name() or __virtualname__,
        ("url", "user", "password"),
    )


def get_dependencies():
    """Report whether the VCF and pyVmomi dependencies can be loaded."""
    return config.check_driver_dependencies(__virtualname__, {"saltext.vcf and pyVmomi": HAS_VCF})


def script(vm_):
    """Return the rendered native Salt Cloud deployment script."""
    script_name = config.get_cloud_config_value("script", vm_, __opts__)
    if not script_name:
        script_name = "bootstrap-salt"
    return salt.utils.cloud.os_script(
        script_name,
        vm_,
        __opts__,
        salt.utils.cloud.salt_config_to_yaml(salt.utils.cloud.minion_config(__opts__, vm_)),
    )


def _cfg(name, vm_, default=None, search_global=True):
    return config.get_cloud_config_value(
        name,
        vm_,
        __opts__,
        search_global=search_global,
        default=default,
    )


def _provider_value(name, default=None):
    provider = get_configured_provider()
    return config.get_cloud_config_value(
        name,
        provider,
        __opts__,
        search_global=False,
        default=default,
    )


def _as_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _vcf_opts():
    """Translate the active Salt Cloud provider to saltext-vcf opts."""
    raw_url = str(_provider_value("url"))
    protocol = str(_provider_value("protocol", "https")).lower()
    if protocol != "https":
        raise SaltCloudSystemExit("saltext-vcf requires an HTTPS vCenter endpoint")

    parsed = urlparse(raw_url if "://" in raw_url else f"https://{raw_url}")
    host = parsed.hostname
    if not host:
        raise SaltCloudSystemExit(f"Invalid vCenter URL: {raw_url!r}")

    port = parsed.port or int(_provider_value("port", 443))
    endpoint = host if port == 443 else f"{host}:{port}"
    return {
        "saltext.vcf": {
            "vcenter": {
                "host": endpoint,
                "username": _provider_value("user"),
                "password": _provider_value("password"),
                "verify_ssl": _as_bool(_provider_value("verify_ssl", True)),
                "timeout": int(_provider_value("vcf_timeout", 30)),
            }
        }
    }


def _service_instance():
    return vcf_vim.get_service_instance(_vcf_opts())


def _content():
    return _service_instance().RetrieveContent()


def _objects(vim_type):
    return [
        item["object"]
        for item in salt_vmware.get_mors_with_properties(
            _service_instance(), vim_type, property_list=["name"]
        )
    ]


def _find_object(vim_type, name_or_id, required=True):
    items = salt_vmware.get_mors_with_properties(
        _service_instance(), vim_type, property_list=["name"]
    )
    matches = [
        item["object"]
        for item in items
        if name_or_id
        in (
            item["object"]._moId,  # pylint: disable=protected-access
            item["name"],
        )
    ]
    if len(matches) > 1:
        raise SaltCloudSystemExit(
            f"More than one {vim_type.__name__} is named {name_or_id!r}; use its MoID"
        )
    if matches:
        return matches[0]
    if required:
        raise SaltCloudSystemExit(f"{vim_type.__name__} {name_or_id!r} does not exist")
    return None


def _vm(name, required=True):
    return _find_object(vim.VirtualMachine, name, required=required)


def _wait_task(task_or_id, timeout=900):
    if isinstance(task_or_id, str):
        task = vim.Task(
            task_or_id,
            stub=_service_instance()._stub,  # pylint: disable=protected-access
        )
    else:
        task = task_or_id
    return vcf_vim.wait_for_task(task, timeout=timeout)


def _task_timeout(vm_=None):
    if vm_ is None:
        return int(_provider_value("task_timeout", 900))
    return int(_cfg("task_timeout", vm_, default=900))


def _memory_mb(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([mMgG][bB])?\s*", str(value))
    if not match:
        raise SaltCloudSystemExit(f"Invalid memory value: {value!r}")
    amount = float(match.group(1))
    unit = (match.group(2) or "MB").lower()
    return int(amount * 1024 if unit == "gb" else amount)


def _list_value(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _network_target(name, switch_type=None):
    networks = _objects(vim.Network)
    distributed = _objects(vim.dvs.DistributedVirtualPortgroup)
    requested_distributed = str(switch_type or "").lower() == "distributed"
    candidates = distributed if requested_distributed else networks + distributed
    candidates = {
        network._moId: network for network in candidates  # pylint: disable=protected-access
    }.values()
    matches = [
        network
        for network in candidates
        if name in (network.name, network._moId)  # pylint: disable=protected-access
    ]
    if len(matches) != 1:
        if not matches:
            raise SaltCloudSystemExit(f"VMware network {name!r} does not exist")
        raise SaltCloudSystemExit(f"More than one VMware network is named {name!r}; use its MoID")

    network = matches[0]
    if isinstance(network, vim.dvs.DistributedVirtualPortgroup):
        return {
            "portgroup_key": network.key,
            "dvs_uuid": network.config.distributedVirtualSwitch.uuid,
        }
    return {"network_moid": network._moId}  # pylint: disable=protected-access


def _datastore_moid(name_or_id):
    if not name_or_id:
        return None
    return _find_object(vim.Datastore, name_or_id)._moId  # pylint: disable=protected-access


def _match_by_label(items, label):
    wanted = label.casefold()
    return next((item for item in items if item["label"].casefold() == wanted), None)


def _configure_networks(name, network_devices, timeout):
    for label, settings in (network_devices or {}).items():
        current = _match_by_label(vim_vm_nic.list_(_vcf_opts(), name), label)
        target = _network_target(settings["name"], settings.get("switch_type"))
        if current:
            task = vim_vm_nic.update_backing(_vcf_opts(), name, current["key"], **target)
        else:
            task = vim_vm_nic.add(
                _vcf_opts(),
                name,
                nic_type=settings.get("adapter_type", "vmxnet3"),
                mac_address=settings.get("mac"),
                start_connected=_as_bool(settings.get("start_connected", True)),
                **target,
            )
        _wait_task(task, timeout=timeout)


def _configure_disks(name, disk_devices, timeout):
    for label, settings in (disk_devices or {}).items():
        current = _match_by_label(vim_vm_disk.list_(_vcf_opts(), name), label)
        requested_size = settings.get("size")
        if current:
            if requested_size is None:
                continue
            current_size = current["capacity_bytes"] / (1024**3)
            requested_size = float(requested_size)
            if requested_size < current_size:
                raise SaltCloudSystemExit(
                    f"Disk {label!r} cannot shrink from {current_size:g}GB "
                    f"to {requested_size:g}GB"
                )
            if requested_size > current_size:
                task = vim_vm_disk.resize(_vcf_opts(), name, current["key"], requested_size)
                _wait_task(task, timeout=timeout)
            continue

        if requested_size is None:
            raise SaltCloudSystemExit(f"New disk {label!r} requires a size")
        task = vim_vm_disk.add(
            _vcf_opts(),
            name,
            requested_size,
            datastore_moid=_datastore_moid(settings.get("datastore")),
            controller_key=settings.get("controller_key"),
            unit_number=settings.get("unit_number"),
            disk_mode=settings.get("mode", "persistent"),
            thin=_as_bool(settings.get("thin_provision", False)),
            eager_scrub=_as_bool(settings.get("eagerly_scrub", False)),
        )
        _wait_task(task, timeout=timeout)


def _reconfigure(name, vm_, timeout):
    extra_config = dict(_cfg("extra_config", vm_, default={}) or {})
    cores_per_socket = _cfg("cores_per_socket", vm_, default=None)
    annotation = _cfg("annotation", vm_, default=None)
    if cores_per_socket is not None or annotation is not None or extra_config:
        task = vim_vm.reconfigure(
            _vcf_opts(),
            name,
            cores_per_socket=cores_per_socket,
            annotation=annotation,
            advanced_settings=extra_config,
        )
        _wait_task(task, timeout=timeout)

    cpu_hot_add = _cfg("cpu_hot_add", vm_, default=extra_config.get("cpu.hotadd"))
    cpu_hot_remove = _cfg("cpu_hot_remove", vm_, default=None)
    mem_hot_add = _cfg("mem_hot_add", vm_, default=extra_config.get("mem.hotadd"))
    nested_hv = _cfg("nested_hv", vm_, default=None)
    vpmc = _cfg("vpmc", vm_, default=None)
    values = {
        "cpuHotAddEnabled": cpu_hot_add,
        "cpuHotRemoveEnabled": cpu_hot_remove,
        "memoryHotAddEnabled": mem_hot_add,
        "nestedHVEnabled": nested_hv,
        "vPMCEnabled": vpmc,
    }
    if not any(value is not None for value in values.values()):
        return

    spec = vim.vm.ConfigSpec()
    for attribute, value in values.items():
        if value is not None and hasattr(spec, attribute):
            setattr(spec, attribute, _as_bool(value))
    _wait_task(_vm(name).ReconfigVM_Task(spec=spec), timeout=timeout)


def _customization_nics(network_devices):
    nics = []
    for settings in (network_devices or {}).values():
        if settings.get("ip"):
            nic = {
                "ip": settings["ip"],
                "subnet": settings.get("subnet_mask") or settings.get("subnet", ""),
                "gateway": _list_value(settings.get("gateway")),
            }
        else:
            nic = {"dhcp": True}
        if settings.get("dns_servers"):
            nic["dns_servers"] = _list_value(settings["dns_servers"])
        nics.append(nic)
    return nics


def _apply_customization(name, vm_, network_devices, timeout):
    customization = _cfg("customization", vm_, default=True, search_global=False)
    named_spec = _cfg("customization_spec", vm_, default=None, search_global=False)
    if not customization:
        return

    if named_spec:
        manager = _content().customizationSpecManager
        spec = manager.GetCustomizationSpec(name=named_spec).spec
    elif network_devices:
        configured_domain = _cfg("domain", vm_, default="local", search_global=False)
        short_name, separator, fqdn_domain = name.partition(".")
        domain = fqdn_domain if separator else configured_domain
        search_path = _cfg("search", vm_, default=None, search_global=False)
        if search_path is None and domain:
            search_path = [domain]
        spec = vim_vm_customization.linux_spec(
            short_name,
            domain,
            time_zone=_cfg("time_zone", vm_, default="UTC", search_global=False),
            nics=_customization_nics(network_devices),
            dns_servers=_list_value(_cfg("dns_servers", vm_, default=[])),
            dns_search_path=_list_value(search_path),
        )
    else:
        return

    task = vim_vm_customization.apply(_vcf_opts(), name, spec)
    _wait_task(task, timeout=timeout)


def _valid_ipv4(value):
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return address.version == 4 and not address.is_loopback and not address.is_link_local


def _guest_ipv4(vm_ref):
    summary_ip = getattr(getattr(vm_ref.summary, "guest", None), "ipAddress", None)
    if summary_ip and _valid_ipv4(summary_ip):
        return summary_ip
    for network in getattr(vm_ref.guest, "net", None) or []:
        for address in getattr(network, "ipAddress", None) or []:
            if _valid_ipv4(address):
                return address
    return None


def _wait_for_ip(name, timeout):
    deadline = time.monotonic() + float(timeout)
    while time.monotonic() < deadline:
        address = _guest_ipv4(_vm(name))
        if address:
            return address
        time.sleep(2)

    try:
        addresses = socket.gethostbyname_ex(name)[2]
    except OSError:
        addresses = []
    return next((address for address in addresses if _valid_ipv4(address)), None)


def _ip_lists(vm_ref):
    private_ips = []
    public_ips = []
    seen = set()
    for network in getattr(vm_ref.guest, "net", None) or []:
        for value in getattr(network, "ipAddress", None) or []:
            try:
                address = ipaddress.ip_address(value)
            except ValueError:
                continue
            if address.is_loopback or address.is_link_local or value in seen:
                continue
            seen.add(value)
            (private_ips if address.is_private else public_ips).append(value)
    return private_ips, public_ips


def _instance_data(vm_ref):
    private_ips, public_ips = _ip_lists(vm_ref)
    config_data = vm_ref.config
    summary = vm_ref.summary
    return {
        "id": vm_ref._moId,  # pylint: disable=protected-access
        "name": vm_ref.name,
        "state": str(vm_ref.runtime.powerState),
        "image": getattr(config_data, "guestFullName", None),
        "size": {
            "cpu": getattr(summary.config, "numCpu", None),
            "memory_mb": getattr(summary.config, "memorySizeMB", None),
        },
        "private_ips": private_ips,
        "public_ips": public_ips,
        "template": bool(getattr(config_data, "template", False)),
        "folder": getattr(vm_ref.parent, "name", None),
        "host": getattr(getattr(vm_ref.runtime, "host", None), "name", None),
        "datastores": [datastore.name for datastore in (vm_ref.datastore or [])],
    }


def _summary_instance_data(item):
    private_ips = []
    public_ips = []
    primary_ip = item.get("guest.ipAddress")
    if primary_ip:
        try:
            address = ipaddress.ip_address(primary_ip)
        except ValueError:
            pass
        else:
            (private_ips if address.is_private else public_ips).append(primary_ip)
    return {
        "id": item["object"]._moId,  # pylint: disable=protected-access
        "name": item.get("name"),
        "state": str(item.get("runtime.powerState")),
        "image": item.get("config.guestFullName", ""),
        "size": {
            "cpu": item.get("config.hardware.numCPU"),
            "memory_mb": item.get("config.hardware.memoryMB"),
        },
        "private_ips": private_ips,
        "public_ips": public_ips,
        "template": bool(item.get("config.template")),
    }


def _summary_instances():
    properties = [
        "name",
        "runtime.powerState",
        "config.template",
        "config.guestFullName",
        "config.hardware.numCPU",
        "config.hardware.memoryMB",
        "guest.ipAddress",
    ]
    return [
        _summary_instance_data(item)
        for item in salt_vmware.get_mors_with_properties(
            _service_instance(), vim.VirtualMachine, property_list=properties
        )
    ]


def _fire_event(message, tag, args):
    __utils__["cloud.fire_event"](
        "event",
        message,
        tag,
        args=args,
        sock_dir=__opts__["sock_dir"],
        transport=__opts__["transport"],
    )


def avail_locations(call=None):
    """Return locations (vSphere placement is configured in profiles)."""
    return {}


def avail_images(call=None):
    """Return virtual machines marked as templates."""
    templates = {}
    for vm_ref in _objects(vim.VirtualMachine):
        if vm_ref.config.template:
            templates[vm_ref.name] = _instance_data(vm_ref)
    return templates


def avail_sizes(call=None):
    """Return sizes (vSphere CPU and memory are configured in profiles)."""
    return {}


def list_nodes_min(kwargs=None, call=None):
    """Return VM names and power states."""
    return {item["name"]: item["state"] for item in _summary_instances()}


def list_nodes(kwargs=None, call=None):
    """Return the standard Salt Cloud view of all VMs."""
    return {
        item["name"]: {
            key: value
            for key, value in item.items()
            if key in ("id", "image", "size", "state", "private_ips", "public_ips")
        }
        for item in _summary_instances()
    }


def list_nodes_full(kwargs=None, call=None):
    """Return all VM fields exposed by this driver."""
    return {item["name"]: item for item in _summary_instances()}


def list_nodes_select(call=None):
    """Return the fields selected by ``query.selection``."""
    if call == "action":
        raise SaltCloudSystemExit(
            "The list_nodes_select function must be called with -f or --function."
        )
    selection = __opts__.get("query.selection")
    if not selection:
        raise SaltCloudSystemExit("query.selection not found in /etc/salt/cloud")
    return {
        item["name"]: {key: item.get(key) for key in selection} for item in _summary_instances()
    }


def show_instance(name, call=None):
    """Return details for one VM, or an empty mapping when it is absent."""
    vm_ref = _vm(name, required=False)
    return _instance_data(vm_ref) if vm_ref else {}


def get_vcenter_version(kwargs=None, call=None):
    """Return vCenter product, version, build, and API version."""
    about = _content().about
    return {
        "name": about.name,
        "version": about.version,
        "build": about.build,
        "api_version": about.apiVersion,
    }


def start(name, call=None):
    """Power on a VM."""
    task = vim_vm_power.power_on(_vcf_opts(), name)
    _wait_task(task, timeout=_task_timeout())
    return True


def stop(name, soft=False, call=None):
    """Power off a VM, or request a guest shutdown when ``soft`` is true."""
    if soft:
        return vim_vm_power.shutdown_guest(_vcf_opts(), name)
    task = vim_vm_power.power_off(_vcf_opts(), name)
    _wait_task(task, timeout=_task_timeout())
    return True


def reboot(name, call=None):
    """Request a guest reboot through VMware Tools."""
    return vim_vm_power.reboot_guest(_vcf_opts(), name)


def destroy(name, call=None):
    """Power off and destroy a VM through the native Salt Cloud lifecycle."""
    if call == "function":
        raise SaltCloudSystemExit(
            "The destroy action must be called with -d, --destroy, -a or --action."
        )

    _fire_event(
        "destroying instance",
        f"salt/cloud/{name}/destroying",
        {"name": name},
    )
    vm_ref = _vm(name, required=False)
    if vm_ref is not None:
        timeout = _task_timeout()
        if str(vm_ref.runtime.powerState) != "poweredOff":
            _wait_task(vim_vm_power.power_off(_vcf_opts(), name), timeout=timeout)
        _wait_task(vim_vm.destroy(_vcf_opts(), name), timeout=timeout)

    _fire_event(
        "destroyed instance",
        f"salt/cloud/{name}/destroyed",
        {"name": name},
    )
    if __opts__.get("update_cachedir", False):
        __utils__["cloud.delete_minion_cachedir"](
            name, _get_active_provider_name().split(":")[0], __opts__
        )
    return True


def create(vm_):
    """Create a VMware VM and hand deployment back to native Salt Cloud."""
    try:
        if (
            vm_.get("profile")
            and config.is_profile_configured(
                __opts__,
                _get_active_provider_name() or __virtualname__,
                vm_["profile"],
                vm_=vm_,
            )
            is False
        ):
            return False
    except AttributeError:
        pass

    name = _cfg("name", vm_)
    event_args = __utils__["cloud.filter_event"](
        "creating", vm_, ["name", "profile", "provider", "driver"]
    )
    _fire_event("starting create", f"salt/cloud/{name}/creating", event_args)

    if _vm(name, required=False) is not None:
        return {"Error": f"VM {name!r} already exists"}

    timeout = _task_timeout(vm_)
    source_name = _cfg("clonefrom", vm_, default=None)
    folder = _cfg("folder", vm_, default=None)
    datastore = _cfg("datastore", vm_, default=None)
    cluster = _cfg("cluster", vm_, default=None)
    resource_pool = _cfg("resourcepool", vm_, default=None)
    if resource_pool is None:
        resource_pool = _cfg("resource_pool", vm_, default=None)
    host = _cfg("host", vm_, default=None)
    num_cpus = _cfg("num_cpus", vm_, default=None)
    memory_mb = _memory_mb(_cfg("memory", vm_, default=None))
    annotation = _cfg("annotation", vm_, default=None)
    template = _as_bool(_cfg("template", vm_, default=False))
    power_on = _as_bool(_cfg("power_on", vm_, default=True))
    deploy = _as_bool(_cfg("deploy", vm_, default=True))
    devices = _cfg("devices", vm_, default={}) or {}
    private_key = _cfg("private_key", vm_, default=None, search_global=False)
    if private_key is None:
        private_key = _cfg("key_filename", vm_, default=None, search_global=False)

    request_args = __utils__["cloud.filter_event"](
        "requesting", vm_, ["name", "profile", "provider", "driver"]
    )
    _fire_event("requesting instance", f"salt/cloud/{name}/requesting", request_args)

    try:
        if source_name:
            source = _vm(source_name)
            target_folder = folder or source.parent._moId  # pylint: disable=protected-access
            if datastore:
                target_datastore = datastore
            elif source.datastore:
                target_datastore = source.datastore[0]._moId  # pylint: disable=protected-access
            else:
                raise SaltCloudSystemExit(f"Source VM/template {source_name!r} has no datastore")
            task = vim_vm.clone(
                _vcf_opts(),
                source_name,
                name,
                folder=target_folder,
                datastore=target_datastore,
                host=host,
                resource_pool=resource_pool,
                cluster=cluster,
                template=template,
                power_on=False,
                cpu_count=num_cpus,
                memory_mb=memory_mb,
                annotation=annotation,
            )
        else:
            if not folder or not datastore:
                raise SaltCloudSystemExit(
                    "folder and datastore are required when creating without clonefrom"
                )
            task = vim_vm.create(
                _vcf_opts(),
                name,
                folder,
                datastore,
                cpu_count=num_cpus or 1,
                memory_mb=memory_mb or 1024,
                guest_id=_cfg("image", vm_, default="otherGuest64"),
                cluster=cluster,
                host=host,
                resource_pool=resource_pool,
                annotation=annotation or "",
            )
        _wait_task(task, timeout=timeout)

        _reconfigure(name, vm_, timeout)
        _configure_networks(name, devices.get("network"), timeout)
        _configure_disks(name, devices.get("disk"), timeout)
        _apply_customization(name, vm_, devices.get("network"), timeout)

        if not template and power_on:
            _wait_task(
                vim_vm_power.power_on(_vcf_opts(), name, host=host),
                timeout=timeout,
            )
    except Exception as exc:  # pylint: disable=broad-except
        log.exception("Error creating VMware VM %s", name)
        return {"Error": f"Error creating {name}: {exc}"}

    if not template and power_on:
        address = _wait_for_ip(
            name,
            _cfg("wait_for_ip_timeout", vm_, default=20 * 60),
        )
        if address and deploy:
            if private_key:
                vm_["key_filename"] = private_key
            vm_.setdefault("ssh_host", address)
            __utils__["cloud.bootstrap"](vm_, __opts__)

    data = show_instance(name, call="action")

    created_args = __utils__["cloud.filter_event"](
        "created", vm_, ["name", "profile", "provider", "driver"]
    )
    _fire_event("created instance", f"salt/cloud/{name}/created", created_args)
    return data
