# saltext-vcf-cloud

[![CI](https://github.com/Arderos/saltext-vcf-cloud/actions/workflows/ci.yml/badge.svg)](https://github.com/Arderos/saltext-vcf-cloud/actions/workflows/ci.yml)
[![CodeQL](https://github.com/Arderos/saltext-vcf-cloud/actions/workflows/codeql.yml/badge.svg)](https://github.com/Arderos/saltext-vcf-cloud/actions/workflows/codeql.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

A native Salt Cloud driver for VMware vSphere, backed by
[`saltext.vcf`](https://github.com/salt-extensions/saltext-vcf).

Salt 3008 no longer ships the old community VMware cloud driver. This extension
restores the `vmware` provider without replacing Salt Cloud's lifecycle. Native
Salt Cloud still generates and pre-accepts minion keys, renders the deployment
script, bootstraps the minion, and removes its key during destruction. The
extension handles the vSphere operations through `saltext.vcf` and pyVmomi.

## What works

- Clone a VM or template, or create a blank VM.
- Select folders, datastores, clusters, resource pools, and hosts by name or MoID.
- Configure CPU, memory, advanced settings, hot-add options, NICs, and disks.
- Apply a named vCenter customization spec or generate a Linux customization spec.
- Wait for an address reported by VMware Tools and run the native Salt bootstrap.
- List, inspect, start, stop, reboot, and destroy VMs.
- Use normal Salt Cloud providers, profiles, maps, events, cache, and key lifecycle.

The driver has been integration-tested with Salt 3008.2, `saltext.vcf` 1.0.0,
pyVmomi 9.1.0.0, and vCenter 8.0.3. Salt 3008.x is the supported Salt line.

## Install

Install this extension on the machine that runs `salt-cloud`, normally the Salt
master. Salt 3008 packages use an isolated onedir Python, so use `salt-pip`, not
the system `pip`.

```bash
sudo salt-pip install saltext.vcf-cloud
```

The wheel installs the compatible `saltext.vcf[vcenter]` and pyVmomi dependencies.
To pin the first release:

```bash
sudo salt-pip install "saltext.vcf-cloud==0.1.0"
```

For a source checkout, use:

```bash
git clone https://github.com/Arderos/saltext-vcf-cloud.git
cd saltext-vcf-cloud
sudo salt-pip install .
```

Verify that Salt can discover the extension:

```bash
salt-pip show saltext.vcf-cloud saltext.vcf pyvmomi
salt-cloud --list-providers
```

`salt-cloud` is a new process on every invocation, so a restart is not required
for the CLI. Restart `salt-master` if runners or another long-lived master process
will load the extension.

To upgrade or remove it:

```bash
sudo salt-pip install --upgrade saltext.vcf-cloud
sudo salt-pip uninstall saltext.vcf-cloud
```

## Configure a provider

Create `/etc/salt/cloud.providers.d/vcenter.conf`:

```yaml
lab-vcenter:
  driver: vmware
  url: vcenter.example.com
  user: svc-salt-cloud@vsphere.local
  password: replace-with-a-secret
  verify_ssl: true
  task_timeout: 900
```

Do not commit the real password. Protect the provider file with restrictive
permissions or use a Salt-supported secret/SDB backend. Set `verify_ssl: false`
only for a vCenter whose certificate cannot yet be verified.

Provider options:

| Option | Required | Default | Meaning |
| --- | --- | --- | --- |
| `url` | yes | — | vCenter hostname or HTTPS URL |
| `user` | yes | — | vCenter account |
| `password` | yes | — | vCenter password |
| `verify_ssl` | no | `true` | Verify the vCenter TLS certificate |
| `protocol` | no | `https` | Only HTTPS is supported |
| `port` | no | `443` | vCenter HTTPS port |
| `vcf_timeout` | no | `30` | Connection timeout in seconds |
| `task_timeout` | no | `900` | Default vSphere task timeout in seconds |

Check connectivity without creating anything:

```bash
salt-cloud -f get_vcenter_version lab-vcenter
salt-cloud -f avail_images lab-vcenter
```

## Configure a profile

Create `/etc/salt/cloud.profiles.d/ubuntu.conf`:

```yaml
ubuntu-2404:
  provider: lab-vcenter
  clonefrom: template-ubuntu-2404
  cluster: compute-cluster
  folder: salt-managed
  datastore: datastore-01

  num_cpus: 2
  memory: 4GB
  cores_per_socket: 1
  annotation: Managed by Salt Cloud

  devices:
    network:
      Network adapter 1:
        name: server-vlan
        switch_type: distributed
        adapter_type: vmxnet3
        start_connected: true
    disk:
      Hard disk 2:
        size: 50
        thin_provision: true

  customization: true
  domain: example.com
  time_zone: UTC
  dns_servers:
    - 192.0.2.53

  ssh_username: ubuntu
  private_key: /root/.ssh/salt-cloud
  script: bootstrap-salt
  script_args: stable 3008
```

The keys under `devices.network` and `devices.disk` are device labels. When a
label already exists on the cloned template, the driver updates its network
backing and `start_connected` state or resizes the disk. vSphere cannot safely
change the type or MAC address of an existing NIC through this operation, so a
different `adapter_type` or `mac` produces a clear error instead of being
silently ignored. A missing NIC label creates a NIC; a missing disk label creates
a disk. Disk sizes must be positive whole numbers of GB. Disks can grow but
cannot shrink.

Use object MoIDs instead of names when vCenter contains duplicate names. For a
blank VM without `clonefrom`, `folder` and `datastore` are required, together
with enough placement information (`cluster`, `resourcepool`, or `host`) for
vCenter to select compute resources. Cloning from a vCenter template requires
`cluster` or `resourcepool`/`resource_pool`; `host` alone does not populate the
required resource pool in the clone placement specification.

### Static guest networking

Put addressing data on each NIC to generate a Linux customization spec:

```yaml
devices:
  network:
    Network adapter 1:
      name: server-vlan
      switch_type: distributed
      ip: 192.0.2.25
      subnet_mask: 255.255.255.0
      gateway:
        - 192.0.2.1
      dns_servers:
        - 192.0.2.53
```

Without `ip`, that NIC uses DHCP in the generated customization spec. Network
mapping order must match the template's guest NIC order. Alternatively, set
`customization_spec` to the name of an existing vCenter customization spec:

```yaml
customization_spec: linux-default
```

Guest customization and address discovery require a compatible guest and
VMware Tools/open-vm-tools running in the template.

Guest customization is not applied when `template: true`, because vCenter does
not support `CustomizeVM_Task` against a template object.

### Profile options

| Option | Default | Meaning |
| --- | --- | --- |
| `clonefrom` | none | Source VM/template name or MoID |
| `folder` | source folder | Destination folder name or MoID |
| `datastore` | source datastore | Destination datastore name or MoID |
| `cluster` | none | Cluster name or MoID |
| `resourcepool` / `resource_pool` | none | Resource pool name or MoID |
| `host` | none | ESXi host name or MoID |
| `image` | `otherGuest64` | vSphere guest ID for a blank VM |
| `num_cpus` | source value | Virtual CPU count |
| `memory` | source value | Memory in MB, `M`, `MB`, `G`, or `GB` |
| `cores_per_socket` | unchanged | Cores per virtual socket |
| `annotation` | unchanged | VM annotation |
| `extra_config` | `{}` | vSphere advanced settings mapping |
| `cpu_hot_add` | unchanged | Enable CPU hot-add |
| `cpu_hot_remove` | unchanged | Enable CPU hot-remove |
| `mem_hot_add` | unchanged | Enable memory hot-add |
| `nested_hv` | unchanged | Expose hardware virtualization |
| `vpmc` | unchanged | Enable virtual CPU performance counters |
| `template` | `false` | Create a template instead of a runnable VM |
| `power_on` | `true` | Power on after configuration |
| `deploy` | `true` | Run Salt's native bootstrap after address discovery |
| `customization` | `true` | Apply guest customization when it can be built |
| `customization_spec` | none | Existing vCenter customization spec name |
| `wait_for_ip_timeout` | `1200` | Seconds to wait for a guest IPv4 address |
| `task_timeout` | provider value | Per-profile vSphere task timeout |

Standard Salt Cloud deployment options such as `ssh_username`, `ssh_password`,
`private_key`/`key_filename`, `script`, `script_args`, `minion`, and `master`
are passed to Salt's native bootstrap implementation.

## Use a map

Create `/etc/salt/cloud.maps.d/vmware.map`:

```yaml
ubuntu-2404:
  - web-01.example.com:
      num_cpus: 4
      memory: 8GB
  - web-02.example.com:
      num_cpus: 4
      memory: 8GB
```

Create the VMs in parallel:

```bash
salt-cloud -m /etc/salt/cloud.maps.d/vmware.map -P
```

Or create one VM directly from a profile:

```bash
salt-cloud -p ubuntu-2404 web-01.example.com
```

Inspect and operate VMs:

```bash
salt-cloud -Q
salt-cloud -F
salt-cloud -a start web-01.example.com
salt-cloud -a stop web-01.example.com
salt-cloud -a reboot web-01.example.com
salt-cloud -d web-01.example.com
```

## Minion keys and bootstrap

There is no custom key-generation protocol in this driver. Salt Cloud's core
performs its normal sequence:

1. Generate the minion keypair before calling the provider's `create()`.
2. Pre-accept the public key on the master.
3. Render the selected deployment script and minion configuration.
4. Pass the generated keys and configuration through the native
   `cloud.bootstrap` utility after the VM has an address.
5. Remove the accepted key when Salt Cloud destroys the VM.

This is the same lifecycle used by native Salt Cloud providers. Options that
change Salt's key handling continue to belong to Salt Cloud itself.

## Troubleshooting

Run with debug logging when a vSphere or bootstrap step fails:

```bash
salt-cloud -l debug -p ubuntu-2404 test-01.example.com
```

Common causes:

- `vmware` does not appear in `--list-providers`: the extension was installed
  with system `pip` instead of `salt-pip`, or a required dependency cannot load.
- Certificate verification fails: install the issuing CA for the Salt host;
  use `verify_ssl: false` only as a temporary exception.
- An object name is ambiguous: use the vSphere MoID.
- The VM is created but bootstrap does not run: check VMware Tools, guest IP
  reporting, DNS fallback, SSH reachability, username/key, and
  `wait_for_ip_timeout`. The create result contains an `Error` and emits a
  `deploy_failed` event when no IPv4 address is found or bootstrap reports a
  failure.
- Guest customization fails: confirm the template OS supports vSphere guest
  customization and that NIC order matches the profile.

Salt Cloud 3008 pre-accepts the minion key before it calls a provider's
`create()` function, but does not tell the provider whether the key was accepted
on the local or a remote master. After a failed deployment, verify the target
master and remove an unused accepted key with `salt-key -d <minion-id>` when
appropriate.

The password is not included in Salt Cloud event payloads or returned instance
data. Debug logs from dependencies can still contain environmental details;
review logs before sharing them publicly.

## Development

```bash
git clone https://github.com/Arderos/saltext-vcf-cloud.git
cd saltext-vcf-cloud
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[tests]" build
pytest
python -m build
```

Unit tests do not require a vCenter. Live integration testing requires an
explicitly configured disposable VM/template and is intentionally not run by
public CI.

## Security

Please report vulnerabilities through GitHub's private vulnerability reporting
for this repository. See [SECURITY.md](SECURITY.md).

## License

Apache License 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
