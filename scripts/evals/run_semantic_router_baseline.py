"""Run the small pre-Phase-1 natural-language routing baseline."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from time import perf_counter

import yaml
from vanna.core.user import User

from data_analysis_agent.metric_context import OLIST_RUNTIME_WORKSPACE
from data_analysis_agent.question_router import QuestionRouter
from data_analysis_agent.question_router import expand_retrieval_question
from data_analysis_agent.semantic_catalog import CatalogLoader, CatalogRetriever
from data_analysis_agent.semantic_intent import (
    SemanticIntentError,
    SemanticIntentInterpreter,
    route_semantic_intent,
    select_intent_catalog,
)


def run(cases_path: Path) -> dict[str, object]:
    document = yaml.safe_load(cases_path.read_text(encoding="utf-8"))
    router = QuestionRouter(
        CatalogRetriever(CatalogLoader(OLIST_RUNTIME_WORKSPACE).load()),
        max_result_rows=100_000,
        full_results=True,
    )
    user = User(id="semantic-router-baseline", group_memberships=["analyst"])
    rows: list[dict[str, object]] = []
    for case in document["cases"]:
        question = case["question"]
        state = case.get("state", {})
        selection = router.retriever.retrieve(question, user)
        retrieval = expand_retrieval_question(
            question, state, has_metric_match=bool(selection.metrics)
        )
        selection = router.retriever.retrieve(retrieval, user)
        route = router.classify(
            question, user=user, selection=selection, conversation_state=state
        )
        admitted = (
            router.admit_sql_request(
                question, user=user, selection=selection, conversation_state=state
            )
            if route.should_generate_sql
            else None
        )
        rows.append(
            {
                "id": case["id"],
                "question": question,
                "expected_intent": case["expected_intent"],
                "state": route.state,
                "intent": route.intent,
                "reason_code": route.reason_code,
                "metric_ids": list(route.metric_ids),
                "requires_database": route.requires_database,
                "clarification": route.clarification,
                "admit_state": admitted.state if admitted else None,
                "admit_reason_code": admitted.reason_code if admitted else None,
                "final_state": (admitted or route).state,
            }
        )
    return {
        "baseline_id": document["baseline_id"],
        "catalog_version": document["catalog_version"],
        "case_count": len(rows),
        "database_admitted": sum(row["final_state"] == "answerable" for row in rows),
        "rows": rows,
    }


async def run_live(cases_path: Path) -> dict[str, object]:
    from dotenv import load_dotenv
    from vanna.core.llm import LlmMessage, LlmRequest
    from vanna.integrations.openai import OpenAILlmService
    from data_analysis_agent.query_plan import QueryPlan
    from data_analysis_agent.result_budget import ResultBudget
    from data_analysis_agent.semantic_catalog import ResultContract
    from data_analysis_agent.candidate_sql_generator import (
        build_runtime_candidate_request,
    )

    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    llm = OpenAILlmService(
        model=os.getenv("SILICONFLOW_MODEL", "deepseek-ai/DeepSeek-V4-Flash"),
        api_key=os.environ["SILICONFLOW_API_KEY"],
        base_url=os.getenv("SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1"),
        max_retries=0,
    )
    catalog = CatalogLoader(OLIST_RUNTIME_WORKSPACE).load()
    router = QuestionRouter(
        CatalogRetriever(catalog), max_result_rows=100_000, full_results=True
    )
    user = User(id="semantic-baseline", group_memberships=["analyst"])

    async def complete(prompt: str, resolved_user: User) -> str:
        response = await llm.send_request(
            LlmRequest(
                messages=[LlmMessage(role="user", content=prompt)],
                user=resolved_user,
                temperature=0,
                tools=None,
                max_tokens=800,
                system_prompt="只输出JSON，不生成SQL或工具调用。",
            )
        )
        return response.content or ""

    interpreter = SemanticIntentInterpreter(catalog, complete)
    cases = yaml.safe_load(cases_path.read_text(encoding="utf-8"))["cases"]
    semaphore = asyncio.Semaphore(3)

    async def evaluate(case: dict) -> dict:
        async with semaphore:
            started = perf_counter()
            row = {
                "id": case["id"],
                "question": case["question"],
                "expected_intent": case["expected_intent"],
            }
            try:
                intent = await interpreter.interpret(
                    case["question"], user, case.get("state")
                )
                selection = select_intent_catalog(
                    intent, router.retriever, user, question=case["question"]
                )
                route = route_semantic_intent(
                    intent, router, selection, case.get("state")
                )
                row.update({"intent": intent.as_dict(), "route": route.as_dict()})
                if route.should_generate_sql:
                    state = {
                        "semantic_intent": intent.as_dict(),
                        "time_range": intent.time_range.model_dump()
                        if intent.time_range
                        else None,
                        "comparison_baseline": intent.comparison_baseline,
                    }
                    plan = QueryPlan.from_selection(
                        selection,
                        case["question"],
                        route,
                        state,
                        result_budget=ResultBudget.aggregate(
                            execution_max_rows=100_000
                        ),
                    )
                    contract = ResultContract.from_selection(
                        selection,
                        case["question"],
                        plan.time_range,
                        catalog=catalog,
                        required_result_columns=plan.required_result_columns,
                        requested_dimensions=plan.dimensions,
                        result_budget=plan.result_budget,
                    )
                    build_runtime_candidate_request(
                        question=case["question"],
                        selection=selection,
                        query_plan=plan,
                        result_contract=contract,
                        workspace=OLIST_RUNTIME_WORKSPACE,
                        route=route,
                    )
                    row["plan"] = plan.as_dict()
                row["status"] = "parsed"
            except SemanticIntentError as exc:
                row.update({"status": "interpretation_failed", "reason": str(exc)})
            except ValueError as exc:
                row.update({"status": "plan_failed", "reason": str(exc)})
            row["elapsed_ms"] = round((perf_counter() - started) * 1000)
            print(f"{row['id']}: {row['status']}", flush=True)
            return row

    rows = await asyncio.gather(*(evaluate(case) for case in cases))
    return {
        "baseline_id": "semantic-router-live-v1",
        "model": llm.model,
        "case_count": len(rows),
        "parsed": sum(row["status"] == "parsed" for row in rows),
        "database_admitted": sum(
            row.get("route", {}).get("should_generate_sql", False) for row in rows
        ),
        "rows": rows,
        "limitations": "Intent and plan validation only; no SQL execution or answer quality score.",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--cases",
        type=Path,
        default=Path("evals/cases/semantic_router_baseline_v1.yaml"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("evals/results/semantic_router_baseline_v1.json"),
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Call the configured provider, never execute SQL.",
    )
    args = parser.parse_args()
    result = asyncio.run(run_live(args.cases)) if args.live else run(args.cases)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    if not args.live:
        for row in result["rows"]:
            print(
                f"{row['id']}: {row['state']} / {row['reason_code']} / {','.join(row['metric_ids'])}"
            )
    print(f"wrote {args.output}")


if __name__ == "__main__":
    main()
