"""Inspect _correlation_guard function in engine.py."""
from pathlib import Path
import re

engine_code = Path("services/worker/engine.py").read_text(encoding="utf-8")

# Find the _correlation_guard function definition
match = re.search(r"def _correlation_guard\(.*?\):\s*\"\"\"(.*?)\"\"\"(.*?)(?=\n\s*def|\n\s*class|\n#|$)", engine_code, re.DOTALL)

if match:
    docstring = match.group(1).strip()
    function_body = match.group(2).strip()
    print("=== _correlation_guard Docstring ===\n", docstring)
    print("\n=== _correlation_guard Function Body ===\n", function_body)
else:
    print("_correlation_guard function not found.")

# Check CORRELATION_GUARD in .env
cfg_content = Path(".env").read_text(encoding="utf-8-sig")
correlation_guard_setting = "Not found"
for line in cfg_content.splitlines():
    if line.startswith("CORRELATION_GUARD="):
        correlation_guard_setting = line.split("=", 1)[1].strip()
        break
print(f"\nCORRELATION_GUARD in .env: {correlation_guard_setting}")
