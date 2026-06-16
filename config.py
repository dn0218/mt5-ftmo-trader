"""Configuration loader with validation."""

from __future__ import annotations
import os
import logging

logger = logging.getLogger(__name__)

REQUIRED_VARS = [
    "MT5_LOGIN",
    "MT5_PASSWORD",
    "MT5_SERVER",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
]


class Config:
    """Application configuration loaded from environment."""

    def __init__(self):
        self.mt5_login = int(os.getenv("MT5_LOGIN", "0"))
        self.mt5_password = os.getenv("MT5_PASSWORD", "")
        self.mt5_server = os.getenv("MT5_SERVER", "")
        self.symbols = [s.strip() for s in os.getenv("SYMBOLS", "EURUSD").split(",")]
        self.timeframe = os.getenv("TIMEFRAME", "M15")
        self.dry_run = os.getenv("DRY_RUN", "true") == "true"

        # LLM provider: "openai" or "ollama"
        self.llm_provider = os.getenv("LLM_PROVIDER", "openai").lower()
        self.llm_enabled = os.getenv("LLM_ENABLED", os.getenv("OLLAMA_ENABLED", "true")) == "true"

        # OpenAI settings
        self.openai_api_key = os.getenv("OPENAI_API_KEY", "")
        self.openai_model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

        # Ollama settings (fallback)
        self.ollama_model = os.getenv("OLLAMA_MODEL", "qwen3:4b")
        self.ollama_base_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")

    @property
    def llm_model(self) -> str:
        if self.llm_provider == "openai":
            return self.openai_model
        return self.ollama_model

    @classmethod
    def from_env(cls) -> "Config":
        """Load config from environment with validation."""
        missing = [v for v in REQUIRED_VARS if not os.getenv(v)]
        if missing:
            raise ValueError(f"Missing required environment variables: {', '.join(missing)}")

        config = cls()

        if config.llm_provider == "openai" and not config.openai_api_key:
            logger.warning("LLM_PROVIDER=openai but OPENAI_API_KEY not set — LLM will be unavailable")

        logger.info(
            "Config loaded: symbols=%s dry_run=%s llm=%s provider=%s model=%s",
            config.symbols, config.dry_run, config.llm_enabled,
            config.llm_provider, config.llm_model,
        )
        return config

    def check_llm(self) -> bool:
        """Verify LLM backend is reachable."""
        if not self.llm_enabled:
            return True
        try:
            import httpx
            if self.llm_provider == "openai":
                r = httpx.get(
                    "https://api.openai.com/v1/models",
                    headers={"Authorization": f"Bearer {self.openai_api_key}"},
                    timeout=10.0,
                )
                return r.status_code == 200
            else:
                r = httpx.get(f"{self.ollama_base_url}/api/tags", timeout=5.0)
                return r.status_code == 200
        except Exception:
            return False
