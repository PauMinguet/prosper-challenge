# Implementation plan

Living doc. Update as we go. Phases are sequential; obvious calls are locked, judgment calls are flagged inline at the phase where we'll actually need to decide.

## Locked decisions (no debate)

- **Conversation framework**: `pipecat-ai-flows` v1.0.0 — node-per-step, dynamic transitions, modeled after `examples/patient_intake.py`.
- **EHR stack**: FastAPI + SQLModel + SQLite. Single file `ehr.db` at repo root, gitignored.
- **EHR transport**: HTTP, separate process (`uvicorn`). Bot calls it via `httpx`.
- **Slots**: pre-seeded rows in DB (script generates next 14 days × clinic hours). Booking = creating an Appointment row that references a slot.
- **Patient identity**: exact match on `(lower(name), dob)`. Two hits → fail loud.
- **No auth on EHR.** Localhost only. Note in SOLUTION.md.
- **Idempotency**: unique constraint on `(slot_id)` for active appointments — prevents double-booking. `create_appointment` is upsert-style: returns the existing appointment if the same patient+slot is requested again.
- **`cancel_on_interruption`**: leave default `True`. Idempotency keeps us safe.
- **STT**: keep `ElevenLabsRealtimeSTTService` but pass `language="eng"` to skip auto-detect. Keep `MANUAL` commit (default).
- **VAD `stop_secs`**: bump from 0.2 → 0.6 globally. Simpler than per-node tuning, hurts conversational responsiveness slightly but materially helps DOB/phone capture.
- **DOB normalization**: at the schema level — `description: "convert to YYYY-MM-DD"`. No regex.

## Judgment calls (flagged where they trigger)

- **[Phase 4]** Confirm-back style — read every digit vs. summary-style ("January first, 1983")?
- **[Phase 4]** How closely to mirror `patient_intake.py` structure?
- **[Phase 6]** Eval bonus depth — markdown only, sketch + one runnable test, or a real harness?
- **[Phase 6 / cut?]** Latency investment — any beyond what's free with the chosen stack?

---

## Phase 1 — EHR API

**Goal:** standalone FastAPI service. Tested with curl. No bot involvement.

**New files:**
```
ehr/
  __init__.py
  models.py        # SQLModel: Patient, Slot, Appointment
  db.py            # engine, get_session, init_db
  api.py           # FastAPI app, the 5 endpoints
  seed.py          # generate slots for next 14 days
```

**Endpoints (request/response shapes TBD in implementation):**
- `POST /patients` — create_patient (name, dob, phone, email)
- `GET /patients/find?name=&dob=` — find_patient
- `GET /slots?from=&to=` — list_availability_slots (only unbooked)
- `POST /appointments` — create_appointment (patient_id, slot_id)
- `DELETE /appointments/{id}` — cancel_appointment

**Deps to add to `pyproject.toml`:** `fastapi`, `uvicorn`, `sqlmodel`, `httpx` (httpx for Phase 3 but add now).

**Done when:**
- `uv run uvicorn ehr.api:app --reload` boots cleanly.
- `uv run python -m ehr.seed` populates slots.
- All 5 endpoints respond correctly to manual curl.
- `ehr.db` gitignored.

## Phase 2 — Smoke test EHR

5-minute curl walkthrough of every endpoint. Catch shape bugs before the bot is in the loop.

## Phase 3 — Bot tool handlers

**Goal:** five `FlowsFunctionSchema` handlers that hit the EHR over HTTP and return data the LLM can speak.

**Approach:**
- Single shared `httpx.AsyncClient` constructed at bot startup, stashed in `flow_manager.state["ehr"]`.
- Handlers stay thin — call the EHR, return the JSON, let the LLM phrase the result.
- Each handler returns `(result_dict, next_node)` per the Flows pattern.

**Handlers:**
- `register_patient` — POST /patients, transitions to action-choice node.
- `find_patient` — GET /patients/find. On miss, transition back to "are you new?" node.
- `list_slots` — GET /slots, returns slots, transitions to slot-pick node.
- `create_appointment` — POST /appointments, transitions to confirmation node.
- `cancel_appointment` — DELETE /appointments/{id}, transitions to end node.

