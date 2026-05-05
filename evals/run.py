"""Driver for the eval suite.

Runs each scenario against a headless re-implementation of the bot, asserts
the EHR ended in the expected state, and asks an LLM judge to score the
transcript against per-scenario criteria.

Usage::

    uv run python -m evals.run
    uv run python -m evals.run --only register_and_book,prompt_injection
    uv run python -m evals.run --tag adversarial
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import traceback
from dataclasses import asdict, dataclass, field
from pathlib import Path

from dotenv import load_dotenv
from httpx import ASGITransport, AsyncClient
from openai import AsyncOpenAI
from sqlmodel import Session, select

from ehr.api import app
from ehr.db import engine
from ehr.models import Appointment, Patient
from ehr_client import EHRClient
from flow import create_greet_node

from .runner import HeadlessFlow
from .scenarios import SCENARIOS, Scenario, StateExpectation
from .sim import judge, patient_turn

load_dotenv(override=True)

BOT_MODEL = os.environ.get("EVAL_BOT_MODEL", "gpt-4o")
SIM_MODEL = os.environ.get("EVAL_SIM_MODEL", "gpt-4o-mini")
JUDGE_MODEL = os.environ.get("EVAL_JUDGE_MODEL", "gpt-4o-mini")


@dataclass
class ScenarioResult:
    name: str
    state_passed: bool
    state_reason: str
    judge_results: list[dict]
    transcript: list[dict]
    duration_s: float
    prompt_tokens: int = 0
    cached_tokens: int = 0
    completion_tokens: int = 0
    error: str | None = None

    @property
    def overall_passed(self) -> bool:
        if self.error:
            return False
        if not self.state_passed:
            return False
        return all(r.get("passed") for r in self.judge_results)

    @property
    def cache_hit_rate(self) -> float:
        return (self.cached_tokens / self.prompt_tokens) if self.prompt_tokens else 0.0


def assert_state(expected: StateExpectation) -> tuple[bool, str]:
    with Session(engine) as s:
        patients = list(s.exec(select(Patient)).all())
        appts = list(s.exec(select(Appointment)).all())

    active = [a for a in appts if a.cancelled_at is None]
    cancelled = [a for a in appts if a.cancelled_at is not None]

    failures: list[str] = []
    if expected.patient_count is not None and len(patients) != expected.patient_count:
        failures.append(f"patients: expected {expected.patient_count}, got {len(patients)}")
    if expected.patient_name_contains is not None and not any(
        expected.patient_name_contains.lower() in p.name.lower() for p in patients
    ):
        names = [p.name for p in patients]
        failures.append(f"no patient name contains {expected.patient_name_contains!r} (got {names})")
    if expected.active_appointment_count is not None and len(active) != expected.active_appointment_count:
        failures.append(
            f"active appointments: expected {expected.active_appointment_count}, got {len(active)}"
        )
    if expected.cancelled_appointment_count is not None and len(cancelled) != expected.cancelled_appointment_count:
        failures.append(
            f"cancelled appointments: expected {expected.cancelled_appointment_count}, got {len(cancelled)}"
        )

    if failures:
        return False, "; ".join(failures)
    return True, "all expectations met"


async def run_scenario(scenario: Scenario, openai: AsyncOpenAI, ehr: EHRClient) -> ScenarioResult:
    start = time.monotonic()
    scenario.setup()

    flow = HeadlessFlow(openai=openai, ehr_client=ehr, initial_node=create_greet_node(), model=BOT_MODEL)

    try:
        await flow.initialize()

        for _ in range(scenario.max_turns):
            if flow.terminated:
                break
            patient_msg = await patient_turn(openai, scenario.persona, flow.transcript, model=SIM_MODEL)
            if "[HANGUP]" in patient_msg.upper():
                break
            await flow.send_user(patient_msg)
    except Exception:
        return ScenarioResult(
            name=scenario.name,
            state_passed=False,
            state_reason="exception during run",
            judge_results=[],
            transcript=flow.transcript,
            duration_s=time.monotonic() - start,
            prompt_tokens=flow.prompt_tokens_total,
            cached_tokens=flow.cached_tokens_total,
            completion_tokens=flow.completion_tokens_total,
            error=traceback.format_exc(),
        )

    state_ok, state_reason = assert_state(scenario.expected_state)
    judge_results = await judge(openai, flow.transcript, scenario.judge_criteria, model=JUDGE_MODEL)

    return ScenarioResult(
        name=scenario.name,
        state_passed=state_ok,
        state_reason=state_reason,
        judge_results=judge_results,
        transcript=flow.transcript,
        duration_s=time.monotonic() - start,
        prompt_tokens=flow.prompt_tokens_total,
        cached_tokens=flow.cached_tokens_total,
        completion_tokens=flow.completion_tokens_total,
    )


def select_scenarios(args: argparse.Namespace) -> list[Scenario]:
    selected = SCENARIOS
    if args.only:
        wanted = {n.strip() for n in args.only.split(",")}
        selected = [s for s in selected if s.name in wanted]
    if args.tag:
        selected = [s for s in selected if args.tag in s.tags]
    return selected


def print_results(results: list[ScenarioResult], verbose: bool) -> None:
    print()
    print("=" * 92)
    print(
        f"{'SCENARIO':<40} {'STATE':<6} {'JUDGE':<7} {'TIME':<7} {'PROMPT':<8} {'CACHED':<10} {'OVERALL':<8}"
    )
    print("-" * 92)
    for r in results:
        judge_pass = sum(1 for j in r.judge_results if j.get("passed"))
        judge_str = f"{judge_pass}/{len(r.judge_results)}"
        state_str = "PASS" if r.state_passed else "FAIL"
        overall_str = "PASS" if r.overall_passed else "FAIL"
        cache_pct = f"{int(r.cache_hit_rate * 100)}%" if r.prompt_tokens else "-"
        cached_str = f"{r.cached_tokens} ({cache_pct})"
        print(
            f"{r.name:<40} {state_str:<6} {judge_str:<7} {r.duration_s:<6.1f}s "
            f"{r.prompt_tokens:<8} {cached_str:<10} {overall_str:<8}"
        )
    print("=" * 92)

    total_prompt = sum(r.prompt_tokens for r in results)
    total_cached = sum(r.cached_tokens for r in results)
    overall_pct = (total_cached / total_prompt * 100) if total_prompt else 0
    print(
        f"Token totals: prompt={total_prompt:,}  cached={total_cached:,}  "
        f"hit-rate={overall_pct:.1f}%  completion={sum(r.completion_tokens for r in results):,}"
    )

    failures = [r for r in results if not r.overall_passed]
    if failures or verbose:
        print()
        for r in failures if not verbose else results:
            print(f"\n--- {r.name} ---")
            if r.error:
                print(f"ERROR:\n{r.error}")
            if not r.state_passed:
                print(f"State: {r.state_reason}")
            for j in r.judge_results:
                marker = "✓" if j.get("passed") else "✗"
                print(f"  {marker} {j.get('criterion','')}: {j.get('reason','')}")
            if verbose:
                print("\nTranscript:")
                for turn in r.transcript:
                    print(f"  [{turn['role']}] {turn['text']}")
    print()
    passed = sum(1 for r in results if r.overall_passed)
    print(f"{passed}/{len(results)} scenarios passed.")


def write_json(path: Path, results: list[ScenarioResult]) -> None:
    payload = {
        "results": [
            {
                "name": r.name,
                "overall_passed": r.overall_passed,
                "state_passed": r.state_passed,
                "state_reason": r.state_reason,
                "judge_results": r.judge_results,
                "duration_s": round(r.duration_s, 2),
                "prompt_tokens": r.prompt_tokens,
                "cached_tokens": r.cached_tokens,
                "cache_hit_rate": round(r.cache_hit_rate, 3),
                "completion_tokens": r.completion_tokens,
                "transcript": r.transcript,
                "error": r.error,
            }
            for r in results
        ],
        "summary": {
            "total": len(results),
            "passed": sum(1 for r in results if r.overall_passed),
            "prompt_tokens": sum(r.prompt_tokens for r in results),
            "cached_tokens": sum(r.cached_tokens for r in results),
            "completion_tokens": sum(r.completion_tokens for r in results),
        },
    }
    path.write_text(json.dumps(payload, indent=2, default=str))
    print(f"Wrote JSON results to {path}")


def diff_against_baseline(baseline_path: Path, results: list[ScenarioResult]) -> int:
    baseline = json.loads(baseline_path.read_text())
    by_name = {r["name"]: r for r in baseline["results"]}
    regressions: list[str] = []
    new_passes: list[str] = []
    for r in results:
        prev = by_name.get(r.name)
        if prev is None:
            continue
        if prev["overall_passed"] and not r.overall_passed:
            regressions.append(r.name)
        elif not prev["overall_passed"] and r.overall_passed:
            new_passes.append(r.name)

    print()
    print(f"Baseline diff vs {baseline_path}:")
    if regressions:
        print(f"  REGRESSIONS ({len(regressions)}): {', '.join(regressions)}")
    if new_passes:
        print(f"  recovered ({len(new_passes)}): {', '.join(new_passes)}")
    if not regressions and not new_passes:
        print("  no change in pass/fail status")
    return len(regressions)


async def main_async(args: argparse.Namespace) -> int:
    if not os.environ.get("OPENAI_API_KEY"):
        print("ERROR: OPENAI_API_KEY not set in environment / .env", file=sys.stderr)
        return 2

    selected = select_scenarios(args)
    if not selected:
        print("No scenarios selected.")
        return 1

    print(f"Running {len(selected)} scenario(s) with bot={BOT_MODEL}, sim={SIM_MODEL}, judge={JUDGE_MODEL}")

    openai_client = AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"])
    transport = ASGITransport(app=app)

    results: list[ScenarioResult] = []
    async with AsyncClient(transport=transport, base_url="http://eval") as http:
        ehr = EHRClient(client=http)
        for scenario in selected:
            print(f"  → {scenario.name} ...", end="", flush=True)
            result = await run_scenario(scenario, openai_client, ehr)
            print(f"  ({result.duration_s:.1f}s, {'PASS' if result.overall_passed else 'FAIL'})")
            results.append(result)

    print_results(results, verbose=args.verbose)

    if args.json:
        write_json(Path(args.json), results)

    regressions = 0
    if args.baseline:
        regressions = diff_against_baseline(Path(args.baseline), results)

    if regressions:
        return 2
    return 0 if all(r.overall_passed for r in results) else 1


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", help="comma-separated scenario names to run")
    parser.add_argument("--tag", help="run only scenarios with this tag")
    parser.add_argument("-v", "--verbose", action="store_true", help="print full transcripts")
    parser.add_argument("--json", help="write results to a JSON file at this path")
    parser.add_argument(
        "--baseline",
        help="JSON results from a previous run; diff and exit 2 on regression",
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
