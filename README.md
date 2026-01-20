# murl - MCP Curl

A curl-like CLI tool for interacting with Model Context Protocol (MCP) servers.

## What is MCP?

MCP (Model Context Protocol) is an open standard developed by Anthropic for AI models to access external data sources, tools, and services. It provides a universal way for large language models (LLMs) to interact with various resources securely and efficiently.

## Installation

### Using pip

```bash
pip install murl
```

### Using Homebrew (Coming Soon)

```bash
brew install murl
```

### From Source

```bash
git clone https://github.com/turlockmike/murl.git
cd murl
pip install -e .
```

## Usage

```bash
# Display version
murl --version

# Show help
murl --help
```

## Development

### Setup Development Environment

```bash
# Clone the repository
git clone https://github.com/turlockmike/murl.git
cd murl

# Install in development mode with dev dependencies
pip install -e ".[dev]"
```

### Running Tests

```bash
pytest
```

## Features (Coming Soon)

- Connect to MCP servers
- Send requests to MCP endpoints
- Display formatted responses
- Support for various MCP operations

## License

MIT License - see LICENSE file for details
