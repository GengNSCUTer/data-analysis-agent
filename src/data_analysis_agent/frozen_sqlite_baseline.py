"""Frozen local-model generation for offline SQLite Text-to-SQL benchmarks.

This module is deliberately separate from the PostgreSQL/Vanna runtime.  It
turns a native benchmark question and read-only SQLite schema into one model
candidate, then emits the minimal JSONL contract consumed by
``sqlite_benchmark``.  Gold SQL and result rows never participate in prompt
construction or prediction serialization.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
import re
import sqlite3
from time import perf_counter
from typing import Any, Iterable, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from data_analysis_agent.sqlite_benchmark import (
    BenchmarkCase,
    BenchmarkInputError,
    SqlPrediction,
    resolve_benchmark_database_path,
)


SUPPORTED_DATASETS = frozenset({"bird_dev", "spider_dev"})
SQL_START_PATTERN = re.compile(
    r"\b(?:WITH|SELECT|INSERT|UPDATE|DELETE|CREATE|DROP|ALTER|ATTACH|DETACH|PRAGMA)\b",
    re.IGNORECASE,
)
FENCED_SQL_PATTERN = re.compile(
    r"```(?:sql|sqlite)?\s*(.*?)```", re.IGNORECASE | re.DOTALL
)


class BaselineGenerationError(ValueError):
    """The frozen baseline cannot safely build or persist a prediction."""


@dataclass(frozen=True)
class GenerationCase:
    """Native benchmark input retained only for one in-memory model request."""

    case_id: str
    database_id: str
    database_path: str
    question: str

    def benchmark_case(self) -> BenchmarkCase:
        """Return the question-free locator required by the execution adapter."""

        return BenchmarkCase(
            case_id=self.case_id,
            database_id=self.database_id,
            database_path=self.database_path,
        )


@dataclass(frozen=True)
class ModelCompletion:
    """Minimal response telemetry needed for a reproducible candidate record."""

    content: str
    generated_tokens: int | None
    generation_elapsed_ms: int


class ChatClient(Protocol):
    """Small boundary that keeps tests independent from a running model server."""

    def complete(self, prompt: str) -> ModelCompletion:
        """Return one model completion for a fully rendered benchmark prompt."""


class OllamaChatClient:
    """Deterministic, no-tools Ollama client for an already pulled frozen model."""

    def __init__(
        self,
        *,
        model: str,
        base_url: str = "http://127.0.0.1:11434",
        seed: int = 42,
        temperature: float = 0.0,
        max_tokens: int = 512,
        timeout_seconds: int = 120,
    ):
        if not model.strip():
            raise BaselineGenerationError("model must be a non-empty string")
        if not base_url.startswith(("http://", "https://")):
            raise BaselineGenerationError("base_url must start with http:// or https://")
        if seed < 0:
            raise BaselineGenerationError("seed must be non-negative")
        if not 0 <= temperature <= 1:
            raise BaselineGenerationError("temperature must be in [0, 1]")
        if max_tokens <= 0:
            raise BaselineGenerationError("max_tokens must be greater than zero")
        if timeout_seconds <= 0:
            raise BaselineGenerationError("timeout_seconds must be greater than zero")

        self.model = model
        self.endpoint = base_url.rstrip("/") + "/api/chat"
        self.seed = seed
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout_seconds = timeout_seconds

    def complete(self, prompt: str) -> ModelCompletion:
        payload = {
            "model": self.model,
            "stream": False,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You generate one SQLite SQL query for a benchmark. "
                        "Never call tools and never explain your answer."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            "options": {
                "seed": self.seed,
                "temperature": self.temperature,
                "top_k": 1,
                "top_p": 1,
                "num_predict": self.max_tokens,
            },
        }
        request = Request(
            self.endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        started_at = perf_counter()
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                raw_response = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise BaselineGenerationError(
                f"Ollama completion request failed: {type(exc).__name__}"
            ) from exc

        message = raw_response.get("message")
        content = message.get("content") if isinstance(message, Mapping) else None
        if not isinstance(content, str) or not content.strip():
            raise BaselineGenerationError("Ollama response did not contain assistant content")
        generated_tokens = raw_response.get("eval_count")
        if isinstance(generated_tokens, bool) or not isinstance(generated_tokens, int):
            generated_tokens = None
        return ModelCompletion(
            content=content,
            generated_tokens=generated_tokens,
            generation_elapsed_ms=round((perf_counter() - started_at) * 1_000),
        )


def load_native_generation_cases(
    items: Iterable[Mapping[str, Any]], *, dataset_id: str
) -> list[GenerationCase]:
    """Keep question text in memory while mapping native order to stable case IDs."""

    if dataset_id not in SUPPORTED_DATASETS:
        raise BaselineGenerationError(f"unsupported dataset_id: {dataset_id}")
    cases: list[GenerationCase] = []
    for index, item in enumerate(items):
        database_id = _required_string(item, "db_id")
        question = _required_string(item, "question")
        cases.append(
            GenerationCase(
                case_id=f"{dataset_id}:{index:05d}",
                database_id=database_id,
                database_path=f"{database_id}/{database_id}.sqlite",
                question=question,
            )
        )
    return cases


def render_sqlite_schema(
    database_root: Path,
    case: GenerationCase,
    *,
    max_schema_characters: int = 20_000,
) -> str:
    """Read table DDL only from the declared SQLite database.

    Schema text deliberately excludes table rows and values.  A schema that
    does not fit the server-owned budget fails explicitly rather than being
    silently truncated into an invalid model context.
    """

    if max_schema_characters <= 0:
        raise BaselineGenerationError("max_schema_characters must be greater than zero")
    database_path = resolve_benchmark_database_path(
        database_root, case.database_path
    )
    connection = sqlite3.connect(database_path.as_uri() + "?mode=ro", uri=True)
    try:
        connection.execute("PRAGMA query_only = ON")
        rows = connection.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
    finally:
        connection.close()
    statements = [str(sql).strip() for _name, sql in rows if isinstance(sql, str)]
    if not statements:
        raise BaselineGenerationError("SQLite database has no user table definitions")
    schema = "\n\n".join(statements)
    if len(schema) > max_schema_characters:
        raise BaselineGenerationError(
            "SQLite schema exceeds max_schema_characters; add a server-owned "
            "schema selection step before running this baseline"
        )
    return schema


def render_sql_prompt(case: GenerationCase, schema: str) -> str:
    """Create prompt version ``sqlite-frozen-baseline-v1`` for one native case."""

    return (
        "Write exactly one SQLite SQL query that answers the question.\n"
        "Use only tables and columns present in the schema.\n"
        "Return SQL only: no Markdown, no prose, no code fence.\n"
        "Do not add a default LIMIT unless the question requires one.\n\n"
        f"Schema:\n{schema}\n\n"
        f"Question:\n{case.question}\n\n"
        "SQL:"
    )


def extract_sql_candidate(content: str) -> str:
    """Remove benign response wrappers without silently correcting model SQL.

    The downstream ``SqliteBenchmarkPolicy`` remains the sole authority for
    SQL safety.  A returned DML/DDL query is intentionally preserved so the
    diagnostic report records a policy rejection rather than hiding a model
    failure as a missing prediction.
    """

    text = content.strip()
    if not text:
        raise BaselineGenerationError("model completion is empty")
    fenced_match = FENCED_SQL_PATTERN.search(text)
    if fenced_match is not None:
        text = fenced_match.group(1).strip()
    text = re.sub(r"^(?:sql|sqlquery)\s*:\s*", "", text, flags=re.IGNORECASE)
    sql_start = SQL_START_PATTERN.search(text)
    if sql_start is not None:
        text = text[sql_start.start() :]
    if not text.strip():
        raise BaselineGenerationError("model completion does not contain a SQL candidate")
    return text.strip()


def generate_predictions(
    *,
    cases: Iterable[GenerationCase],
    database_root: Path,
    client: ChatClient,
    max_schema_characters: int = 20_000,
) -> list[SqlPrediction]:
    """Generate one candidate per case without executing any model output."""

    predictions: list[SqlPrediction] = []
    for case in cases:
        schema = render_sqlite_schema(
            database_root, case, max_schema_characters=max_schema_characters
        )
        completion = client.complete(render_sql_prompt(case, schema))
        predictions.append(
            SqlPrediction(
                case_id=case.case_id,
                candidate_sql=extract_sql_candidate(completion.content),
                candidate_index=0,
                generated_tokens=completion.generated_tokens,
                generation_elapsed_ms=completion.generation_elapsed_ms,
            )
        )
    return predictions


def load_existing_prediction_case_ids(path: Path) -> set[str]:
    """Load existing output IDs for explicit resume mode without raw prompts."""

    if not path.exists():
        return set()
    case_ids: set[str] = set()
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise BaselineGenerationError(
                f"existing prediction JSONL is invalid on line {line_number}"
            ) from exc
        prediction = SqlPrediction.from_mapping(raw)
        if prediction.candidate_index != 0:
            raise BaselineGenerationError(
                "frozen baseline resume only supports candidate_index 0"
            )
        if prediction.case_id in case_ids:
            raise BaselineGenerationError("existing prediction JSONL has duplicate case IDs")
        case_ids.add(prediction.case_id)
    return case_ids


def append_predictions(path: Path, predictions: Iterable[SqlPrediction]) -> None:
    """Append only Adapter-compatible records; never write prompts or questions."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        for prediction in predictions:
            stream.write(json.dumps(asdict(prediction), ensure_ascii=False) + "\n")
            stream.flush()


def ensure_path_outside_repository(path: Path, repository_root: Path) -> Path:
    """Keep raw model predictions out of the Git worktree by construction."""

    resolved_path = path.resolve()
    resolved_repository = repository_root.resolve()
    if resolved_path.is_relative_to(resolved_repository):
        raise BaselineGenerationError("prediction output must stay outside the repository")
    return resolved_path


def _required_string(raw: Mapping[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise BaselineGenerationError(f"{key} must be a non-empty string")
    return value.strip()
