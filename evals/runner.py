"""Headless runner for our Pipecat Flows graph.

Reuses `flow.py`'s `NodeConfig` definitions and handler functions, but bypasses
Pipecat entirely. Drives the OpenAI client directly with the current node's
tools and steps the conversation forward turn-by-turn. Used by the eval
harness — never by production.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, Optional

from openai import AsyncOpenAI
from pipecat_flows.types import ContextStrategy

from flow import CLINIC_PERSONA


class HeadlessFlow:
    def __init__(
        self,
        *,
        openai: AsyncOpenAI,
        ehr_client: Any,
        initial_node: dict,
        model: str = "gpt-4o-mini",
    ) -> None:
        self.openai = openai
        self.model = model
        self.flow_manager = SimpleNamespace(state={"ehr": ehr_client})
        self._persona_msg = {"role": "system", "content": CLINIC_PERSONA}
        self.history: list[dict] = [self._persona_msg]
        self.transcript: list[dict] = []  # human-readable [{"role": "agent"|"patient", "text": ...}]
        self.current_node: Optional[dict] = None
        self.terminated = False
        self.prompt_tokens_total = 0
        self.cached_tokens_total = 0
        self.completion_tokens_total = 0
        self._enter_node(initial_node)

    def _enter_node(self, node: dict) -> None:
        strategy_config = node.get("context_strategy")
        if strategy_config and getattr(strategy_config, "strategy", None) == ContextStrategy.RESET:
            self.history = [self._persona_msg]

        self.current_node = node

        if node.get("role_message"):
            self.history.append({"role": "system", "content": node["role_message"]})

        for msg in node.get("task_messages", []) or []:
            role = "system" if msg["role"] == "developer" else msg["role"]
            self.history.append({"role": role, "content": msg["content"]})

        if any(a.get("type") == "end_conversation" for a in node.get("post_actions") or []):
            self._will_terminate = True
        else:
            self._will_terminate = False

    def _tools_payload(self) -> Optional[list[dict]]:
        funcs = self.current_node.get("functions") or []
        if not funcs:
            return None
        return [
            {
                "type": "function",
                "function": {
                    "name": fn.name,
                    "description": fn.description,
                    "parameters": {
                        "type": "object",
                        "properties": fn.properties,
                        "required": fn.required,
                    },
                },
            }
            for fn in funcs
        ]

    def _find_handler(self, name: str):
        for fn in self.current_node.get("functions") or []:
            if fn.name == name:
                return fn.handler
        raise ValueError(
            f"No handler {name!r} on node {self.current_node['name']!r}"
        )

    async def initialize(self) -> str:
        """Let the bot speak first (greeting node opens the conversation)."""
        return await self._run_loop()

    async def send_user(self, text: str) -> str:
        if self.terminated:
            return ""
        self.history.append({"role": "user", "content": text})
        self.transcript.append({"role": "patient", "text": text})
        return await self._run_loop()

    async def _run_loop(self) -> str:
        max_iterations = 8  # protect against tool-call loops
        for _ in range(max_iterations):
            tools = self._tools_payload()
            kwargs: dict[str, Any] = {"model": self.model, "messages": self.history}
            if tools:
                kwargs["tools"] = tools

            kwargs["temperature"] = 0  # determinism for the eval; production runs at default
            resp = await self.openai.chat.completions.create(**kwargs)
            msg = resp.choices[0].message
            usage = getattr(resp, "usage", None)
            if usage is not None:
                self.prompt_tokens_total += usage.prompt_tokens or 0
                self.completion_tokens_total += usage.completion_tokens or 0
                details = getattr(usage, "prompt_tokens_details", None)
                if details is not None:
                    self.cached_tokens_total += getattr(details, "cached_tokens", 0) or 0

            entry: dict[str, Any] = {"role": "assistant"}
            if msg.content:
                entry["content"] = msg.content
            if msg.tool_calls:
                entry["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in msg.tool_calls
                ]
            self.history.append(entry)

            if msg.tool_calls:
                next_node_to_set: Optional[dict] = None
                for tc in msg.tool_calls:
                    name = tc.function.name
                    args = json.loads(tc.function.arguments or "{}")
                    handler = self._find_handler(name)
                    try:
                        result, candidate_next = await handler(args, self.flow_manager)
                    except Exception as exc:  # surface as tool error to the LLM
                        result = {"error": str(exc)}
                        candidate_next = None
                    if candidate_next is not None:
                        next_node_to_set = candidate_next
                    self.history.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": json.dumps(result if result is not None else {"status": "ok"}),
                        }
                    )
                if next_node_to_set is not None:
                    self._enter_node(next_node_to_set)
                continue  # let the LLM speak in response to the tool result

            if msg.content:
                self.transcript.append({"role": "agent", "text": msg.content})
            if self._will_terminate:
                self.terminated = True
            return msg.content or ""

        return ""
