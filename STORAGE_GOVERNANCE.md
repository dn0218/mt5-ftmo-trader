# Storage Governance — 1 GB 分层保留策略

> 实现于 2026-10-05。目标：半年以上持续运行不因磁盘满中断，复盘能力不削弱。
> 当前占用 ~28 MB（storage/），上限 1 GB，预算余量 ~97%。

## 目录结构

```
storage/
├── logs/
│   ├── audit.jsonl                 # 流A：操作日志（噪音），>2MB 日轮转，30天
│   ├── audit_YYYYMMDD.jsonl        #   轮转归档（30天后由守卫删除）
│   ├── audit_lifecycle.jsonl       # 流B：交易生命周期（本月，永久）
│   └── bot.log                     # 不变（TimedRotating 14天）
├── audit_lifecycle/
│   └── {YYYY-MM}.jsonl.gz          # 流B月度压缩归档（永久，守卫最后动它）
├── snapshots/
│   └── {YYYYMMDD}/
│       └── {symbol}_{side}_{ticket}.json.gz   # 入场时 K 线冻结（90天）
└── db/                             # performance.json 等，永不可删
    └── storage_state.json          # 守卫写入：{storage_full: bool}
```

## 保留期与预算

| 类别 | 当前 | 保留期 | 预算 |
|------|------|--------|------|
| 操作日志（audit.jsonl + 归档） | 28 MB | 30 天 | ≤60 MB |
| 生命周期日志（lifecycle 流 + gz） | ~0 | 永久（>180天才可能被守卫删） | ≤50 MB |
| K 线快照 | 0 | 90 天 | ≤150 MB |
| db/（performance 等） | <1 MB | 永久 | — |
| **合计** | **~28 MB** | — | **≤1 GB（硬上限）** |

## 容量守卫（scripts/storage_guard.py，每日 04:00 UTC）

| 阈值 | 行为 |
|------|------|
| ≤800 MB | 只写 STORAGE_STATUS 审计事件（占用/分类/余量） |
| >800 MB | 按序删除：操作归档(>30天) → 快照(>90天) → 生命周期gz(>180天)；每步后重算 |
| 仍>800 MB | **停止删除**，写 STORAGE_CRITICAL，Telegram 告警 |
| >950 MB | `storage_state.json` 标 full → `RiskManager.can_trade()` 拒绝新开仓（STORAGE_FULL）；平仓/管理不受影响 |

删除白名单外的文件**永不触碰**（包括 `archive_mock_purge_*.bak` 这类未知文件——宁可占盘不可误删）。
所有失败路径 fail-safe：删除失败 → 跳过保留文件；状态文件读不到 → 不拦截交易。

## 快照机制（services/snapshots/snapshot_writer.py）

- 触发：每笔 `TRADE_REGISTERED`（SMC 与 breakout 两条路径都已挂接）
- 内容：M15×500 OHLCV + H4×80 + ATR14/EMA50/点差/signal dict
- 异步：`submit()` 纯入队（实测 <1ms），daemon 线程 gzip 落盘，绝不阻塞扫描循环
- 事件：SNAPSHOT_SAVED / SNAPSHOT_FAILED（lifecycle 流，可追溯）
- 队列上限 64：满则丢弃该笔快照并记 SNAPSHOT_FAILED，交易不受影响

## 分析脚本兼容（scripts/audit_reader.py）

`analyze_rejects.py` / `monitor_gates.py` / `b3_refuse_buckets.py` 已改为合并读：
旧 `audit.jsonl` + 轮转归档 + lifecycle 流 + gz 归档，按时间排序输出。
支持 `--since YYYY-MM-DD`。新结构不存在时自动回退旧单文件。

## 运维

```powershell
# 手动跑守卫（只报告不删）
.venv\Scripts\python.exe scripts\storage_guard.py --dry
# 端到端验证（快照→日志→归档→守卫→拒单，沙箱内）
.venv\Scripts\python.exe scripts\e2e_storage_verify.py
```

## 测试

- `tests/test_snapshots.py` — 3 用例：保存/读回、异步不阻塞、90天过期清理
- `tests/test_audit_streams.py` — 5 用例：双流分离、默认兼容、事件名强制lifecycle、月度归档+合并读、AUDIT_LOG_OVERRIDE 隔离
- `tests/test_storage_guard.py` — 4 用例：阈值内不动、分层删除顺序、CRITICAL 停止、STORAGE_FULL 拒单
- `tests/test_audit_reader.py` — 4 用例：新旧结构、since 过滤、argv 解析
- 合计新增 16 个，全套 **148 passed**（基线 132）
