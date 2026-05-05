# Pipecat function/tool calling — research notes

**Pipecat version pinned:** `pipecat-ai==0.0.100` (per `uv.lock`, Jan 2026)
**Sources verified:**
- https://docs.pipecat.ai/pipecat/learn/function-calling
- https://docs.pipecat.ai/pipecat/learn/context-management

> Even if we use Pipecat Flows (recommended — see `pipecat-flows.md`), the same primitives (`FunctionSchema`, `FunctionCallParams`) apply. This doc covers the **raw** API for cases where Flows is overkill (one-off tools, or as fallback if Flows misbehaves).

## 1. Two ways to declare a function

### A. `FunctionSchema` — explicit, provider-agnostic (recommended)

```python
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema

weather_function = FunctionSchema(
    name="get_current_weather",
    description="Get the current weather in a location",
    properties={
        "location": {
            "type": "string",
            "description": "The city and state, e.g. San Francisco, CA",
        },
        "format": {
            "type": "string",
            "enum": ["celsius", "fahrenheit"],
            "description": "The temperature unit to use.",
        },
    },
    required=["location", "format"],
)

tools = ToolsSchema(standard_tools=[weather_function])
context = LLMContext(tools=tools)
```

`properties` is JSON Schema-shaped (same shape OpenAI uses for tool params).

### B. Direct functions — auto-schema from signature + docstring

```python
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.services.llm_service import FunctionCallParams

async def get_current_weather(params: FunctionCallParams, location: str, format: str):
    """Get the current weather.

    Args:
        location: The city and state, e.g. "San Francisco, CA".
        format: The temperature unit to use. Must be either "celsius" or "fahrenheit".
    """
    weather_data = {"conditions": "sunny", "temperature": "75"}
    await params.result_callback(weather_data)

tools = ToolsSchema(standard_tools=[get_current_weather])
```

Pipecat parses the type hints + docstring to build the schema. Less ceremony, but coupled to docstring discipline.

## 2. Handler signature

```python
async def handler(params: FunctionCallParams):
    # do stuff, then:
    await params.result_callback(result_data)
```

Handlers are **async**. The result is delivered via `params.result_callback(...)`, **not** a return value. This lets you stream partial results or skip the LLM follow-up turn (see §4).

### `FunctionCallParams` shape

```python
@dataclass
class FunctionCallParams:
    function_name: str
    tool_call_id: str
    arguments: Mapping[str, Any]
    llm: LLMService
    context: LLMContext
    result_callback: FunctionCallResultCallback
    tool_resources: Any
```

- `arguments` — the LLM's parsed tool args.
- `context` — the live `LLMContext`, mutable. Useful if you want to inject extra context before the LLM continues.
- `tool_resources` — application-level deps you set on `PipelineTask` (see §5). This is how you give handlers DB connections / HTTP clients without globals.

## 3. Registering handlers on the LLM service

```python
llm = OpenAILLMService(api_key="...")

async def fetch_weather_from_api(params: FunctionCallParams):
    weather_data = {"conditions": "sunny", "temperature": "75"}
    await params.result_callback(weather_data)

llm.register_function(
    "get_current_weather",
    fetch_weather_from_api,
    cancel_on_interruption=True,
    timeout_secs=30.0,
)
```

Options:
- `cancel_on_interruption=True` (default) — if the user starts speaking again, cancel the in-flight handler.
- `cancel_on_interruption=False` — handler keeps running in the background.
- `timeout_secs` — per-function timeout override.

## 4. `FunctionCallResultProperties` — controlling LLM follow-up

```python
from pipecat.frames.frames import FunctionCallResultProperties

properties = FunctionCallResultProperties(
    run_llm=False,                # Don't let the LLM speak after this tool result
    on_context_updated=on_done,   # Callback when context has been updated
)

await params.result_callback(weather_data, properties=properties)
```

Useful when:
- The handler wants to be silent (e.g., logging-only side effect).
- You want to force a specific transition handled outside the LLM (rare for our use case).

## 5. Sharing resources via `tool_resources`

```python
@dataclass
class AppResources:
    ehr_client: httpx.AsyncClient
    db: SomeDB

resources = AppResources(ehr_client=httpx.AsyncClient(...), db=...)

task = PipelineTask(pipeline, tool_resources=resources)

async def find_patient(params: FunctionCallParams, name: str, dob: str):
    """Look up a patient by name and date of birth."""
    ehr = params.tool_resources.ehr_client
    resp = await ehr.get("/find_patient", params={"name": name, "dob": dob})
    await params.result_callback(resp.json())
```

This is the clean way to get an HTTP client into our handlers. Avoids module-level globals and gives us a single test seam.

(If we use Flows, the equivalent is `flow_manager.state["ehr"] = client` — Flows handlers get the `flow_manager` reference, not `params.tool_resources`.)

## 6. How the result flows back

- The context aggregators (in our pipeline as `user_aggregator` / `assistant_aggregator`) automatically append the function call AND the function result to the conversation history.
- After `result_callback`, the LLM service triggers a follow-up turn so the bot can speak the answer (unless `run_llm=False` was set).
- TL;DR: just call `result_callback` with the data — the rest is automatic.

## 7. Pipeline structure (no changes from current `bot.py` needed)

```python
context = LLMContext(tools=tools)  # tools registered here

user_aggregator, assistant_aggregator = LLMContextAggregatorPair(context, ...)

pipeline = Pipeline([
    transport.input(),
    rtvi,
    stt,
    user_aggregator,
    llm,             # llm.register_function(...) called before this point
    tts,
    transport.output(),
    assistant_aggregator,
])
```

Order matters: `user_aggregator` must come BEFORE `llm`, and `assistant_aggregator` AFTER `transport.output()` so it captures spoken responses.

## 8. Gotchas (from docs)

- **Two LLMContext flavours**: the new universal `LLMContext` (used in `bot.py` and the Flows example) and the older `OpenAILLMContext`. Tool registration on the universal one is what's documented above. If you see examples using `OpenAILLMContext`, they're older — don't mix.
- **Cancellation semantics**: `cancel_on_interruption=True` (default) means a long-running EHR call will be cancelled if the user speaks. For our `create_appointment` and similar mutation calls, we may want `cancel_on_interruption=False` so a slot we tried to book doesn't end up in a "did it work?" state. **Decision needed when implementing.**
- **Result must be JSON-serializable.** Pydantic models and dicts work; raw `httpx.Response` does not — call `.json()` first.
- **No native parallel tool calls** in the docs above — handlers run one at a time per turn. (OpenAI's API supports parallel calls, but Pipecat's behavior wasn't explicit in the docs we read; verify if we hit a use case.)

## 9. Flows vs raw — short pointer

- **Raw**: one `LLMContext` with a flat list of tools, the LLM picks freely. Best for small, linear bots.
- **Flows**: per-node tool gating; only the tools relevant to the current step are exposed. Best when the LLM should be *forced* to follow a specific step order. See `pipecat-flows.md`.

For this challenge we're going with Flows (see recommendation in that doc), but the underlying `FunctionCallParams` and `result_callback` semantics are the same — Flows just owns when which `FunctionSchema` is active.
