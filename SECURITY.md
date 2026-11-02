# Security

mcp-secret exists to keep personal data away from an AI agent, so a leak is a vulnerability: a
value that reaches `public/public.db` or an MCP tool output in clear, a way past the `serve`
sandbox, or a path from a token back to its value.

Report it privately: **Security → Report a vulnerability** on this repository. Describe what
leaks and how to reproduce it with generated data (see `fixtures/generate.py`); never attach
real personal data.

A value the detector merely misses is a known limit, not a vulnerability: see Residual risks
in the [README](README.md#residual-risks). Open a normal issue for those, with made-up values.
