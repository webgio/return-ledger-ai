"""Same extraction as `extract.py`, provider-agnostic, through LangChain only.

Nothing here imports a vendor SDK or a provider package: the model is any LangChain chat model,
picked by a `provider:model` string (`LLM_MODEL`, e.g. `anthropic:claude-haiku-5-5`,
`openai:gpt-5`, `google_genai:gemini-3-flash`) and resolved by `init_chat_model`. The matching
`langchain-<provider>` package and API key are the only per-provider setup. The model must
support tool calling and PDF input as a standard LangChain file block.

Prompt, schemas, validation and the retry loop (`run_extraction`) live in `extract.py`.
This is the only extraction engine; LangChain is imported lazily to keep the module cheap to load.
"""
from __future__ import annotations

import base64
import os
from typing import Any

from .extract import (
    STATEMENT_TOOL,
    SYSTEM_PROMPT,
    TOOL_NAME,
    TOOLS,
    Outcome,
    run_extraction,
)

DEFAULT_LLM = "anthropic:claude-haiku-5-5"

# Provider-neutral tool definitions (name / description / JSON-schema parameters).
NEUTRAL_TOOLS = [
    {"name": t["name"], "description": t["description"], "parameters": t["input_schema"]}
    for t in TOOLS
]


def make_chat_model(spec: str | None = None) -> Any:
    """`spec` is `provider:model`; defaults to `LLM_MODEL`, then DEFAULT_LLM."""
    from langchain.chat_models import init_chat_model

    # Managed functions time out around 45 s: 2 attempts x 18 s fits the 45 s limit of managed functions.
    return init_chat_model(
        spec or os.environ.get("LLM_MODEL", DEFAULT_LLM),
        max_tokens=4000,  # a statement lists every position
        timeout=float(os.environ.get("MODEL_TIMEOUT_S", "18")),
        max_retries=0,
    )


def _build_message(pdf_bytes: bytes, feedback: str | None) -> Any:
    from langchain_core.messages import HumanMessage

    instruction = "Extract the trade from this document."
    if feedback:
        instruction += (
            "\n\nA previous attempt failed these automatic checks:\n"
            f"{feedback}\nRe-read the document carefully and correct the extraction."
        )
    return HumanMessage(
        content=[
            {
                "type": "file",
                "base64": base64.b64encode(pdf_bytes).decode("ascii"),
                "mime_type": "application/pdf",
                # OpenAI-compatible endpoints (e.g. OpenRouter) want a name for file uploads.
                "filename": "document.pdf",
            },
            {"type": "text", "text": instruction},
        ]
    )


def extract(pdf_bytes: bytes, llm: Any, max_attempts: int = 2) -> Outcome:
    """`llm` is a LangChain chat model (see `make_chat_model`)."""
    from langchain_core.messages import SystemMessage

    # Forced tool call; LangChain maps "any" per provider.
    bound = llm.bind_tools(NEUTRAL_TOOLS, tool_choice="any")

    def call(pdf: bytes, feedback: str | None) -> tuple[str, dict[str, Any]]:
        reply = bound.invoke([SystemMessage(SYSTEM_PROMPT), _build_message(pdf, feedback)])
        for tc in reply.tool_calls:
            if tc["name"] in (TOOL_NAME, STATEMENT_TOOL):
                return tc["name"], dict(tc["args"])
        raise ValueError("The model did not call a tool.")

    return run_extraction(pdf_bytes, call, max_attempts)
