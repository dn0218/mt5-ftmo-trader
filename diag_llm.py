"""Diagnose raw LLM response structure. Prints NO secrets. Safe to delete."""
import os, sys, json
sys.path.insert(0, ".")
from dotenv import load_dotenv
load_dotenv()

import httpx

base = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
key = os.getenv("OPENAI_API_KEY", "")
model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

msg = [{"role": "system", "content": "Respond with JSON: {\"decision\": \"ALLOW\", \"confidence\": 0.9, \"justification\": \"...\"}"},
       {"role": "user", "content": "Test: EURUSD BUY setup, allow or refuse?"}]

def probe(label, payload):
    print(f"\n=== {label} ===")
    try:
        r = httpx.post(f"{base}/chat/completions", json=payload,
                       headers={"Authorization": f"Bearer {key}"},
                       timeout=float(os.getenv("LLM_TIMEOUT", "30")))
        print("HTTP", r.status_code)
        if r.status_code != 200:
            print("Body:", r.text[:400]); return
        d = r.json()
        ch = d.get("choices", [{}])[0]
        m = ch.get("message", {})
        print("finish_reason:", ch.get("finish_reason"))
        print("message keys:", sorted(m.keys()))
        print("content:", repr(m.get("content"))[:300])
        if m.get("reasoning_content"):
            print("reasoning_content (first 200):", repr(m["reasoning_content"])[:200])
        print("usage:", d.get("usage"))
    except Exception as e:
        print("ERROR:", type(e).__name__, str(e)[:300])

# A: exact bot payload (max_tokens=200 + json_object)
probe("A: bot-identical (max_tokens=200, json_object)",
      {"model": model, "messages": msg, "max_tokens": 200, "temperature": 0.1,
       "response_format": {"type": "json_object"}})

# B: larger budget, same json_object
probe("B: max_tokens=2000, json_object",
      {"model": model, "messages": msg, "max_tokens": 2000, "temperature": 0.1,
       "response_format": {"type": "json_object"}})

# C: larger budget, no response_format
probe("C: max_tokens=2000, plain",
      {"model": model, "messages": msg, "max_tokens": 2000, "temperature": 0.1})
