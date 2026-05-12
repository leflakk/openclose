"""HTTP fetch tool — retrieves web content."""

from __future__ import annotations

import httpx

from openclose.tool.tool import Tool, ToolResult, ToolParameter
from openclose.tool.truncation import truncate_output


def make_webfetch_tool() -> Tool:
    """Create the web fetch tool."""

    async def execute(
        url: str = "",
        **kwargs: object,
    ) -> ToolResult:
        if not url:
            return ToolResult(error="URL is required")

        try:
            async with httpx.AsyncClient(
                follow_redirects=True, timeout=30.0
            ) as client:
                response = await client.get(url)
                response.raise_for_status()
        except httpx.HTTPStatusError as e:
            return ToolResult(error=f"HTTP {e.response.status_code}: {e}")
        except Exception as e:
            return ToolResult(error=f"Fetch error: {e}")

        content_type = response.headers.get("content-type", "")
        text = response.text

        # Convert HTML to plain text to save context tokens
        if "text/html" in content_type:
            try:
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(text, "html.parser")
                for tag in soup(["script", "style", "nav", "footer",
                                 "header", "aside", "noscript"]):
                    tag.decompose()
                text = soup.get_text(separator="\n", strip=True)
            except Exception:
                pass  # graceful degradation — return raw HTML

        return ToolResult(
            output=truncate_output(text),
            metadata={
                "url": url,
                "status": response.status_code,
                "content_type": content_type,
            },
        )

    return Tool(
        name="webfetch",
        description=(
            "USE IT TO RETRIEVE FROM A KNOWN URL - webpage content, documentation, API references, an "
            "upstream PR/commit/issue page linked from a bug report."
        ),
        parameters=[
            ToolParameter(
                name="url",
                description=(
                    "Absolute URL to fetch (must include scheme, e.g. "
                    "`https://...`). Only GET is supported. Use a URL "
                    "provided by the user or one found inside content "
                    "already fetched — do not invent or guess URLs."
                ),
            ),
        ],
        execute_fn=execute,
    )
