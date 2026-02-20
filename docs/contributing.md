# Contributing

## Development Setup

```bash
git clone https://github.com/turlockmike/murl.git
cd murl
pip install -e ".[dev]"
```

## Running Tests

```bash
pytest
pytest --cov=murl --cov-report=html
```

## How It Works

murl translates REST-like URLs into MCP JSON-RPC 2.0 requests:

1. **Parses the URL** — extracts the base endpoint and virtual path
2. **Maps the path** — converts to the appropriate MCP method (`/tools` → `tools/list`, `/tools/echo` → `tools/call`)
3. **Parses `-d` flags** — builds method parameters with automatic type coercion
4. **Sends JSON-RPC** — HTTP POST to the base endpoint
5. **Returns the result** — extracts and prints the JSON-RPC response

## Releasing

Update the version in `pyproject.toml` and `murl/__init__.py`, then tag:

```bash
git tag v0.X.Y
git push origin v0.X.Y
```

This triggers a GitHub Actions workflow that:
1. Builds and publishes to PyPI
2. Creates a GitHub release with artifacts
3. Updates the [Homebrew tap](https://github.com/turlockmike/homebrew-murl) formula automatically
