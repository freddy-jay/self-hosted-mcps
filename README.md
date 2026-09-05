# mcps

Self-host any MCP server. Give it a GitHub link; it builds a Podman pod with a
Tailscale sidecar and hands you an HTTPS URL.

## Quick start

Install and configure once (Podman must be running):

```sh
uv tool install .
mcps init
```

`init` asks for your Tailscale auth key. Create a reusable, ephemeral,
pre-approved key in [Tailscale settings](https://login.tailscale.com/admin/settings/keys).

Add a server and get its client configuration:

```sh
mcps add pypi:mcp-server-time --name time
mcps client time
```

`client` defaults to Codex. Copy its output into `~/.codex/config.toml`.
For Claude Code, `add` also prints the connection command.

Using the OpenAI API or another hosted client? Add `--public`:

```sh
mcps add pypi:mcp-server-time --name time --public
mcps client time --client openai
```

The token is generated and stored automatically. Retrieve it with
`mcps token time` when configuring the client. If `time` already exists,
add `--force` to the `add` command to replace it.

Everyday commands: `mcps ls`, `mcps logs time`, `mcps restart time`, `mcps rm time`.
If setup fails, run `mcps doctor`. On Windows/macOS, start Podman with
`podman machine start`.

## Commands

```
mcps add <target>   build a server and put it on your tailnet
mcps ls             what's running, and its URL
mcps logs <name>    server output (--tailscale for the sidecar, -f to follow)
mcps restart <name>
mcps token <name>   show the bearer token (--rotate replaces and applies it)
mcps client <name>  print OpenAI Codex config (--client openai for Responses API)
mcps secrets        add / ls / rm a server's environment secrets
mcps autostart      bring servers back after a reboot (--enable / --disable)
mcps rm <name>      pod, tailnet node, image, secret, sources
mcps doctor
```

`<target>` is `owner/repo`, a full git URL, `npm:<package>` or `pypi:<package>`.

Common flags on `add` (everything else has a default):

| flag | why |
| --- | --- |
| `--public` | also publish it on the internet, for clients that aren't on your tailnet |
| `--subdir src/foo` | monorepos — the whole repo stays the build context, so shared tsconfig/workspace files resolve |
| `--cmd "node dist/x.js"` | the entrypoint guess was wrong |
| `-e KEY=VALUE`, `--env-file` | API keys the server needs |
| `--ref v1.2.0` | pin a branch or tag |
| `--name` | the tailnet hostname becomes `mcp-<name>` |

For optional token, IP and runtime controls, see `mcps add --help` under
**Advanced**. You do not need them for normal setup.

## Which clients can reach it

By default a server is **tailnet-only**. Local clients such as OpenAI Codex,
Claude Code and Claude Desktop can reach it from a device on your tailnet.
Hosted clients such as the OpenAI Responses API and claude.ai need public
reachability. For those, use `--public`:

```
mcps add pypi:mcp-server-time --name time --public
```

Public mode serves `https://mcp-time.your-tailnet.ts.net/mcp` on standard HTTPS
port 443 through [Tailscale Funnel](https://tailscale.com/kb/1223/funnel).
All requests, including those from your tailnet, then require a bearer token.
There is no separate 8443 endpoint. IP restrictions are optional and off by default.

Funnel is ACL-gated and Tailscale **ignores it silently** when the attribute is
missing: the local config reports "Funnel on" while the control plane publishes
no public DNS record, so the name resolves inside your tailnet and nowhere else.
`add --public` therefore checks the name against a public resolver over HTTPS (a
plain lookup from a tailnet machine is answered by MagicDNS and never leaves the
host) and prints the policy snippet you need at login.tailscale.com/admin/acls:

```jsonc
"nodeAttrs": [{ "target": ["autogroup:member"], "attr": ["funnel"] }]
```

### OpenAI Codex

Print a configuration entry without retrieving or embedding credentials:

```sh
mcps client time
```

Add its output to your Codex configuration (`~/.codex/config.toml`). For a public
server it includes `bearer_token_env_var = "MCPS_TIME_TOKEN"`. Set that variable
in the environment of the Codex process; for a private server no token is needed.

```powershell
$env:MCPS_TIME_TOKEN = mcps token time
codex
```

```sh
export MCPS_TIME_TOKEN="$(mcps token time)"
codex
```

Restart a desktop client from an environment containing the variable if needed.
The generated TOML never stores the credential. See the official
[Codex MCP configuration](https://learn.chatgpt.com/docs/extend/mcp?surface=cli).

### OpenAI Responses API

```sh
mcps client time --client openai
```

This prints runnable Python using the OpenAI SDK's MCP tool, the public HTTPS
endpoint, and `authorization` read from `MCPS_TIME_TOKEN`. Install `openai` in
your client environment and set `OPENAI_API_KEY` and `OPENAI_MODEL` as well.
The OpenAI API key belongs to the calling client; do not send it to the MCP gateway.
The command refuses private endpoints, and does not make a paid API request.

The example keeps `require_approval="always"` and prints the response items.
For tool execution, inspect each `mcp_approval_request` and submit an explicit
`mcp_approval_response` for the approved request, including the tool configuration
and token again. Follow the official
[MCP and Connectors guide](https://developers.openai.com/api/docs/guides/tools-connectors-mcp).
Do not automatically approve tool calls based on text returned by the server.

### ChatGPT

The generated Codex and Responses configurations are not ChatGPT connector
configuration. ChatGPT's custom MCP setup and authentication depend on the
account and current product UI; see
[Connect and test](https://developers.openai.com/plugins/deploy/connect-chatgpt).
This gateway implements shared bearer authentication, not OAuth discovery or
per-user authorization. If your ChatGPT setup requires OAuth, put a compatible
OAuth gateway in front; never disable bearer authentication to make it connect.
The token-in-URL compatibility form may work with clients accepting a URL and no
custom headers, but it is not a verified ChatGPT integration here.

### The bearer token

`mcps` generates it: 32 bytes from the OS CSPRNG (`secrets.token_urlsafe`).
It is not a password — nothing types it, so a password manager's generator adds
no strength. Generate your own only if you want your vault to be the system of
record, and pipe it in rather than passing it as an argument (argv is visible to
other processes and lands in shell history):

```
bw get password mcp-time | mcps add pypi:mcp-server-time --name time --public --token-stdin
```

The token is stored in a **podman secret**, not on disk in your home directory:
`podman inspect` shows it as `*******`, where a plain `-e` variable shows the
value. The same applies to your Tailscale auth key, which lives in the
`mcps-authkey` secret rather than in `config.json`. 
Rebuilds keep the token, so a connector you already configured keeps working.
`mcps token <name>` prints it, `--rotate` replaces it and recreates the app container to apply it immediately;
`--new-token` on `add` does the same during a rebuild. `add` and `restart` do not
print credentials. For clients without headers, the compatibility URL is
`<public-url>/<token>`; treat it as a password and prefer header authentication.
Custom tokens must be 32-256 URL-safe letters, digits, underscores or hyphens.

Your own `-e` / `--env-file` variables get the same treatment — each becomes its
own podman secret, mounted into the container under its real name. Nothing you
pass ends up in a file under `MCPS_HOME`, and `podman inspect` shows every one
of them as `*******`:

```
mcps add owner/repo -e API_KEY=... -e API_SECRET=...
```

A rebuild with no `-e` keeps the variables the server already had, and `mcps rm`
deletes them with the server. To change one afterwards, use `mcps secrets` —
it recreates the app container so the new value takes effect:

```
mcps secrets ls bring                                  # names only, never values
mcps secrets add bring --name BRING_PASSWORD           # prompts, hidden input
bw get password bring | mcps secrets add bring --name BRING_PASSWORD --stdin
mcps secrets rm bring --name BRING_PASSWORD
```

`--value` exists for scripting, but the prompt and `--stdin` keep the value out
of your shell history and out of argv.

**On encryption:** podman's default file driver stores secrets unencrypted at
mode 0600 inside the podman machine. What you gain is that values never reach
your host filesystem and never appear in `podman inspect`, process arguments or
shell history. Real at-rest encryption needs an external secret driver
(`pass`/gopass) configured in `containers.conf`; this tool does not set one up.

A server running in a container also cannot reach your OS keychain — Windows
Credential Manager, macOS Keychain, or a Linux D-Bus keyring. MCP servers that
prefer a credential store fall back to environment variables; pass them here.

### The IP allowlist

The allowlist is **off by default**, so public OpenAI and Claude callers both
use bearer authentication without a provider-specific IP restriction. Opt in
only if your reverse proxy supplies a trustworthy original client address:

```
mcps add owner/repo --public --allow 192.0.2.4/32
```

`--allow` accepts IPv4 and IPv6 CIDRs and may be repeated. `--allow any` must be
used alone. Invalid ranges fail closed. The gateway accepts a forwarded address
only from a loopback peer and uses the rightmost `X-Forwarded-For` entry. It binds
only to loopback inside the pod. Funnel may not expose the original source IP;
if it does not, an allowlist will block legitimate requests too.

An IP allowlist does not identify your account. Provider ranges are shared by
other users, and crawler ranges are not a substitute for verified MCP egress
ranges. The bearer token is the authentication control. Rejection logs omit
URLs, tokens, query strings and raw forwarding headers.

`--silent` drops unauthorised connections instead of answering, so a scanner
gets a reset rather than a 401 confirming something is listening. Turn it on
after a client is working: setup wizards often probe without credentials first,
and a silent drop is indistinguishable from an unreachable host.

**Your URL is not a secret.** Tailscale obtains a Let's Encrypt certificate for
`mcp-<name>.<tailnet>.ts.net`, and every issued certificate is recorded in the
public [Certificate Transparency](https://tailscale.com/kb/1153/enabling-https)
ledger, hostname included — so don't put anything sensitive in a server name.
Tailnet-only names still do not resolve on public DNS (a public resolver returns
NXDOMAIN), but a `--public` server has to resolve publicly by definition. Assume
the address is discoverable and let the token do the work. Whether Funnel surfaces the real client address to the backend
is worth confirming on your first live request — if the gateway sees no
usable address, use `--allow any` and rely on the token.

## How it works

One pod per server, two containers sharing a network namespace:

- **tailscale** (userspace mode, no `/dev/net/tun` needed) joins your tailnet as
  `mcp-<name>` and serves HTTPS on 443 → `127.0.0.1:8081` for private
  servers, or → `127.0.0.1:8080` with `AllowFunnel` for public servers.
- **app** runs a small node gateway on 8080 that spawns
  [supergateway](https://github.com/supercorp-ai/supergateway) on 8081 — which
  wraps the MCP server's stdio into streamable HTTP at `/mcp` — and fronts it
  with the allowlist and bearer checks, passing SSE straight through.

Traffic from your tailnet reaches 8081 directly, since Tailscale ACLs already
gate it. Public mode sends all traffic through the gateway on 443, so it always requires
a token and applies any configured IP allowlist.

Both are built on one shared runtime image (`mcps-base`) carrying node, python,
uv and the bridge, so a repo needs no Dockerfile of its own. `add` detects the
ecosystem from `package.json`, `pyproject.toml` or `requirements.txt` and picks
the install and run commands; `--cmd` overrides the run command when the guess
misses. Before reporting success it runs a real MCP `initialize` handshake
against the published URL, so a server that started but can't answer — usually a
missing API key — is reported as broken instead of live.

Podman labels are the source of truth for what exists.

## Access control on a shared tailnet

Nodes are yours alone on a solo tailnet. If your tailnet has other members, tag
the servers and restrict them:

```
mcps init --tags "--advertise-tags=tag:mcp"
```

```jsonc
"tagOwners": { "tag:mcp": ["autogroup:admin"] },
"nodeAttrs": [{ "target": ["tag:mcp"], "attr": ["funnel"] }],
"acls": [{ "action": "accept", "src": ["you@example.com"], "dst": ["tag:mcp:*"] }]
```

Replace `you@example.com` with your Tailscale login; using `autogroup:member`
would allow every member of a shared tailnet.

The `acls` line matters even on a solo tailnet: a tagged node is owned by the
tailnet rather than by you, so without a rule granting access to the tag it
disappears from your own `tailscale status` and stops resolving in MagicDNS —
while still serving the public internet perfectly, because Funnel bypasses
tailnet ACLs.

## Surviving a reboot

Off by default. `mcps autostart --enable` turns on both halves it needs:

```
mcps autostart            # show the current state
mcps autostart --enable
mcps autostart --disable
```

Every pod already runs with `--restart always`, but rootless podman only replays
that at boot once the *user* `podman-restart.service` is enabled inside the
machine, and it ships disabled. On Windows and macOS the podman VM also has to
start at login, so `--enable` adds a logon entry for `podman machine start` —
a hidden script in your Startup folder on Windows (a scheduled `ONLOGON` task
would need admin), a LaunchAgent on macOS, a systemd user unit on Linux.

Servers come back with the same tailnet hostname and the same token, so
configured clients keep working.

## Configuration

Nothing is baked in. State lives outside the repo and every default is
overridable:

| variable | default |
| --- | --- |
| `MCPS_HOME` | `~/.mcps` — settings, server metadata and clones; no secrets |
| `TS_AUTHKEY` | read by `mcps init` so the key can be set non-interactively |
| `MCPS_TAILSCALE` | path to the host `tailscale` binary |
| `MCPS_BASE_IMAGE` | `localhost/mcps-base:2` |
| `MCPS_TS_IMAGE` | `docker.io/tailscale/tailscale:latest` |

The runtime image takes `--build-arg NODE_IMAGE=...`. Bridge dependencies live in
`src/mcps/runtime/package.json` and its lockfile; update and audit both together. Use `mcps init --http` if your tailnet
has no HTTPS certificates enabled; that mode is tailnet-only, since Funnel is
TLS-only.

## Security and development

Read [SECURITY.md](SECURITY.md) for the trust model, reporting and upgrade steps.
Server names use lowercase letters, digits and hyphens (maximum 59 characters).
The `env` name segment is reserved to prevent collisions with environment-secret
IDs; Windows device names are also rejected. Automatically generated server
names handle these restrictions for you. Older servers using reserved names
need an explicit rename/recreation before management with this release.
Environment names must be valid identifiers; names that map to the same Podman
secret (such as `FOO_BAR` and `FOO__BAR`) cannot be combined.

The hardened runtime uses a new base-image revision, so the next `mcps add --force`
builds it automatically unless `MCPS_BASE_IMAGE` overrides that default. Existing
running containers still need rebuilding; use `--rebuild-base` when refreshing
an explicitly configured runtime image.

```sh
python -m pip install -e .
python -m unittest discover -s tests -v
node --test tests/gateway.test.js
```

CI runs these tests and a Python dependency vulnerability audit. Fork workflows
require owner approval before execution. Hosted MCP packages and runtime image
contents need their own dependency review; tests do not certify arbitrary servers.
