"""Entry point for the YouTube MCP server.

Usage: python -m youtube_mcp
"""

from .coordinator import mcp
from . import tools  # noqa: F401  -- importing registers the @mcp.tool() functions


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
