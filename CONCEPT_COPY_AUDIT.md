# Concept-Copy Audit — 同一计算的多处副本

> 背景：两类生产 bug 都是"同一概念在两个模块各算一遍，修了一处漏一处"。
> 1. pip 换算：`calculate_volume` 用 `pip_size()`，`validate_sl_tp` 曾各算各的
> 2. R 锚点：BE/current_r 用成交价几何，tp1/tp2 曾用信号 bar 几何（9/28 实锤）
>
> 本表是定位手册：再遇到"修复后行为仍不对"时，先查这里的副本对。

| # | 概念 | 副本位置 | 当前状态 | 单一来源 |
|---|------|----------|----------|----------|
| 1 | **pip 大小** | a) `core/mt5/symbols.py::pip_size` b) `risk_manager._check_daily_dd_risk` 内联 `10*point if digits in (3,5)` | ⚠️ b 是近似副本（XAU digits=2 走 point 分支，两处一致；但若上 3 位小数的品种会分叉） | 全部改走 `pip_size()` |
| 2 | **R 距离（SL 距离）** | a) `manager` 冻结 `initial_sl_dist` b) `manager._handle_closed_position` 的 r_multiple 计算用 `trade["initial_sl_dist"]` c) `_total_open_risk`/`_check_daily_dd_risk` 用 `p.price_open - p.sl`（MT5 持仓实时） | ✅ a=b（同一冻结值）；c 是**实时**口径，与 a 的"开仓时冻结"不同——这是有意的（敞口该用当前 SL），不是 bug | R 语义分裂：`initial R`（记账）vs `current R`（风控），注释已写明 |
| 3 | **手数计算** | 只有 `risk_manager.calculate_volume` 一处 | ✅ 单副本 | — |
| 4 | **ATR** | a) `breakout._atr14`（原始 rolling14） b) engine 传给 validate_sl_tp 的 `atr`（EMA5 平滑版，SL_ATR 模式用） | ⚠️ 有意为之但易误读：SL 门用平滑版抗尖峰，breakout 信号用原始版。**不要**统一 | 语义不同，各自保留，此表存档 |
| 5 | **点差上限** | a) `_check_spread` 读 `MAX_SPREAD_POINTS_XXX`（per-symbol） b) `.env` 生效值 | ✅ 单副本 + per-symbol 覆盖 | — |
| 6 | **时段** | a) kill zone 判定（SCAN_SKIP） b) trading window 07-20 UTC（engine 外层循环） c) `_is_eos_guard_active`（BE guard 的 TRADING_END 前 N 分钟） | ✅ 三者读同一组 env（`SESSION_KILL_ZONES` / `TRADING_START/END_UTC`） | env 是单一来源 |
| 7 | **TP 阶梯锚点** | a) engine 算 sig ladder（信号几何） b) manager 重锚到 fill（10/4 修复） c) dry_run_tracker 用信号几何（虚拟仓，无滑点，无所谓） | ✅ b 强制统一到成交几何；engine 现已传 `sig_entry` | `register_trade(entry=fill, sig_entry=sig)` 双参数显式化 |

## 已知的有意分裂（不要"修"）

- `initial R` vs `current R`：记账用冻结值（SL 移动不改历史），敞口风控用实时值
- ATR 原始 vs EMA5 平滑：信号 vs SL 门，抗尖峰设计
- dry_run_tracker 的信号几何锚：无真实成交，无滑点概念

## 下次遇到"修了还错"的排查顺序

1. 价格/距离不对 → 查 #1（pip）和 #7（锚点）
2. 手数不对 → 查 #2（R 语义）——是冻结还是实时？
3. 门时开时关 → 查 #5（点差）#6（时段）
