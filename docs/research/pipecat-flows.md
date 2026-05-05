# Pipecat Flows — research notes

**Package:** `pipecat-ai-flows` v1.0.0 (released 2026-04-15)
**Imports from:** `pipecat_flows`
**Compatible with Pipecat:** uses the new universal `LLMContext` / `LLMContextAggregatorPair` (same primitives we already have in `bot.py`)
**Sources verified:**
- README: https://github.com/pipecat-ai/pipecat-flows
- Patient-intake example (verbatim, see below): https://github.com/pipecat-ai/pipecat-flows/blob/main/examples/patient_intake.py
- Docs landing: https://docs.pipecat.ai/guides/features/pipecat-flows

> One-line summary: a node-based conversation framework that sits **alongside** the LLM service. Each node has its own `task_messages`, allowed `functions`, and optional context strategy. Function handlers return `(result, next_node)` to drive transitions. This is essentially a state machine where transitions are decided by the LLM picking which tool to call.

## 1. What it is, mechanically

- A **graph of nodes**. Each node is a `NodeConfig` describing the system/task prompt and the limited set of tools the LLM is allowed to call **at this step**.
- The LLM picks a tool ⇒ Flows runs your handler ⇒ handler returns `(result, next_node)` ⇒ Flows swaps the active node, replaces the tools, and continues.
- Solves "monolithic prompts with many tools lead to hallucinations." Each node sees only the tools it should be picking from.
- Two flow styles:
  - **Static**: declare the whole graph up-front (transitions are config).
  - **Dynamic**: handlers compute the next node at runtime by returning a freshly built `NodeConfig`. The patient_intake example is dynamic.
- Has a separate **visual editor** (`pipecat-flows-editor` repo) for designing flows. Not needed for our take-home.

## 2. Imports and core API

```python
from pipecat_flows import (
    ContextStrategy,
    ContextStrategyConfig,
    FlowArgs,
    FlowManager,
    FlowsFunctionSchema,
    NodeConfig,
)
```

**Handler signature** (dynamic flow):

```python
async def handler(args: FlowArgs, flow_manager: FlowManager) -> tuple[Result, NodeConfig]:
    # args is the LLM's parsed tool args (TypedDict-friendly)
    # flow_manager.state is a free-form dict you can stash data in across nodes
    flow_manager.state["birthday"] = args["birthday"]
    return SomeResult(...), create_next_node()
```

The handler returns a 2-tuple:
- **First element**: the data sent back to the LLM as the tool result (what it sees in the next turn).
- **Second element**: the `NodeConfig` to transition to. Returning `None` for the next node keeps you on the current node.

**Defining a node:**

```python
def create_initial_node() -> NodeConfig:
    verify_birthday_func = FlowsFunctionSchema(
        name="verify_birthday",
        handler=verify_birthday,
        description="Verify the user's birthday. Once confirmed, proceed to prescriptions.",
        properties={
            "birthday": {
                "type": "string",
                "description": "The user's birthdate (convert to YYYY-MM-DD format)",
            }
        },
        required=["birthday"],
    )

    return NodeConfig(
        name="start",
        role_message="You are Jessica, an agent for Tri-County Health Services...",
        task_messages=[
            {
                "role": "developer",
                "content": "Start by introducing yourself, then ask for their date of birth...",
            }
        ],
        functions=[verify_birthday_func],
    )
```

`role_message` is the persistent system prompt; `task_messages` are the per-node instructions injected when the node activates.

## 3. Wiring into the pipeline

Flows **does not replace** the LLM service. It plugs in alongside, holding a reference to the pipeline task, the LLM service, the context aggregator, and the transport. Pipeline structure stays the same as a normal Pipecat bot:

```python
context = LLMContext()  # no tools here — Flows manages tools per-node
context_aggregator = LLMContextAggregatorPair(
    context,
    user_params=LLMUserAggregatorParams(
        vad_analyzer=SileroVADAnalyzer(),
        filter_incomplete_user_turns=True,
    ),
)

pipeline = Pipeline([
    transport.input(),
    stt,
    context_aggregator.user(),
    llm,
    tts,
    transport.output(),
    context_aggregator.assistant(),
])

task = PipelineTask(pipeline, params=PipelineParams(...))

flow_manager = FlowManager(
    task=task,
    llm=llm,
    context_aggregator=context_aggregator,
    transport=transport,
)

@transport.event_handler("on_client_connected")
async def on_client_connected(transport, client):
    await flow_manager.initialize(create_initial_node())
```

