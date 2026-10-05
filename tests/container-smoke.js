// Run inside the isolated runtime container; exercises the real supergateway.
const { spawn } = require('node:child_process');
const assert = require('node:assert/strict');
const { setTimeout: delay } = require('node:timers/promises');
const token = 'test-only-token-01234567890123456789';
const gateway = spawn('node', ['/usr/local/lib/mcps-gateway.js'], {
  env: { ...process.env, MCP_CMD: 'node /tests/fixtures/mcp-server.js',
    MCP_TOKEN: token, MCP_REQUIRE_TOKEN: '1' }, stdio: 'inherit',
});

async function main() {
  const url = 'http://127.0.0.1:8080/mcp';
  let ready = false;
  for (let i = 0; i < 100; i++) {
    try {
      if ((await fetch('http://127.0.0.1:8081/healthz')).ok) { ready = true; break; }
    } catch {}
    await delay(100);
  }
  assert.ok(ready, 'bridge became healthy');
  assert.equal((await fetch(url)).status, 401);
  assert.equal((await fetch(url, { headers: { Authorization: 'Bearer wrong' } })).status, 401);
  const headers = { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json',
    Accept: 'application/json, text/event-stream' };
  async function rpc(payload, endpoint = url) {
    const response = await fetch(endpoint, { method: 'POST', headers, body: JSON.stringify(payload),
      signal: AbortSignal.timeout(10000) });
    assert.ok(response.ok, `RPC ${payload.method}: ${response.status}`);
    const session = response.headers.get('mcp-session-id');
    if (session) headers['mcp-session-id'] = session;
    const text = await response.text();
    if (!text) return null;
    const data = text.split('\n').find(line => line.startsWith('data:'));
    const message = JSON.parse(data ? data.slice(5) : text);
    assert.equal(message.error, undefined, JSON.stringify(message));
    return message.result;
  }
  await rpc({ jsonrpc: '2.0', id: 1, method: 'initialize', params: {
    protocolVersion: '2025-06-18', capabilities: {}, clientInfo: { name: 'smoke', version: '1' },
  } });
  await rpc({ jsonrpc: '2.0', method: 'notifications/initialized' });
  const tools = await rpc({ jsonrpc: '2.0', id: 2, method: 'tools/list' });
  assert.equal(tools.tools[0].name, 'environment_check');
  const result = await rpc({ jsonrpc: '2.0', id: 3, method: 'tools/call',
    params: { name: 'environment_check', arguments: {} } });
  assert.equal(result.content[0].text, 'isolated');
  delete headers.Authorization;
  const viaPath = await rpc({ jsonrpc: '2.0', id: 4, method: 'tools/list' }, `${url}/${token}`);
  assert.equal(viaPath.tools.length, 1);
  headers.Authorization = `Bearer ${token}`;
  const live = headers['mcp-session-id'];

  // A client that hangs up before its answer is ready must not take the bridge,
  // and with it every other session, down. The response headers arrive at once,
  // so the hang-up happens while the body is still being awaited.
  await assert.rejects(fetch(url, { method: 'POST', headers, signal: AbortSignal.timeout(200),
    body: JSON.stringify({ jsonrpc: '2.0', id: 5, method: 'tools/call',
      params: { name: 'environment_check', arguments: { delay_ms: 1500 } } }) })
    .then(response => response.text()), { name: 'TimeoutError' });
  await delay(2500);
  assert.equal(gateway.exitCode, null, 'gateway and bridge survived a dropped client');
  const survived = await rpc({ jsonrpc: '2.0', id: 6, method: 'tools/list' });
  assert.equal(survived.tools.length, 1);

  // A session the bridge does not have is answered 404 on every method: that
  // is the MCP signal for a client to start a new session.
  for (const method of ['POST', 'GET', 'DELETE']) {
    const lost = await fetch(url, { method, signal: AbortSignal.timeout(10000),
      headers: { ...headers, 'mcp-session-id': '00000000-0000-4000-8000-000000000000' },
      body: method === 'POST' ? JSON.stringify({ jsonrpc: '2.0', id: 7, method: 'tools/list' }) : undefined });
    assert.equal(lost.status, 404, `${method} with an unknown session`);
    await lost.arrayBuffer();
  }
  // None of that disturbed the session that is still live.
  const still = await rpc({ jsonrpc: '2.0', id: 8, method: 'tools/list' });
  assert.equal(still.tools.length, 1);
  assert.equal(headers['mcp-session-id'], live);
  // A client that starts over gets a new session.
  delete headers['mcp-session-id'];
  await rpc({ jsonrpc: '2.0', id: 9, method: 'initialize', params: {
    protocolVersion: '2025-06-18', capabilities: {}, clientInfo: { name: 'smoke', version: '1' },
  } });
  assert.notEqual(headers['mcp-session-id'], live);
  console.log('Container smoke passed: auth, initialize, sessions, dropped client, lost-session 404, tools, URL fallback, child environment scoping');
}
main().then(() => process.exitCode = 0, error => {
  console.error(error); process.exitCode = 1;
}).finally(() => gateway.kill());
