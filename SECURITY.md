# Security

Report vulnerabilities privately through this repository's Security tab using
**Report a vulnerability**. Do not put credentials or exploit details in public
issues. Rotate any exposed bearer token with `mcps token NAME --rotate` and
rotate upstream API keys at their provider.

## Trust boundaries

- Only install repositories and packages you trust. Their install scripts execute
  during image builds, and their tools execute with the API credentials you give
  them. Containers do not make arbitrary code safe. Use rootless Podman; do not
  mount the host filesystem or container socket into hosted servers.
- Tailnet-only servers rely on Tailscale ACLs and have no application bearer
  check. Restrict access to your identity on shared tailnets. Public mode routes
  all HTTPS traffic through the bearer gateway, including tailnet callers.
- Public servers use a shared bearer credential, not OAuth or per-user scopes.
  Anyone holding the token can invoke the server's tools. Keep client approvals
  enabled, especially for write tools; server output can contain prompt injection.
- Prefer Authorization headers. Token-in-path compatibility is supported, but
  URLs can leak into client history, proxy logs and screenshots. The gateway does
  not log request URLs or pass its authorization header to the MCP process.
- The optional IP allowlist is defense in depth, never user authentication.
  It is off by default because Funnel may not provide a trustworthy original
  source IP. Do not use crawler IP lists as MCP egress allowlists.
- Podman secrets are not encrypted by this project. Container and host
  administrators can retrieve them. Values supplied on the command line can
  appear in shell history; prefer hidden prompts, stdin, or a protected env file.
- Server code shares a pod network namespace with Tailscale. Capability dropping
  and `no-new-privileges` reduce risk, but this is not a hostile multi-tenant
  sandbox. This project does not enforce outbound-network or per-tool policy.

## Maintenance and upgrades

Reinstall the CLI and rebuild each hosted server with `--force --rebuild-base`
to apply gateway changes. Existing containers are not updated by pulling source.
Preserve the server's original flags, especially `--public`, environment and
allowlist settings. Rotation recreates the app container and interrupts sessions.

The bridge and its transitive npm dependencies are locked; `qs` is overridden to
6.16.0 to fix the vulnerable range reported by npm audit. Bridge traffic logging
is disabled to avoid persisting tool arguments/results; idle sessions expire
after ten minutes. OS images, uv and third-party server dependencies
still require regular updates and scanning; pin image digests in deployments
that need reproducibility. A passed test suite is not a security certification.

## Repository controls

All files have one code owner: `@freddy-jay`. Default-branch rules require PRs,
successful CI, resolved review conversations, and block force pushes/deletion.
A separate rule restricts merges to the owner through PRs and requires code-owner
review. Only that user may bypass the review rule to merge their own PR, since
GitHub prohibits self-approval. The separate PR/CI rule has no bypass actors.
CODEOWNERS is enforced from the base branch, so its new policy takes effect
after the initial security PR is merged. GitHub settings must be configured;
merely adding this document or CODEOWNERS does not enable protections.

CI uses read-only permissions, no saved checkout credentials, and no secrets for
fork PR tests. GitHub Actions cannot approve PRs. Dependabot checks Python, bridge npm and
Actions dependencies weekly. Review third-party code and dependency changes
before running them with production credentials.
