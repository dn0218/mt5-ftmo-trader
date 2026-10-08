"""One-shot config validation — run after filling .env. Safe to delete."""
import sys
sys.path.insert(0, ".")
from dotenv import load_dotenv
load_dotenv()

from config import Config
c = Config.from_env()
print("Config OK")
print("  dry_run   =", c.dry_run)
print("  provider  =", c.llm_provider)
print("  model     =", c.llm_model)
print("  symbols   =", c.symbols)
print("  LLM check =", "PASSED" if c.check_llm() else "FAILED (endpoint/key/model issue)")
