const { test } = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const { EventEmitter } = require('node:events');

// Isolate process/network side effects, exercising the actual gateway handler.
function gateway(env = {}) {
  const state = { logs: [], forwarded: [], child: null };
  const http = {
    createServer(handler) {
      state.handler = handler;
      return { listen(port, host) { state.host = host; } };
    },
    request(options, onResponse) {
      state.forwarded.push(options);
      state.respond = onResponse;
      state.upstream = new EventEmitter();
      state.upstream.destroy = () => { state.upstreamDestroyed = true; };
      return state.upstream;
    },
  };
  const context = {
    require(name) {
      if (name === 'node:http') return http;
      if (name === 'node:child_process') return { spawn(cmd, args, opts) {
        state.child = { cmd, args, opts }; return new EventEmitter();
      } };
      return require(name);
    },
    Buffer,
    process: { env: { MCP_CMD: 'test-server', MCP_TOKEN: 'test-token', ...env },
      execPath: '/usr/bin/node', exit(code) { throw new Error(`exit ${code}`); }, on() {}, },
    console: { log(s) { state.logs.push(s); }, error(s) { state.logs.push(s); } },
  };
  vm.runInNewContext(fs.readFileSync('src/mcps/runtime/gateway.js', 'utf8'), context);
  state.request = (url, headers = {}) => {
    const res = Object.assign(new EventEmitter(), { status: 0, body: '', piped: false,
      writeHead(status, headers) { this.status = status; this.headers = headers; }, flushHeaders() {},
      end(chunk) { this.body += chunk || ''; } });
    state.response = res;
    state.handler({ url, method: 'POST', headers,
      socket: { remoteAddress: '127.0.0.1', destroy() { state.socketDestroyed = true; } }, pipe() {}, on() {} }, res);
    return res.status;
  };
  return state;
}

test('invalid configured CIDRs fail closed at startup', () => {
  for (const cidr of ['garbage', '10.0.0.0/33', '::/129', '10.0.0.1/abc']) {
    assert.throws(() => gateway({ MCP_ALLOW_CIDRS: cidr }), undefined, cidr);
  }
});

test('silent mode also drops allowlist rejections', () => {
  const g = gateway({ MCP_ALLOW_CIDRS: '192.0.2.0/24', MCP_SILENT: '1' });
  assert.equal(g.request('/mcp'), 0);
  assert.equal(g.socketDestroyed, true);
});
test('client disconnect destroys upstream connection', () => {
  const g = gateway();
  g.request('/mcp', { authorization: 'Bearer test-token' });
  g.response.emit('close');
  assert.equal(g.upstreamDestroyed, true);
});
test('malformed source cannot crash the gateway or match an allowlist', () => {
  const g = gateway({ MCP_ALLOW_CIDRS: '160.79.104.0/21' });
  for (const ip of ['x.y.z.q', '160.79.104.256', ':::::', '::gggg']) {
    assert.equal(g.request('/mcp', { 'x-forwarded-for': ip }), 403);
  }
});
test('rejected requests never log URL credentials or query strings', () => {
  const g = gateway({ MCP_ALLOW_CIDRS: '192.0.2.0/24' });
  g.request('/mcp/test-token?secret=private');
  assert.ok(!g.logs.join('\n').includes('test-token'));
  assert.ok(!g.logs.join('\n').includes('private'));
});
test('credentials are consumed at the gateway, not passed to the MCP process', () => {
  const g = gateway();
  g.request('/mcp/test-token', { authorization: 'Bearer test-token' });
  assert.equal(g.forwarded[0].path, '/mcp');
  assert.equal(g.forwarded[0].headers.authorization, undefined);
  assert.equal(g.child.opts.env.MCP_TOKEN, undefined);
});
test('public mode refuses startup without a token', () => {
  assert.throws(() => gateway({ MCP_TOKEN: '', MCP_REQUIRE_TOKEN: '1' }));
});
test('valid bearer and URL tokens work; bad tokens and health probes require auth', () => {
  const g = gateway();
  assert.equal(g.request('/mcp'), 401);
  assert.equal(g.request('/healthz'), 401);
  g.request('/mcp', { authorization: 'Bearer test-token' });
  g.request('/mcp/test-token');
  assert.equal(g.forwarded.length, 2);
  assert.equal(g.forwarded[1].path, '/mcp');
});
test('the trusted rightmost address wins; gateway binds only loopback', () => {
  const g = gateway({ MCP_ALLOW_CIDRS: '192.0.2.0/24' });
  assert.equal(g.request('/mcp', { 'x-forwarded-for': '192.0.2.3, 203.0.113.1' }), 403);
  g.request('/mcp', { 'x-forwarded-for': '203.0.113.1, 192.0.2.3', authorization: 'Bearer test-token' });
  assert.equal(g.forwarded.length, 1);
  assert.equal(g.host, '127.0.0.1');
});

