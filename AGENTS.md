# Protect installed services

The installed `mcps` CLI and existing MCP pods are production, even when hosted
on this development machine. Ordinary development is not permission to deploy.

- Use the repository virtual environment, temporary metadata, and mocked Podman
  calls for unit tests. Never use Bring!, SEC EDGAR, or other existing servers as
  development fixtures.
- Run container integration tests only in a disposable Podman machine/connection,
  with separate `MCPS_HOME`. A separate state directory or server name alone does
  not isolate shared images, secrets, volumes, or autostart settings.
- Do not reinstall or upgrade the global `mcps` tool, rebuild/restart live pods,
  change live routing, or rotate production secrets as part of development or
  verification. Do those only for an explicitly requested deployment or repair.
- Keep the global tool non-editable. Promote a reviewed, tested release artifact
  deliberately; never point the production installation at a working checkout.
- Do not set `MCPS_ALLOW_LIVE_CHANGES` globally. Its process-local override is
  only for an explicitly selected disposable test environment or authorized repair.
- Adding a transport must preserve existing transports, URLs, authentication,
  and access policy. Successful OpenAI tunnel setup must not disable Funnel or
  restart existing services. Cover success, failure, removal, and rebuild paths.
- For an authorized live repair, inspect the current configuration, preserve a
  backup, make the smallest targeted change, and verify every affected access path.
