# Security

diy-port emulates a UniFi AI Port on a trusted LAN. It is a proof of concept,
not a hardened internet-facing service.

## Trust boundary

- The HTTPS control API on `:443` intentionally has no authentication: it
  impersonates the camera API Protect expects. Bind it to the camera's own
  interface address, not `0.0.0.0`, and keep it off untrusted networks. Anyone
  who can reach it can read device information and snapshots and can adopt or
  re-point the instance.
- Snapshot uploads are restricted to the console host's
  `/internal/camera-upload/...` endpoints; redirects are refused so the client
  certificate and device headers are not replayed elsewhere.
- The face DB is fetched only from the console's HTTPS host, with redirects
  refused. TLS certificate verification is disabled because Protect uses a
  self-signed console certificate; the host pin is the boundary. Do not use
  this on a network where the console path can be impersonated.
- Runtime state (adoption token, client key, face DB) defaults to
  `$XDG_STATE_HOME/diyport` or `~/.local/state/diyport`, with private file
  modes. Snapshots, face crops and logs are private to the service user.

## Reporting

Report vulnerabilities privately through the repository's GitHub Security
Advisories. Do not open a public issue for a credential, adoption or
network-boundary problem.
