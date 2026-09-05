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
    request(options) {
      state.forwarded.push(options);
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
      exit(code) { throw new Error(`exit ${code}`); }, on() {}, },
    console: { log(s) { state.logs.push(s); }, error(s) { state.logs.push(s); } },
  };
  vm.runInNewContext(fs.readFileSync('src/mcps/runtime/gateway.js', 'utf8'), context);
  state.request = (url, headers = {}) => {
    const res = Object.assign(new EventEmitter(), { status: 0, writeHead(status) { this.status = status; }, end() {} });
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