Notable diffs from our current `bot.py`:
- The `LLMContext` is empty when constructed — Flows owns the tools.
- The `LLMContextAggregatorPair` in the example uses `vad_analyzer` and `filter_incomplete_user_turns=True` inside `LLMUserAggregatorParams`, instead of our `user_turn_strategies=[TurnAnalyzerUserTurnStopStrategy(...)]`. We can keep our Smart-Turn config — both patterns are accepted.
- `flow_manager.initialize(initial_node)` replaces our `messages.append({...}); task.queue_frames([LLMRunFrame()])`.

## 4. Context strategy per node

Useful for the "verify all collected info" step in our flow — the LLM doesn't need the full back-and-forth, just a summary:

```python
NodeConfig(
    name="verify",
    task_messages=[...],
    context_strategy=ContextStrategyConfig(
        strategy=ContextStrategy.RESET_WITH_SUMMARY,
        summary_prompt=(
            "Summarize the patient intake conversation, including their birthday, "
            "prescriptions, allergies, conditions, and reasons for visiting."
        ),
    ),
    functions=[revise_information_func, confirm_information_func],
)
```

Strategies seen in the patient_intake example:
- `ContextStrategy.RESET` — wipe history, keep node messages.
- `ContextStrategy.RESET_WITH_SUMMARY` — replace history with an LLM-generated summary first.
- (Default if omitted: append-and-keep, i.e. preserve full history.)

## 5. Terminal nodes

```python
NodeConfig(
    name="end",
    task_messages=[{"role": "developer", "content": "Thank them and end."}],
    post_actions=[{"type": "end_conversation"}],
)
```

`post_actions` fires after the LLM speaks the node's content. `end_conversation` triggers Pipecat's graceful disconnect.

## 6. Static vs dynamic — when to pick which

- **Static**: full graph wired up at start. Better when the flow is fully known and you want declarative auditability.
- **Dynamic**: handlers return the next node. Better when transitions depend on runtime state (e.g., "if patient is new, go to register; if existing, go to identify").

Our use case is dynamic — the new/existing patient branch and the schedule/cancel branch are runtime decisions based on what the caller says and what the EHR returns.

## 7. The patient_intake example — directly applicable to us

The `examples/patient_intake.py` in this repo is essentially a template for a clinic intake bot:

- 7 nodes: `start` (verify birthday) → `get_prescriptions` → `get_allergies` → `get_conditions` → `get_visit_reasons` → `verify` (with summary reset) → `confirm` → `end`.
- All transitions dynamic (handlers return the next NodeConfig).
- Uses `flow_manager.state["..."] = ...` to carry data across nodes.
- Uses our exact `LLMContext` / `LLMContextAggregatorPair` setup.

Differences from what we need:
- Their flow doesn't branch (linear). We need a branch at "new vs existing" and at "schedule vs cancel."
- Their handlers don't make HTTP calls — ours need to call the EHR. The handlers receive `flow_manager` but **not** `tool_resources` (that's the raw-function-calling pattern). For the EHR client, we'd either (a) close over a module-level client, (b) attach it to `flow_manager.state` at startup, or (c) inject via a closure when constructing nodes. Option (b) seems cleanest — set `flow_manager.state["ehr"] = ehr_client` once, read it in every handler.

## 8. Maturity

- v1.0.0 in April 2026. Production-ready by their own labelling.
- 582 stars, 22 releases — actively maintained.
- Full CHANGELOG exists in repo (we haven't read it; if we hit version-specific issues, check there first).

## 9. When NOT to use Flows

Flows is overkill if:
- The flow is **truly linear** with no branching (a single tight system prompt + tool calls is shorter).
- You only need 1–2 tool calls total and the LLM doesn't need help picking them.

For our challenge:
- We have **at least two real branches** (new/existing, schedule/cancel) plus per-step constraints (don't ask for DOB twice, don't try to schedule before identifying the patient).
- We have ~5 tool calls total, and the LLM should not be considering "create_appointment" while it's still collecting patient identity. Per-node tool gating directly addresses this.

## Recommendation

**Use Flows.** The patient_intake example is so close to our use case that we'd be doing more work to *not* use it. Per-node tool gating is the single best hedge against the "the LLM tried to book before identifying the patient" failure mode, and `RESET_WITH_SUMMARY` cleanly handles the "read back the appointment details before booking" step. The dependency cost (one extra package, all imports namespaced under `pipecat_flows`) is trivial.

The one concrete trade-off: we sacrifice some "vibe-codeable" simplicity for structure. For an interview deliverable where they explicitly want to discuss decisions and trade-offs, picking Flows gives us material for that conversation — we can articulate why per-node tool gating beats a giant system prompt.