// What the bridge sends back, as the gateway's upstream callback receives it.
function upstreamAnswer(g, status, body) {
  const upstreamRes = Object.assign(new EventEmitter(), { statusCode: status,
    headers: { 'content-type': 'application/json' }, pipe(res) { res.piped = true; } });
  g.respond(upstreamRes);
  upstreamRes.emit('data', Buffer.from(body));
  upstreamRes.emit('end');
  return g.response;
}
const NO_SESSION = '{"jsonrpc":"2.0","error":{"code":-32000,"message":"Bad Request: No valid session ID provided"},"id":null}';
const BAD_VERSION = '{"jsonrpc":"2.0","error":{"code":-32000,"message":"Bad Request: Unsupported protocol version: 1999-01-01"},"id":null}';
const AUTH = { authorization: 'Bearer test-token' };

test('a session the bridge no longer has is answered 404, which tells clients to start a new one', () => {
  const g = gateway();
  g.request('/mcp', { ...AUTH, 'mcp-session-id': 'expired' });
  const res = upstreamAnswer(g, 400, NO_SESSION);
  assert.equal(res.status, 404);
  assert.equal(res.body, NO_SESSION);
});
test('other bad requests keep their 400, with or without a session', () => {
  const g = gateway();
  g.request('/mcp', { ...AUTH, 'mcp-session-id': 'live' });
  assert.equal(upstreamAnswer(g, 400, BAD_VERSION).status, 400);
  assert.equal(g.response.body, BAD_VERSION);
  g.request('/mcp', AUTH);
  assert.equal(upstreamAnswer(g, 400, NO_SESSION).status, 400);
});
test('successful answers are streamed through untouched', () => {
  const g = gateway();
  g.request('/mcp', { ...AUTH, 'mcp-session-id': 'live' });
  const res = upstreamAnswer(g, 200, 'event: message');
  assert.equal(res.status, 200);
  assert.equal(res.piped, true);
  assert.equal(res.body, '');
});
test('an oversized error body keeps its 400 and is never buffered without limit', () => {
  const g = gateway();
  g.request('/mcp', { ...AUTH, 'mcp-session-id': 'expired' });
  const res = upstreamAnswer(g, 400, NO_SESSION + 'x'.repeat(70000));
  assert.equal(res.status, 400);
  assert.equal(res.body.length, 0);
});
test('idle sessions are kept for a day, not ten minutes', () => {
  const args = gateway().child.args;
  assert.equal(args[args.indexOf('--sessionTimeout') + 1], String(24 * 60 * 60 * 1000));
  assert.ok(args.includes('--stateful'));
});
test('the bridge runs under node with unhandled rejections downgraded, and the server does not inherit that', () => {
  const dir = fs.mkdtempSync(require('node:path').join(require('node:os').tmpdir(), 'mcps-bridge-'));
  const bin = require('node:path').join(dir, 'supergateway');
  fs.writeFileSync(bin, '');
  try {
    const found = gateway({ PATH: ['/nonexistent', dir].join(require('node:path').delimiter) }).child;
    assert.equal(found.cmd, '/usr/bin/node');
    // Spread: the array comes from the gateway's vm realm, so its prototype differs.
    assert.deepEqual([...found.args.slice(0, 3)], ['--unhandled-rejections=warn', fs.realpathSync(bin), '--stdio']);
    assert.equal(found.opts.env.NODE_OPTIONS, undefined);
  } finally {
    fs.rmSync(dir, { recursive: true });
  }
  const fallback = gateway({ PATH: '/nonexistent' }).child;
  assert.equal(fallback.cmd, 'supergateway');
  assert.equal(fallback.args[0], '--stdio');
});
