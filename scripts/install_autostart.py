"""Install the trading bot as a Windows Scheduled Task that starts at logon.

Run as administrator:  python scripts/install_autostart.py
Remove:                python scripts/install_autostart.py --remove
"""

from __future__ import annotations
import os
import sys
import subprocess

TASK_NAME = "MT5_FTMO_TradingBot"
BOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYTHON = os.path.join(BOT_DIR, ".venv", "Scripts", "pythonw.exe")
SCRIPT = os.path.join(BOT_DIR, "main.py")


def install():
    """Create a scheduled task that starts the bot at logon."""
    # Use pythonw.exe (no console window) for background operation
    cmd = [
        "schtasks", "/Create",
        "/TN", TASK_NAME,
        "/TR", f'"{PYTHON}" "{SCRIPT}"',
        "/SC", "ONLOGON",
        "/RL", "HIGHEST",
        "/F",  # force overwrite if exists
    ]
    print(f"Installing scheduled task: {TASK_NAME}")
    print(f"  Python: {PYTHON}")
    print(f"  Script: {SCRIPT}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode == 0:
        print(f"\n✅ Task '{TASK_NAME}' created successfully!")
        print("The bot will start automatically when you log in to Windows.")
        print(f"\nTo start it now:  schtasks /Run /TN {TASK_NAME}")
        print(f"To check status:  schtasks /Query /TN {TASK_NAME}")
        print(f"To remove:        python scripts/install_autostart.py --remove")
    else:
        print(f"\n❌ Failed to create task:")
        print(result.stderr)
        if "Access is denied" in result.stderr:
            print("\nTip: Run this script as Administrator")


def remove():
    """Remove the scheduled task."""
    cmd = ["schtasks", "/Delete", "/TN", TASK_NAME, "/F"]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode == 0:
        print(f"✅ Task '{TASK_NAME}' removed.")
    else:
        print(f"❌ Failed: {result.stderr}")


if __name__ == "__main__":
    if "--remove" in sys.argv:
        remove()
    else:
        install()
