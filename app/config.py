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
        "read_csv", "read_csv_auto", "read_parquet", "read_json", "read_json_auto",
        "read_json_objects", "glob", "sniff_csv", "read_text", "read_blob",
        "getenv", "current_setting",
    }))


@dataclass
class Settings:
    model: str = os.getenv("TEXT2SQL_MODEL", "claude-sonnet-5")
    db_path: str = os.path.join(ROOT, os.getenv("TEXT2SQL_DB_PATH", "data/warehouse.duckdb"))
    log_dir: str = os.path.join(ROOT, "logs")
    guardrails: GuardrailConfig = field(default_factory=GuardrailConfig)
    # Below this combined confidence, the UI shows a "low confidence" warning
    low_confidence_threshold: float = 0.6


settings = Settings()
os.makedirs(settings.log_dir, exist_ok=True)
