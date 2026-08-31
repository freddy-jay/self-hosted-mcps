// Runs supergateway on 8081 and fronts it on 8080 with two gates:
//   MCP_ALLOW_CIDRS - source IP allowlist, for servers published over Funnel
//   MCP_TOKEN       - bearer token, as a header or as a path segment
// Both are what make `mcps add --public` safe: a Funnel URL is public and guessable.
const http = require("node:http");
const crypto = require("node:crypto");
const { spawn } = require("node:child_process");

const TOKEN = process.env.MCP_TOKEN || "";
// Silent mode drops unauthorised connections instead of answering them, so a
// scanner gets a reset rather than a 401 confirming something is listening.
const SILENT = process.env.MCP_SILENT === "1";
const UPSTREAM = 8081;
const PORT = Number(process.env.MCP_PORT || 8080);

const child = spawn(
  "supergateway",
  [
    "--stdio", process.env.MCP_CMD,
    "--outputTransport", "streamableHttp",
    "--port", String(UPSTREAM),
    "--streamableHttpPath", "/mcp",
    "--healthEndpoint", "/healthz",
    "--stateful",
    "--cors",
  ],
  { stdio: "inherit" },
);
child.on("exit", (code) => process.exit(code === null ? 1 : code));

// --- source IP allowlist ---------------------------------------------------

function ipToBigInt(ip) {
  if (ip.includes(":")) {
    const [head, tail] = ip.split("::");
    const left = head ? head.split(":") : [];
    const right = tail ? tail.split(":") : [];
    const groups = ip.includes("::")
      ? [...left, ...Array(8 - left.length - right.length).fill("0"), ...right]
      : ip.split(":");
    if (groups.length !== 8) return null;
    return groups.reduce((acc, g) => (acc << 16n) + BigInt(parseInt(g || "0", 16)), 0n);
  }
  const octets = ip.split(".");
  if (octets.length !== 4) return null;
  return octets.reduce((acc, o) => (acc << 8n) + BigInt(Number(o)), 0n);
}

function parseCidr(entry) {
  const [addr, bitsRaw] = entry.split("/");
  const value = ipToBigInt(addr);
  if (value === null) return null;
  const width = addr.includes(":") ? 128 : 32;
  const bits = bitsRaw === undefined ? width : Number(bitsRaw);
  const mask = ((1n << BigInt(bits)) - 1n) << BigInt(width - bits);
  return { network: value & mask, mask, width };
}

const ALLOW = (process.env.MCP_ALLOW_CIDRS || "")
  .split(",")
  .map((entry) => entry.trim())
  .filter(Boolean)
  .map(parseCidr)
  .filter(Boolean);

function normalise(ip) {
  return (ip || "").replace(/^::ffff:/, "").replace(/^\[|\]$/g, "").split("%")[0];
}

// tailscaled proxies from inside the pod, so a loopback peer means the real
// client address can only come from the header - and only its rightmost entry,
// because anything a client sends itself is appended to the left of that.
function clientIp(req) {
  const peer = normalise(req.socket.remoteAddress);
  if (peer !== "127.0.0.1" && peer !== "::1") return peer;
  const forwarded = (req.headers["x-forwarded-for"] || "").split(",").map((s) => s.trim()).filter(Boolean);
  return forwarded.length ? normalise(forwarded[forwarded.length - 1]) : peer;
}

function allowed(ip) {
  if (!ALLOW.length) return true; // no allowlist configured
  const value = ipToBigInt(ip);
  if (value === null) return false;
  return ALLOW.some((cidr) => {
    const sameFamily = (ip.includes(":") ? 128 : 32) === cidr.width;
    return sameFamily && (value & cidr.mask) === cidr.network;
  });
}

// --- bearer token ----------------------------------------------------------

function constantEquals(a, b) {
  const left = Buffer.from(a);
  const right = Buffer.from(b);
  return left.length === right.length && crypto.timingSafeEqual(left, right);
}

// Returns the upstream path, or null when the request is not authorised.
function authorise(req) {
  if (!TOKEN) return req.url;
  const header = req.headers.authorization || "";
  if (header.startsWith("Bearer ") && constantEquals(header.slice(7), TOKEN)) {
    return req.url;
  }
  // Token-in-path, for clients with no way to set a header.
  const prefix = `/mcp/${TOKEN}`;
  if (req.url === prefix || req.url.startsWith(`${prefix}/`) || req.url.startsWith(`${prefix}?`)) {
    return `/mcp${req.url.slice(prefix.length)}`;
  }
  return null;
}

// --- proxy -----------------------------------------------------------------

http
  .createServer((req, res) => {
    const source = clientIp(req);
    if (!allowed(source)) {
      // Logged in full so `mcps logs <name>` answers "what address did it arrive from?"
      console.log(
        `[mcps] blocked ${req.method} ${req.url} from ${source} ` +
          `(allowed: ${process.env.MCP_ALLOW_CIDRS}, xff: ${req.headers["x-forwarded-for"] || "none"})`,
      );
      res.writeHead(403, { "Content-Type": "text/plain" });
      res.end(`forbidden: ${source} is not in this server's allowlist\n`);
      return;
    }

    // Past the allowlist, so this never answers an unauthorised public caller.
    // Liveness from inside the pod goes to the bridge's own /healthz on 8081.
    if (req.url === "/healthz") {
      res.writeHead(200, { "Content-Type": "text/plain" });
      res.end("ok");
      return;
    }

    const path = authorise(req);
    if (path === null) {
      console.log(`[mcps] rejected ${req.method} ${req.url} from ${source}: bad or missing token`);
      if (SILENT) {
        req.socket.destroy();
        return;
      }
      res.writeHead(401, {
        "Content-Type": "text/plain",
        "WWW-Authenticate": 'Bearer realm="mcp"',
      });
      res.end("unauthorized");
      return;
    }

    const headers = { ...req.headers };
    delete headers.host;
    delete headers.connection;

    const upstream = http.request(
      { host: "127.0.0.1", port: UPSTREAM, method: req.method, path, headers },
      (upstreamRes) => {
        res.writeHead(upstreamRes.statusCode, upstreamRes.headers);
        res.flushHeaders(); // SSE: get the headers out before the first event
        upstreamRes.pipe(res);
      },
    );
    upstream.on("error", () => {
      if (!res.headersSent) res.writeHead(502, { "Content-Type": "text/plain" });
      res.end("upstream unavailable");
    });
    req.pipe(upstream);
  })
  .listen(PORT, "0.0.0.0", () => {
    console.log(
      `[mcps] gateway on ${PORT} -> supergateway ${UPSTREAM}` +
        `${TOKEN ? " (bearer required)" : ""}` +
        `${ALLOW.length ? ` (allowlist: ${process.env.MCP_ALLOW_CIDRS})` : ""}` +
        `${SILENT ? " (silent)" : ""}`,
    );
  });
