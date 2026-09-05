// Minimal stdio fixture: no external services, credentials, or tool side effects.
const readline = require('node:readline');
readline.createInterface({ input: process.stdin }).on('line', (line) => {
  const request = JSON.parse(line);
  if (request.id === undefined) return;
  let result;
  if (request.method === 'initialize') {
    result = { protocolVersion: '2025-06-18', capabilities: { tools: {} },
      serverInfo: { name: 'security-fixture', version: '1' } };
  } else if (request.method === 'tools/list') {
    result = { tools: [{ name: 'environment_check', description: 'Inspect test credential isolation',
      inputSchema: { type: 'object', properties: {} } }] };
  } else if (request.method === 'tools/call') {
    result = { content: [{ type: 'text', text: process.env.MCP_TOKEN ? 'leaked' : 'isolated' }] };
  } else {
    process.stdout.write(JSON.stringify({ jsonrpc: '2.0', id: request.id,
      error: { code: -32601, message: 'Method not found' } }) + '\n');
    return;
  }
  process.stdout.write(JSON.stringify({ jsonrpc: '2.0', id: request.id, result }) + '\n');
});
