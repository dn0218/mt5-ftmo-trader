"""Demo 高风险配置自洽性检查（切换后、启动前必须通过）"""
import os
import sys

from dotenv import load_dotenv

load_dotenv()

risk = float(os.getenv("RISK_PER_TRADE"))
daily = float(os.getenv("MAX_DAILY_DRAWDOWN"))
total = float(os.getenv("MAX_TOTAL_DRAWDOWN"))
concurrent = int(os.getenv("MAX_CONCURRENT_POSITIONS"))
open_risk = float(os.getenv("MAX_TOTAL_OPEN_RISK_PCT"))
consecutive_halt = int(os.getenv("CONSECUTIVE_LOSS_HALT"))

# 硬约束
assert risk < daily, f"单笔 {risk}% 必须 < 日限 {daily}%"
assert risk * concurrent <= daily, f"并发敞口 {risk*concurrent}% 必须 ≤ 日限 {daily}%"
assert open_risk >= risk * concurrent, f"总敞口 {open_risk}% 必须 ≥ 并发 {risk*concurrent}%"
assert daily < total, f"日限 {daily}% 必须 < 总回撤 {total}%"
assert risk * consecutive_halt <= daily, f"连亏 {consecutive_halt} 笔 = {risk*consecutive_halt}% 不能超日限 {daily}%"

print(f"✅ 配置自洽：单笔 {risk}% / 日限 {daily}% / 总回撤 {total}% / 总敞口 {open_risk}%")
print(f"   最坏情况：连亏 {consecutive_halt} 笔 = {risk*consecutive_halt}%（触发 halt），当天最多亏损约 {risk*concurrent}%")
print(f"   总回撤 {total}% 约 {int(total/risk)} 笔连续止损触发")
