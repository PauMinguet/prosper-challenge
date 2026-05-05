"""Patient simulator + judge LLM."""

from __future__ import annotations

import json

from openai import AsyncOpenAI

PATIENT_SYSTEM = """\
You are role-playing a patient calling a clinic. Stay in character.

Speak naturally like a real phone call: short sentences, sometimes hesitate, sometimes ask
clarifying questions. Don't mention being an AI or break the simulation in any way.

When the agent has clearly finished (greeted you goodbye, told you the booking or
cancellation is done, told you they cannot help), respond with the literal string
[HANGUP] alone. Don't say [HANGUP] before the agent has clearly wrapped up.

Your persona and goal:
{persona}
"""

JUDGE_SYSTEM = """\
You are evaluating a transcript from a clinic voice agent ("Robin from Prosper Health").

Given the transcript and a list of criteria, decide whether each criterion is satisfied.

Respond ONLY with a JSON object of this shape:

{"results": [{"criterion": "...", "passed": true, "reason": "one short sentence"}]}

Be strict — only pass if the transcript clearly meets the criterion. If the agent did
something subtly wrong, that's a fail.
"""


async def patient_turn(
    openai: AsyncOpenAI,
    persona: str,
    transcript: list[dict],
    *,
    model: str = "gpt-4o-mini",
) -> str:
    messages: list[dict] = [
        {"role": "system", "content": PATIENT_SYSTEM.format(persona=persona)}
    ]
    for turn in transcript:
        # From the patient sim's perspective, the agent's lines are "user" and the patient's are "assistant".
        if turn["role"] == "agent":
            messages.append({"role": "user", "content": turn["text"]})
        else:
            messages.append({"role": "assistant", "content": turn["text"]})

    resp = await openai.chat.completions.create(
        model=model,
        messages=messages,
        max_tokens=200,
        temperature=0.7,
    )
    return (resp.choices[0].message.content or "").strip()


async def judge(
    openai: AsyncOpenAI,
    transcript: list[dict],
    criteria: list[str],
    *,
    model: str = "gpt-4o-mini",
) -> list[dict]:
    transcript_text = "\n".join(f"{t['role'].upper()}: {t['text']}" for t in transcript)
    prompt = (
        "TRANSCRIPT:\n"
        f"{transcript_text}\n\n"
        "CRITERIA:\n" + "\n".join(f"- {c}" for c in criteria)
    )
    resp = await openai.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": JUDGE_SYSTEM},
            {"role": "user", "content": prompt},
        ],
        response_format={"type": "json_object"},
        max_tokens=600,
        temperature=0,
    )
    raw = resp.choices[0].message.content or "{}"
    parsed = json.loads(raw)
    if isinstance(parsed, list):
        return parsed
    return parsed.get("results", [])
