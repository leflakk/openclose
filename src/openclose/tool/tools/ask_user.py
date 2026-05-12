"""Ask-user tool — allows the agent to ask the user multiple-choice questions."""

from __future__ import annotations

from typing import Any

from openclose.tool.tool import Tool, ToolResult, ToolParameter


def make_ask_user_tool() -> Tool:
    """Create the ask_user tool.

    The actual user interaction is handled by the agent loop, which
    detects the ``awaiting_ask_user`` metadata and delegates to the
    AskUserBroker.  The tool itself just validates and returns the marker
    so the loop knows to suspend.
    """

    async def execute(
        questions: list[dict[str, Any]] | None = None,
        **kwargs: object,
    ) -> ToolResult:
        if isinstance(questions, str):
            return ToolResult(
                error=(
                    "`questions` was passed as a string. Pass it as a real "
                    "JSON array of objects, e.g. "
                    "[{\"question\": \"...\", \"choices\": [\"A\", \"B\"]}]"
                )
            )
        if not questions or not isinstance(questions, list):
            return ToolResult(error="questions must be a non-empty list")

        if len(questions) > 10:
            return ToolResult(error="Maximum 10 questions allowed")

        validated: list[dict[str, Any]] = []
        for i, q in enumerate(questions):
            if not isinstance(q, dict):
                return ToolResult(error=f"Question {i + 1} must be an object")
            text = q.get("question", "")
            if not text or not isinstance(text, str):
                return ToolResult(
                    error=f"Question {i + 1} must have a non-empty 'question' string"
                )
            choices = q.get("choices", [])
            if not isinstance(choices, list) or len(choices) < 2:
                return ToolResult(
                    error=f"Question {i + 1} must have at least 2 choices"
                )
            for j, c in enumerate(choices):
                if not isinstance(c, str) or not c:
                    return ToolResult(
                        error=f"Question {i + 1}, choice {j + 1} must be a non-empty string"
                    )
            validated.append({"question": text, "choices": choices})

        return ToolResult(
            output="",
            metadata={"questions": validated, "awaiting_ask_user": True},
        )

    return Tool(
        name="ask_user",
        description=(
            "USE IT TO RESOLVE KEY DECISIONS or ambiguities you cannot "
            "answer from the codebase alone (preferences, scope choices, "
            "naming, picking between equally valid approaches)."
        ),
        parameters=[
            ToolParameter(
                name="questions",
                type="array",
                description=(
                    "Ordered list of up to 10 question objects to ask in one "
                    "batch. Prefer a single call with several questions over "
                    "asking one at a time. Must be a JSON array of objects — "
                    "do NOT stringify it. Example: "
                    "[{\"question\": \"Keep the legacy endpoint?\", "
                    "\"choices\": [\"Keep it\", \"Remove it\"]}, "
                    "{\"question\": \"Name for the new flag?\", "
                    "\"choices\": [\"enable_v2\", \"use_new_pipeline\"]}]"
                ),
                items={
                    "type": "object",
                    "properties": {
                        "question": {
                            "type": "string",
                            "description": (
                                "The question text shown to the user. Be "
                                "specific and self-contained — give enough "
                                "context that the user can answer without "
                                "re-reading the conversation."
                            ),
                        },
                        "choices": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "At least 2 short option strings shown as "
                                "clickable buttons. Make them mutually "
                                "exclusive and self-explanatory; the user "
                                "picks exactly one."
                            ),
                        },
                    },
                    "required": ["question", "choices"],
                },
            ),
        ],
        execute_fn=execute,
    )