## Phase 4 — Flow definition

**Goal:** rewrite `bot.py` to use `FlowManager` with the node graph below.

**Node graph (draft):**
```
greet
  └─→ ask_new_or_existing
        ├─→ register_patient_node ──┐
        └─→ identify_patient_node ──┤
                                    ↓
                             choose_action
                              ├─→ schedule
                              │    └─→ pick_date → pick_slot → confirm_booking → book → end
                              └─→ cancel
                                   └─→ list_appointments → confirm_cancel → cancel → end
```

**Decisions to make IN this phase:**
- **[J] Confirm-back style** — design the `confirm_booking` node. Default: speak it back as a sentence ("So that's Dr. Singh on Tuesday May 12 at 3 PM, correct?"), not digit-by-digit. Flag in SOLUTION as a UX choice with alternatives.
- **[J] Mirror patient_intake closely?** — Default: keep their `role_message` / `task_messages` / `RESET_WITH_SUMMARY` patterns; diverge on prompts and tools. We're not trying to be clever; we're trying to ship something that works.

**Risks:**
- Branching after `ask_new_or_existing` — Flows handles this naturally because each handler returns the next NodeConfig.
- LLM picking the wrong tool at branch points — mitigated by per-node tool gating (this is exactly what Flows is for).

**Done when:** the flow runs end-to-end in WebRTC, both branches.

## Phase 5 — Manual end-to-end

- Two terminals: `uv run uvicorn ehr.api:app` and `uv run bot.py`.
- Browser at `localhost:7860`, `Connect`.
- Walk both branches: register-then-book, identify-then-cancel.
- Iterate on prompts wherever the LLM stalls or picks wrong.
- Capture failure modes — they go into SOLUTION.md as known limitations or eval test cases.

## Phase 6 — SOLUTION.md

**Structure:**
1. **Overview** — one diagram, the node graph + EHR API surface.
2. **Stack & decisions** — why Flows, why FastAPI/SQLite/SQLModel, why pre-seeded slots, why we kept STT defaults beyond the language hint, etc.
3. **Trade-offs explicitly punted** — no auth, exact-match identity only, single-process EHR, simple slot model.
4. **Latency** — what we did (language hint, default Pipecat optimizations), what we didn't (multi-STT racing, smaller TTS model for short confirmations, prompt caching), why.
5. **Reliability** — what happens on EHR down, OpenAI down, ElevenLabs down. Aspirational unless we build in fallback.
6. **Evaluation** — see Phase 6+ below.

**Capture decisions as we go.** Don't write this last.

## Phase 6+ — Evaluation bonus *(judgment call)*

Three options, pick when we get here:

- **A) Markdown-only sketch.** Describe an eval harness — simulated callers (script-driven LLM playing the patient), a judge LLM scoring transcripts, regression testing. ~30 min of writing. Lowest signal.
- **B) Sketch + one runnable test.** Same markdown, plus a `evals/` script that scripts a fake-patient LLM through the bot and asserts the EHR ended up in the expected state. ~2-3 hours. **Default pick — good interview material, bounded effort.**
- **C) Real harness.** Multiple test scripts, judge LLM, transcript diffing, CI runnable. Days. Skip unless something goes wrong with the rest and we have surplus time.

## Phase 7 — Polish

- Update README with run instructions (two-process startup).
- Verify `.env.example` and `env.example` (fix duplicates).
- Update Dockerfile if we want it functional (low priority — challenge doesn't require it works end-to-end in Docker).
- Final pass on `SOLUTION.md`.

---

## Cut list (don't do unless explicitly justified)

- Auth on EHR.
- Postgres / Docker compose.
- Multi-provider STT/LLM fallback (mention in SOLUTION reliability section instead).
- Real calendar integrations.
- Voicemail / SMS.
- A frontend admin UI for the EHR.
