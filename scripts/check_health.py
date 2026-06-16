"""Health check script — verifies all 8+ components."""

from __future__ import annotations
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv
load_dotenv()

import logging
logging.basicConfig(level=logging.WARNING)


def _check_python():
    return sys.version_info >= (3, 12)


def _check_imports():
    try:
        import pandas, numpy, pydantic, httpx, dotenv, pytz
        return True
    except ImportError as e:
        print(f"  Missing package: {e}")
        return False


def _check_env():
    required = ["MT5_LOGIN", "MT5_PASSWORD", "MT5_SERVER",
                 "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]
    missing = [v for v in required if not os.getenv(v)]
    if missing:
        print(f"  Missing: {', '.join(missing)}")
        return False
    return True


def _check_ollama():
    import httpx
    try:
        r = httpx.get("http://localhost:11434/api/tags", timeout=3)
        return r.status_code == 200
    except Exception:
        return False


def _check_model():
    import httpx
    try:
        r = httpx.get("http://localhost:11434/api/tags", timeout=3)
        if r.status_code != 200:
            return False
        models = r.json().get("models", [])
        return any("qwen3:4b" in m.get("name", "") for m in models)
    except Exception:
        return False


def _check_mt5_process():
    try:
        import subprocess
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq terminal64.exe"],
            capture_output=True, text=True, timeout=5,
        )
        return "terminal64.exe" in result.stdout
    except Exception:
        return False


def _check_mt5_connect():
    try:
        import MetaTrader5 as mt5
        if not mt5.initialize():
            return False
        login = int(os.getenv("MT5_LOGIN", "0"))
        password = os.getenv("MT5_PASSWORD", "")
        server = os.getenv("MT5_SERVER", "")
        result = mt5.login(login=login, password=password, server=server)
        return bool(result)
    except Exception:
        return False


def _check_telegram():
    from services.notifications.telegram import notify
    return notify("🏥 Health check ping")


def _check_news_feed():
    import httpx
    try:
        r = httpx.get(
            "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
            timeout=10, follow_redirects=True,
        )
        return r.status_code == 200
    except Exception:
        return False


def main():
    checks = [
        ("Python >= 3.12", _check_python),
        ("All packages importable", _check_imports),
        (".env exists + complete", _check_env),
        ("Ollama running", _check_ollama),
        ("qwen3:4b downloaded", _check_model),
        ("MT5 terminal running", _check_mt5_process),
        ("MT5 Python connects", _check_mt5_connect),
        ("Telegram works", _check_telegram),
        ("ForexFactory reachable", _check_news_feed),
    ]

    results = []
    for name, check in checks:
        try:
            ok = check()
            status = "✅" if ok else "❌"
            results.append((name, ok))
            print(f"{status} {name}")
        except Exception as e:
            results.append((name, False))
            print(f"❌ {name}: {e}")

    all_pass = all(ok for _, ok in results)
    print()
    if all_pass:
        print("✅ ALL CHECKS PASSED")
        from services.notifications.telegram import notify
        msg = "✅ HEALTH CHECK PASSED\n\n"
        for name, ok in results:
            msg += f"{'✅' if ok else '❌'} {name}\n"
        notify(msg)
    else:
        print("❌ SOME CHECKS FAILED")
        from services.notifications.telegram import notify
        msg = "❌ HEALTH CHECK — some failures:\n\n"
        for name, ok in results:
            msg += f"{'✅' if ok else '❌'} {name}\n"
        notify(msg)

    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
