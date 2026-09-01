from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # Keeps static analysis usable before optional setup completes.
    load_dotenv = None


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if load_dotenv is not None:
    load_dotenv(_PROJECT_ROOT / ".env", override=False)


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        return default


@dataclass(frozen=True, slots=True)
class Settings:
    app_name: str
    log_level: str
    log_value_limit: int
    trace_functions: bool
    max_trace: bool
    trace_file_enabled: bool
    trace_path: Path
    allowed_root: Path
    clone_root: Path
    clone_timeout_seconds: int
    max_clone_bytes: int
    max_clone_files: int
    max_retained_clones: int
    max_repository_files: int
    max_python_file_bytes: int
    max_unresolved_call_details: int
    anthropic_api_key: str | None = field(repr=False)
    openrouter_api_key: str | None = field(repr=False)
    openrouter_base_url: str
    openrouter_timeout_seconds: int
    bedrock_region: str | None
    bedrock_access_key_id: str | None = field(repr=False)
    bedrock_secret_access_key: str | None = field(repr=False)
    bedrock_timeout_seconds: int
    bedrock_primary_model: str
    gemini_api_key: str | None = field(repr=False)
    gemini_base_url: str
    gemini_timeout_seconds: int
    gemini_fallback_model: str
    model_name: str
    model_architecture: str
    investigation_provider: str
    investigation_model: str
    investigation_bedrock_model: str
    synthesis_provider: str
    synthesis_model: str
    synthesis_bedrock_model: str
    investigation_rounds: int
    synthesis_max_attempts: int
    claude_code_fallback: bool
    claude_code_model: str | None
    claude_code_timeout_seconds: int
    claude_code_max_turns: int
    claude_code_max_budget_usd: float | None
    provider_startup_probe: bool
    chat_history_turns: int
    chat_semantic_memory_items: int
    chat_memory_retention_turns: int
    chat_memory_max_characters: int
    chat_memory_min_similarity: float
    agent_max_tool_rounds: int
    agent_max_schema_corrections: int
    agent_max_submission_corrections: int
    agent_max_output_tokens: int
    github_token: str | None = field(repr=False)
    state_path: Path
    cors_origins: tuple[str, ...]

    @classmethod
    def from_environment(cls) -> "Settings":
        workspace = Path(
            os.getenv("ONBOARD_ALLOWED_ROOT", Path.cwd())
        ).expanduser().resolve()
        clone_root = Path(
            os.getenv("ONBOARD_CLONE_ROOT", workspace / ".waypoint-clones")
        ).expanduser().resolve()
        return cls(
            app_name="adaptive-codebase-onboarding",
            log_level=os.getenv("ONBOARD_LOG_LEVEL", "INFO").upper(),
            log_value_limit=max(128, _env_int("ONBOARD_LOG_VALUE_LIMIT", 4000)),
            trace_functions=_env_bool("ONBOARD_TRACE_FUNCTIONS", True),
            max_trace=_env_bool("ONBOARD_MAX_TRACE"),
            trace_file_enabled=_env_bool("ONBOARD_TRACE_FILE", True),
            trace_path=Path(
                os.getenv(
                    "ONBOARD_TRACE_PATH",
                    _PROJECT_ROOT / ".waypoint-data" / "traces" / "waypoint.jsonl",
                )
            ).expanduser().resolve(),
            allowed_root=workspace,
            clone_root=clone_root,
            clone_timeout_seconds=max(
                10, _env_int("ONBOARD_CLONE_TIMEOUT_SECONDS", 180)
            ),
            max_clone_bytes=max(
                1_000_000,
                _env_int("ONBOARD_MAX_CLONE_BYTES", 1_000_000_000),
            ),
            max_clone_files=max(
                100,
                _env_int("ONBOARD_MAX_CLONE_FILES", 100_000),
            ),
            max_retained_clones=max(
                1,
                _env_int("ONBOARD_MAX_RETAINED_CLONES", 10),
            ),
            max_repository_files=max(
                1, _env_int("ONBOARD_MAX_REPOSITORY_FILES", 5000)
            ),
            max_python_file_bytes=max(
                1024, _env_int("ONBOARD_MAX_PYTHON_FILE_BYTES", 2_000_000)
            ),
            max_unresolved_call_details=max(
                0, _env_int("ONBOARD_MAX_UNRESOLVED_CALL_DETAILS", 20_000)
            ),
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY") or None,
            openrouter_api_key=(
                os.getenv("OPENROUTER_API_KEY")
                or os.getenv("OPEN_ROUTER_KEY")
                or None
            ),
            openrouter_base_url=os.getenv(
                "WAYPOINT_OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"
            ).rstrip("/"),
            openrouter_timeout_seconds=max(
                15, _env_int("WAYPOINT_OPENROUTER_TIMEOUT_SECONDS", 180)
            ),
            bedrock_region=(
                os.getenv("AWS_REGION")
                or os.getenv("AWS_DEFAULT_REGION")
                or None
            ),
            bedrock_access_key_id=(
                os.getenv("AWS_ACCESS_KEY_ID")
                or os.getenv("ACCESS_KEY")
                or None
            ),
            bedrock_secret_access_key=(
                os.getenv("AWS_SECRET_ACCESS_KEY")
                or os.getenv("SECRET_ACCESS_KEY")
                or None
            ),
            bedrock_timeout_seconds=max(
                15, _env_int("WAYPOINT_BEDROCK_TIMEOUT_SECONDS", 180)
            ),
            bedrock_primary_model=os.getenv(
                "WAYPOINT_BEDROCK_PRIMARY_MODEL", "us.anthropic.claude-sonnet-4-6"
            ),
            gemini_api_key=os.getenv("GEMINI_API_KEY") or None,
            gemini_base_url=os.getenv(
                "WAYPOINT_GEMINI_BASE_URL",
                "https://generativelanguage.googleapis.com/v1beta",
            ).rstrip("/"),
            gemini_timeout_seconds=max(
                15, _env_int("WAYPOINT_GEMINI_TIMEOUT_SECONDS", 180)
            ),
            gemini_fallback_model=os.getenv(
                "WAYPOINT_GEMINI_FALLBACK_MODEL", "gemini-3.1-pro-preview"
            ),
            model_name=os.getenv("WAYPOINT_MODEL", "claude-sonnet-5"),
            model_architecture=os.getenv(
                "WAYPOINT_MODEL_ARCHITECTURE", "single"
            ).strip().lower(),
            investigation_provider=os.getenv(
                "WAYPOINT_INVESTIGATION_PROVIDER", "auto"
            ).strip().lower(),
            investigation_model=os.getenv(
                "WAYPOINT_INVESTIGATION_MODEL", "claude-sonnet-5"
            ).strip(),
            investigation_bedrock_model=os.getenv(
                "WAYPOINT_INVESTIGATION_BEDROCK_MODEL", "us.anthropic.claude-sonnet-4-6"
            ).strip(),
            synthesis_provider=os.getenv(
                "WAYPOINT_SYNTHESIS_PROVIDER", "auto"
            ).strip().lower(),
            synthesis_model=os.getenv(
                "WAYPOINT_SYNTHESIS_MODEL", "claude-opus-5"
            ).strip(),
            synthesis_bedrock_model=os.getenv(
                "WAYPOINT_SYNTHESIS_BEDROCK_MODEL",
                "us.anthropic.claude-opus-4-6-v1",
            ).strip(),
            investigation_rounds=max(
                1, min(10, _env_int("WAYPOINT_INVESTIGATION_ROUNDS", 3))
            ),
            synthesis_max_attempts=max(
                1, min(5, _env_int("WAYPOINT_SYNTHESIS_MAX_ATTEMPTS", 2))
            ),
            claude_code_fallback=_env_bool("WAYPOINT_CLAUDE_CODE_FALLBACK", True),
            claude_code_model=os.getenv("WAYPOINT_CLAUDE_CODE_MODEL", "sonnet") or None,
            claude_code_timeout_seconds=max(
                15, _env_int("WAYPOINT_CLAUDE_CODE_TIMEOUT_SECONDS", 180)
            ),
            claude_code_max_turns=max(
                1, min(8, _env_int("WAYPOINT_CLAUDE_CODE_MAX_TURNS", 8))
            ),
            claude_code_max_budget_usd=(
                max(0.01, _env_float("WAYPOINT_CLAUDE_CODE_MAX_BUDGET_USD", 0.0))
                if _env_float("WAYPOINT_CLAUDE_CODE_MAX_BUDGET_USD", 0.0) > 0
                else None
            ),
            provider_startup_probe=_env_bool("WAYPOINT_PROVIDER_STARTUP_PROBE", True),
            chat_history_turns=max(
                1, min(50, _env_int("WAYPOINT_CHAT_HISTORY_TURNS", 12))
            ),
            chat_semantic_memory_items=max(
                0, min(20, _env_int("WAYPOINT_CHAT_SEMANTIC_MEMORY_ITEMS", 6))
            ),
            chat_memory_retention_turns=max(
                12, min(2000, _env_int("WAYPOINT_CHAT_MEMORY_RETENTION_TURNS", 200))
            ),
            chat_memory_max_characters=max(
                1000, min(50_000, _env_int("WAYPOINT_CHAT_MEMORY_MAX_CHARACTERS", 8000))
            ),
            chat_memory_min_similarity=max(
                0.0, min(1.0, _env_float("WAYPOINT_CHAT_MEMORY_MIN_SIMILARITY", 0.12))
            ),
            agent_max_tool_rounds=max(
                1, min(20, _env_int("WAYPOINT_AGENT_MAX_TOOL_ROUNDS", 8))
            ),
            agent_max_schema_corrections=max(
                0, min(10, _env_int("WAYPOINT_AGENT_MAX_SCHEMA_CORRECTIONS", 2))
            ),
            agent_max_submission_corrections=max(
                0, min(10, _env_int("WAYPOINT_AGENT_MAX_SUBMISSION_CORRECTIONS", 2))
            ),
            agent_max_output_tokens=max(
                512, min(32_000, _env_int("WAYPOINT_AGENT_MAX_OUTPUT_TOKENS", 6000))
            ),
            github_token=os.getenv("GITHUB_TOKEN") or None,
            state_path=Path(
                os.getenv(
                    "WAYPOINT_STATE_PATH",
                    _PROJECT_ROOT / ".waypoint-data" / "waypoint.sqlite3",
                )
            ).expanduser().resolve(),
            cors_origins=tuple(
                origin.strip().rstrip("/")
                for origin in os.getenv("WAYPOINT_CORS_ORIGINS", "").split(",")
                if origin.strip()
            ),
        )


settings = Settings.from_environment()
