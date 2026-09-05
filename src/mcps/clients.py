"""Credential-free setup for OpenAI clients of the existing MCP transport."""
import json
from urllib.parse import urlsplit

from .validation import server_name


def render(name: str, meta: dict, client: str) -> str:
    server_name(name)
    url = meta.get("url", "")
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("server has no valid HTTP endpoint; run mcps add first")
    public = bool(meta.get("public"))
    variable = "MCPS_" + name.upper().replace("-", "_") + "_TOKEN"
    if client == "codex":
        lines = [f'[mcp_servers.{json.dumps(name)}]', f'url = {json.dumps(url)}']
        if public:
            lines.append(f'bearer_token_env_var = {json.dumps(variable)}')
        return "\n".join(lines)
    if client == "openai":
        if not public or parsed.scheme != "https":
            raise ValueError("OpenAI hosted MCP calls need a public HTTPS endpoint; re-add with --public")
        tool = {"type": "mcp", "server_label": name.replace("-", "_"),
                "server_url": url, "require_approval": "always"}
        payload = json.dumps(tool, indent=4)
        payload = payload[:-2] + f',\n    "authorization": os.environ[{json.dumps(variable)}]\n}}'
        return (
            "import os\nfrom openai import OpenAI\n\n"
            "# Install: pip install openai\n"
            "# Set OPENAI_API_KEY, OPENAI_MODEL, and the MCP token environment variable.\n"
            "client = OpenAI()\n"
            f"mcp_tool = {payload}\n\n"
            "response = client.responses.create(\n"
            '    model=os.environ["OPENAI_MODEL"],\n'
            '    input="List the tools available from this MCP server.",\n'
            "    tools=[mcp_tool],\n    store=False,\n)\n"
            "# Review mcp_approval_request items before submitting an mcp_approval_response.\n"
            "print(response.output)"
        )
    raise ValueError("--client must be codex or openai")
