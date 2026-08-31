# mcps

Self-host any MCP server. Give it a GitHub link; it builds a Podman pod with a
Tailscale sidecar and hands you an HTTPS URL.

```
mcps add modelcontextprotocol/servers --subdir src/filesystem --name fs
-> https://mcp-fs.your-tailnet.ts.net/mcp
```

## Install

```
uv tool install .
mcps init          # paste a Tailscale auth key
mcps doctor        # confirms podman, git, tailscale, key
```

Podman must be running (`podman machine start` on Windows/macOS).

The auth key needs to be **reusable**, **ephemeral** and **pre-approved**
(tailscale.com/admin/settings/keys). Ephemeral means a removed server leaves no
dead node behind.

## Commands

```
mcps add <target>   build a server and put it on your tailnet
mcps ls             what's running, and its URL
mcps logs <name>    server output (--tailscale for the sidecar, -f to follow)
mcps restart <name>
mcps token <name>   show the bearer token (--rotate to replace it)
mcps secrets        add / ls / rm a server's environment secrets
mcps autostart      bring servers back after a reboot (--enable / --disable)
mcps rm <name>      pod, tailnet node, image, secret, sources
mcps doctor
```

`<target>` is `owner/repo`, a full git URL, `npm:<package>` or `pypi:<package>`.

Useful flags on `add`:

| flag | why |
| --- | --- |
| `--public` | also publish it on the internet, for clients that aren't on your tailnet |
| `--new-token` | rotate the bearer token; without it a rebuild keeps the existing one |
| `--token-stdin` | read the token from stdin instead of generating one |
| `--allow <cidr>` | who may reach a `--public` server; repeatable, `any` disables the check |
| `--silent` | drop unauthorised requests with no reply at all, instead of answering 401 |
| `--subdir src/foo` | monorepos — the whole repo stays the build context, so shared tsconfig/workspace files resolve |
| `--cmd "node dist/x.js"` | the entrypoint guess was wrong |
| `-e KEY=VALUE`, `--env-file` | API keys the server needs |
| `--ref v1.2.0` | pin a branch or tag |
| `--name` | the tailnet hostname becomes `mcp-<name>` |

## Which clients can reach it

By default a server is **tailnet-only**: reachable from your own devices and
nothing else. That covers Claude Code and Claude Desktop, which run on a machine
you control and can join your tailnet.

**claude.ai cannot.** Anthropic's servers reach a custom connector over the
public internet, so a URL that only resolves inside your tailnet never connects.
For that, add the server with `--public`:

```
mcps add pypi:mcp-server-time --name time --public
```

That leaves the tailnet URL exactly as it was and opens a *second* endpoint on
port 8443 via [Tailscale Funnel](https://tailscale.com/kb/1223/funnel), so only
the public leg carries the gates:

```
https://mcp-time.your-tailnet.ts.net/mcp             tailnet, no token
https://mcp-time.your-tailnet.ts.net:8443/mcp        public, token + IP allowlist
```

Funnel is ACL-gated and Tailscale **ignores it silently** when the attribute is
missing: the local config reports "Funnel on" while the control plane publishes
no public DNS record, so the name resolves inside your tailnet and nowhere else.
`add --public` therefore checks the name against a public resolver over HTTPS (a
plain lookup from a tailnet machine is answered by MagicDNS and never leaves the
host) and prints the policy snippet you need at login.tailscale.com/admin/acls:

```jsonc
"nodeAttrs": [{ "target": ["autogroup:member"], "attr": ["funnel"] }]
```

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
`mcps token <name>` prints it, `--rotate` replaces it, `--new-token` on `add`
does the same during a rebuild.

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

A public server accepts requests only from Anthropic's published outbound range,
[`160.79.104.0/21`](https://platform.claude.com/docs/en/api/ip-addresses). Set
your own with `--allow`, repeatable, or `--allow any` to switch the check off:

```
mcps add owner/repo --public --allow 160.79.104.0/21 --allow 203.0.113.4/32
```

The check reads the **rightmost** `X-Forwarded-For` entry and only trusts the
header because the only thing that can reach the gateway is tailscaled on
loopback — a client-injected value is appended to the left of the real one and
never wins. Anthropic publishes no IPv6 outbound range, so an IPv6 source is
rejected unless you allow one explicitly.

It **fails closed**: a request whose source address can't be established is
blocked, and the rejection is logged with the address it did see:

```
mcps logs time
[mcps] blocked POST /mcp from 127.0.0.1 (allowed: 160.79.104.0/21, xff: none)
```

The allowlist is not authorization: every claude.ai user's connector calls leave
from that same Anthropic range, so it narrows the field from "the whole internet"
to "someone who already has your URL and token" — the token is what identifies
you. Treat it as the control that carries the weight, and the allowlist as
defence in depth.

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
is worth confirming on your first live request — if that log line shows no
usable address, use `--allow any` and rely on the token.

## How it works

One pod per server, two containers sharing a network namespace:

- **tailscale** (userspace mode, no `/dev/net/tun` needed) joins your tailnet as
  `mcp-<name>` and serves HTTPS on 443 → `127.0.0.1:8081`, plus 8443 →
  `127.0.0.1:8080` with `AllowFunnel` when the server is public.
- **app** runs a small node gateway on 8080 that spawns
  [supergateway](https://github.com/supercorp-ai/supergateway) on 8081 — which
  wraps the MCP server's stdio into streamable HTTP at `/mcp` — and fronts it
  with the allowlist and bearer checks, passing SSE straight through.

Traffic from your tailnet reaches 8081 directly, since Tailscale ACLs already
gate it. Public traffic can only arrive on 8443, so it always passes both gates.

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
"acls": [{ "action": "accept", "src": ["autogroup:member"], "dst": ["tag:mcp:*"] }]
```

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
| `MCPS_BASE_IMAGE` | `localhost/mcps-base:1` |
| `MCPS_TS_IMAGE` | `docker.io/tailscale/tailscale:latest` |
| `MCPS_ANTHROPIC_CIDRS` | `160.79.104.0/21`, the default `--allow` value |

The runtime image takes `--build-arg NODE_IMAGE=...` and
`--build-arg SUPERGATEWAY_VERSION=...`. Use `mcps init --http` if your tailnet
has no HTTPS certificates enabled; that mode is tailnet-only, since Funnel is
TLS-only.
