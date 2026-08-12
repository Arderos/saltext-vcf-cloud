# Changelog

## 0.1.1 (2026-08-13)

### Added

- Allow profiles and map entries to attach vCenter tags by category and name to newly
  created VMs and templates. Providers can optionally create missing categories and
  tags with configurable new-category cardinality.

## 0.1.0 (2026-08-12)

### Added

- Native Salt Cloud `vmware` provider for Salt 3008.x.
- VM clone/create, hardware, disk, NIC, Linux customization, power, query,
  bootstrap, destroy, events, cache, and native minion-key lifecycle support.
- Installation and configuration documentation, unit tests, CI, CodeQL, and
  Trusted Publishing release automation.
- Complete guest grains as successful deployment output and factual minion IDs
  in `created` events, with a non-fatal instance-data fallback when the
  master-side grains query is unavailable or times out.
