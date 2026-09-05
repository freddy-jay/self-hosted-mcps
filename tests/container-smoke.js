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
  console.log('Container smoke passed: auth, initialize, sessions, tools, URL fallback, child environment scoping');
}
main().then(() => process.exitCode = 0, error => {
  console.error(error); process.exitCode = 1;
}).finally(() => gateway.kill());
