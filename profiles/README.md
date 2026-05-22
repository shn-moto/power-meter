# Device Profiles

Authoritative device configuration now starts with JSON profiles under `profiles/devices/`.

Current implementation slice:
- seed profiles are stored on disk
- startup sync loads them into `device_profiles`
- startup sync materializes runtime rows into `devices`, `device_connections`, and `device_capabilities`

Required top-level sections:
- `profile_version`
- `device`
- `connection`
- `summary`
- `dps`
- `controls`

Required `connection` fields:
- `local_key`
- `local_ip`
- `protocol_version`

Required `summary` fields:
- `default_power_mode`
- `default_power_dps_key`
- `default_visualized_codes`

File naming rule:
- the file name must match `device.device_id`, for example `profiles/devices/33741346d8f15bcb2282.json`

Zigbee gateways and their sub-devices:
- A Zigbee gateway is just a regular profile with `device.is_gateway: true`. It has its own `local_key` and `local_ip`.
- A sub-device profile has empty `connection.local_key` and `connection.local_ip`, and points at the gateway via `connection.gateway_device_id`. The runtime inherits the gateway's IP and local_key when querying — the sub-device is addressed by its own `device_id` as `cid`.

Current scope:
- upload-only registration UI is not implemented yet
- separate discovery flow is not implemented yet
- cloud artifacts remain diagnostic-only and are not authoritative for runtime semantics