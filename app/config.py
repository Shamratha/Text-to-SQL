"""Central configuration, loaded once from environment / .env."""
import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@dataclass
class GuardrailConfig:
    allow_only_select: bool = True
    max_subquery_depth: int = 3
    default_row_limit: int = int(os.getenv("TEXT2SQL_ROW_LIMIT", "1000"))
    query_timeout_s: float = float(os.getenv("TEXT2SQL_QUERY_TIMEOUT_S", "10"))
    blocked_functions: frozenset = field(default_factory=lambda: frozenset({
        # file / blob readers (data exfiltration)
        "read_csv", "read_csv_auto", "read_parquet", "read_json", "read_json_auto",
        "read_json_objects", "glob", "sniff_csv", "read_text", "read_blob",
        # environment / settings (info disclosure)
        "getenv", "current_setting",
        # DuckDB introspection table functions (config, paths, and — critically —
        # stored secrets/credentials leakage)
        "duckdb_settings", "duckdb_databases", "duckdb_secrets", "duckdb_extensions",
        "duckdb_functions", "duckdb_logs", "pragma_database_list", "pragma_table_info",
        "sql_auto_complete",
    }))


def _detect_provider() -> str:
    """Explicit LLM_PROVIDER wins; otherwise pick by which key is configured."""
    explicit = os.getenv("LLM_PROVIDER", "").strip().lower()
    if explicit:
        return explicit
    if os.getenv("GROQ_API_KEY"):
        return "groq"
    return "anthropic"


_PROVIDER_DEFAULTS = {
    # provider: (base_url, key_env, default_model)
    "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY", "llama-3.3-70b-versatile"),
    "openai_compatible": (os.getenv("LLM_BASE_URL", ""), "LLM_API_KEY", ""),
    "anthropic": ("", "ANTHROPIC_API_KEY", "claude-sonnet-5"),
}


@dataclass
class Settings:
    llm_provider: str = _detect_provider()
    llm_base_url: str = ""
    llm_api_key: str = ""
    model: str = ""
    db_path: str = os.path.join(ROOT, os.getenv("TEXT2SQL_DB_PATH", "data/warehouse.duckdb"))
    log_dir: str = os.path.join(ROOT, "logs")
    guardrails: GuardrailConfig = field(default_factory=GuardrailConfig)
    # Below this combined confidence, the UI shows a "low confidence" warning
    low_confidence_threshold: float = 0.6


settings = Settings()

_base_url, _key_env, _default_model = _PROVIDER_DEFAULTS.get(
    settings.llm_provider, _PROVIDER_DEFAULTS["openai_compatible"]
)
settings.llm_base_url = os.getenv("LLM_BASE_URL", _base_url)
settings.llm_api_key = os.getenv(_key_env, "")
settings.model = os.getenv("TEXT2SQL_MODEL") or _default_model

os.makedirs(settings.log_dir, exist_ok=True)
