# Installation

Install on the host that runs `salt-cloud`. For an onedir Salt 3008.x install,
use Salt's own pip wrapper:

```bash
sudo salt-pip install saltext.vcf-cloud
```

Using the system Python's `pip` does not make the extension visible to an
onedir Salt installation. See the repository README for provider, profile, map,
upgrade, removal, and troubleshooting examples.
