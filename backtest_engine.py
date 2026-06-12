#!/usr/bin/env python3
"""
backtest_engine.py — A股回调低吸策略 v3.0 专属回测引擎

=============================================================================
使用方式:
  python3 backtest_engine.py                                     # 默认回测
  python3 backtest_engine.py --start 2024-06-01 --end 2026-06-01 # 指定区间
  python3 backtest_engine.py --fast                              # 快速模式(仅最近1年)
  python3 backtest_engine.py --save-json report.json             # 输出JSON报告
  python3 backtest_engine.py --no-benchmark                      # 跳过基准对比
  python3 backtest_engine.py --verbose                            # 详细日志

数据源: mootdx(通达信) + HTTP备用K线 + 缓存
=============================================================================
"""

import argparse, json, sys, time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

# ── 回测子模块 ──
from backtest_proxy import (
    KlineCache, MarketThermometerProxy, SectorScorerProxy,
    get_stock_name, filter_stock_pool,
)
from backtest_simulator import (
    TradeEngine, SignalReplayer, Signal,
)
from backtest_analytics import (
    PerformanceAnalyzer, load_benchmark_returns,
)


# ═══════════════════════════════════════════════════════════
# 回测配置
# ═══════════════════════════════════════════════════════════
@dataclass
class BacktestConfig:
    """所有可调回测参数"""

    # ── 时间范围 ──
    start_date: str = "2024-01-01"
    end_date: str = ""

    # ── 资金 ──
    initial_capital: float = 1_000_000

    # ── 交易规则 ──
    buy_at_next_open: bool = True
    t_plus_1: bool = True
    stop_loss: float = 0.08        # -8%
    take_profit: float = 0.15      # +15%（设 0 禁用）
    max_hold_days: int = 20
    trailing_stop_activate: float = 0.10  # 涨超+10%后激活移动止盈
    trailing_stop_drawdown: float = 0.06  # 从最高点回撤6%离场

    # ── 仓位 ──
    half_kelly: bool = True
    max_single_position: float = 0.80
    max_concurrent_positions: int = 10

    # ── 代理开关 ──
    use_market_temp: bool = True
    use_sector_score: bool = True

    # ── 策略参数覆盖（None = 原策略默认） ──
    drawdown_min: Optional[float] = None
    drawdown_max: Optional[float] = None
    vol_ratio_min: Optional[float] = None
    atr_min: Optional[float] = None
    atr_max: Optional[float] = None

    # ── 费用（A股标准） ──
    commission_rate: float = 0.00025     # 万2.5
    stamp_tax_rate: float = 0.001       # 千1（卖出）
    min_commission: float = 5.0

    # ── 输出 ──
    output_dir: str = "./backtest_results"
    verbose: bool = False
    save_json: bool = True
    benchmark: bool = True


# ═══════════════════════════════════════════════════════════
# BacktestEngine — 回测引擎主控制器
# ═══════════════════════════════════════════════════════════
class BacktestEngine:
    """
    管理回测完整生命周期：
    初始化 → 预热 → 逐日回放 → 分析 → 报告
    """

    def __init__(self, config: BacktestConfig):
        self.config = config
        self.start_time = time.time()

        # 如果 end_date 为空，设为今天
        if not config.end_date:
            config.end_date = datetime.now().strftime("%Y-%m-%d")

        # 子模块
        self.cache = KlineCache()
        self.temp_proxy = MarketThermometerProxy()
        self.sector_proxy = None  # 延迟初始化（需要 stock_pool）
        self.replayer = SignalReplayer()
        self.engine = TradeEngine(config)

        # 运行时状态
        self.trading_dates: List[str] = []
        self.date_idx_map: Dict[str, int] = {}
        self._warmed_up = False

        # 结果
        self.results = {}
        self.analyzer = None

        print(f"\n{'█' * 60}")
        print(f"  A股回调低吸策略 v3.0 回测引擎")
        print(f"  区间: {config.start_date} ~ {config.end_date}")
        print(f"  初始资金: ¥{config.initial_capital:,.0f}")
        if config.verbose:
            print(f"  止损: -{config.stop_loss*100:.0f}%  "
                  f"止盈: +{config.take_profit*100:.0f}%  "
                  f"持有期: {config.max_hold_days}日")
            print(f"  并发持仓: {config.max_concurrent_positions}  "
                  f"半凯利: {'是' if config.half_kelly else '否'}")
        print(f"{'█' * 60}\n")

    def _apply_strategy_override(self):
        """如果配置中有覆盖参数，应用到 STRATEGY 全局变量"""
        from stock_strategy_v3 import STRATEGY as S
        cfg = self.config
        overrides = {
            "drawdown_min": ("base", "drawdown_min"),
            "drawdown_max": ("base", "drawdown_max"),
            "vol_ratio_min": ("base", "vol_ratio_min"),
            "atr_min": ("elite", "atr_min"),
            "atr_max": ("elite", "atr_max"),
        }
        for attr, (section, key) in overrides.items():
            val = getattr(cfg, attr, None)
            if val is not None:
                old = S[section][key]
                S[section][key] = val
                if self.config.verbose:
                    print(f"  [参数覆盖] STRATEGY.{section}.{key}: {old} → {val}")

    # ════════════════════════════════════════════════
    # 阶段1: 预热
    # ════════════════════════════════════════════════

    def warm_up(self, force_refetch: bool = False):
        """预热 K 线缓存 + 交易日历"""
        self._apply_strategy_override()

        ok = self.cache.warm_up(force=force_refetch)
        if ok == 0:
            print("\n❌ [错误] 无可用 K 线数据，请检查 mootdx 连接")
            return False

        dates = self.cache.build_trading_calendar()
        if not dates:
            print("\n❌ [错误] 无法构建交易日历")
            return False

        # 过滤到回测区间
        sd = self.config.start_date
        ed = self.config.end_date
        self.trading_dates = [d for d in dates if sd <= d <= ed]
        self.date_idx_map = {d: i for i, d in enumerate(self.trading_dates)}

        print(f"\n  回测区间: {self.trading_dates[0]} ~ {self.trading_dates[-1]} "
              f"({len(self.trading_dates)} 交易日)")

        if len(self.trading_dates) < 60:
            print(f"\n⚠️ 回测天数不足 60 日 ({len(self.trading_dates)})，结果可能不可靠")

        # 初始化 SectorScorerProxy（需要 stock_pool）
        self.sector_proxy = SectorScorerProxy(self.cache.get_stock_pool())

        self._warmed_up = True
        return True

    # ════════════════════════════════════════════════
    # 阶段2: 回测主循环
    # ════════════════════════════════════════════════

    def run(self) -> Tuple[dict, PerformanceAnalyzer]:
        """执行主回测循环"""
        if not self._warmed_up:
            if not self.warm_up():
                return {}, None

        print("\n📡 [回测] 开始逐日回放...")
        print(f"  {'日期':<14s} {'温度':>4s} {'信号':>4s} {'持仓':>4s} {'总资产':>12s}")

        last_report_time = time.time()
        report_interval = 5  # 秒

        for i, date in enumerate(self.trading_dates):
            # ── 获取当日行情快照 ──
            snapshot = self.cache.get_pool_snapshot(date)
            if not snapshot:
                if self.config.verbose:
                    print(f"    [跳过] {date} 无行情数据")
                # 仍记录 equity（用上一个值或现金）
                self.engine.record_equity(date, snapshot)
                continue

            # ── ① 执行昨日的待办订单（以今开价） ──
            self.engine.execute_sells(date, snapshot)
            self.engine.execute_buys(date, snapshot)

            # ── ② 代理计算（温度 + 板块） ──
            if self.config.use_market_temp:
                temp = self.temp_proxy.calc_from_snapshot(snapshot)
            else:
                temp = 50  # 固定中性

            if self.config.use_sector_score:
                sector_scores = self.sector_proxy.calc(snapshot, date)
            else:
                sector_scores = {}  # 空 = 都走默认 50 分

            # ── ③ 检查现有持仓（止损/止盈/到期） ──
            self.engine.check_positions(date, snapshot)

            # ── ④ 信号回放 ──
            signals = self.replayer.scan_day(date, snapshot, temp, sector_scores)

            # ── ⑤ 处理信号 → 待买订单（次日执行） ──
            self.engine.process_signals(date, signals, temp, i, self.trading_dates)

            # ── ⑥ 记录当日净资产 ──
            self.engine.record_equity(date, snapshot)

            # ── 进度报告 ──
            now = time.time()
            if now - last_report_time >= report_interval or i == 0 or i == len(self.trading_dates) - 1:
                equity = self.engine.daily_equity[-1][1] if self.engine.daily_equity else 0
                ret = ((equity - self.config.initial_capital) / self.config.initial_capital * 100)
                ret_str = f"+{ret:.1f}%" if ret >= 0 else f"{ret:.1f}%"
                print(f"  [{i+1}/{len(self.trading_dates)}] {date}  "
                      f"{temp:>3d}°  {len(signals):>3d}个  "
                      f"{len(self.engine.positions):>2d}仓  "
                      f"¥{equity:>10.2f} ({ret_str})")
                last_report_time = now

            # 每日节流（同花顺接口不调用，但缓存读取也稍微慢）
            # 这里不需要 sleep

        print(f"\n✅ [回测] 完成！共处理 {len(self.trading_dates)} 个交易日\n")

        # ── 阶段3: 绩效分析 ──
        # 加载基准数据
        benchmark = None
        if self.config.benchmark:
            benchmark = load_benchmark_returns(self.trading_dates)

        self.analyzer = PerformanceAnalyzer(self.engine, benchmark)
        self.results = self.analyzer.analyze()

        elapsed = time.time() - self.start_time
        print(f"⏱️  耗时: {elapsed:.1f} 秒")
        print(f"📊 总交易: {self.results.get('总交易', 0)} 笔")

        return self.results, self.analyzer

    # ════════════════════════════════════════════════
    # 报告输出
    # ════════════════════════════════════════════════

    def print_report(self):
        """打印终端摘要报告"""
        if self.analyzer:
            print(self.analyzer.summary_text())
        else:
            print("无回测结果可输出")

    def save_json_report(self, path: str = None):
        """保存 JSON 报告"""
        if not self.analyzer:
            print("无回测结果可保存")
            return

        if path is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            out_dir = Path(self.config.output_dir)
            out_dir.mkdir(parents=True, exist_ok=True)
            path = str(out_dir / f"backtest_{timestamp}.json")

        self.analyzer.to_json(path)
        print(f"  [报告] JSON 已保存: {path}")


# ═══════════════════════════════════════════════════════════
# 信号日志对照验证
# ═══════════════════════════════════════════════════════════
def validate_against_signal_log(bt_engine: BacktestEngine):
    """
    将回测产生的信号与 cache/signal_log.json 中的历史信号进行对照。
    用于验证回测信号生成是否准确。

    注意: 回测使用 Proxy（模拟温度/板块），signal_log 是生产环境数据，
    二者不可能完全一致。对照主要是为了检查趋势是否合理。
    """
    try:
        log_path = Path(__file__).parent / "cache" / "signal_log.json"
        if not log_path.exists():
            print("  [验证] signal_log.json 不存在，跳过对照验证")
            return

        import json
        with open(log_path) as f:
            signal_log = json.load(f)

        # 找出回测区间内的记录
        sd = bt_engine.config.start_date
        ed = bt_engine.config.end_date
        matching_dates = sorted(
            d for d in signal_log if sd <= d <= ed
        )

        if not matching_dates:
            print("  [验证] 回测区间内无 signal_log 记录")
            return

        total_signals = 0
        total_bt_signals = 0
        matched = 0

        for date in matching_dates:
            log_signals = signal_log[date]
            log_codes = {s["code"] for s in log_signals if isinstance(s, dict)}

            # 查找回测当日信号
            if date in bt_engine.date_idx_map:
                idx = bt_engine.date_idx_map[date]
                if idx < len(bt_engine.trading_dates):
                    snapshot = bt_engine.cache.get_pool_snapshot(date)
                    if snapshot:
                        temp = bt_engine.temp_proxy.calc_from_snapshot(snapshot)
                        sector_scores = bt_engine.sector_proxy.calc(snapshot, date)
                        bt_signals = bt_engine.replayer.scan_day(
                            date, snapshot, temp, sector_scores)
                        bt_codes = {s.code for s in bt_signals}

                        common = log_codes & bt_codes
                        matched += len(common)
                        total_signals += len(log_codes)
                        total_bt_signals += len(bt_codes)

        if total_signals > 0:
            match_rate = matched / max(total_signals, 1) * 100
            print(f"\n  [验证] signal_log 对照: "
                  f"回测∩生产={matched} 生产共{total_signals} "
                  f"回测共{total_bt_signals} 重合率{match_rate:.0f}%")
            if match_rate < 20:
                print("  [验证] ⚠️ 重合率偏低，可能因为 Proxy 温度/板块与实时数据差异较大")
        else:
            print("  [验证] 无对照数据")

    except Exception as e:
        print(f"  [验证] signal_log 对照失败: {e}")


# ═══════════════════════════════════════════════════════════
# CLI 入口
# ═══════════════════════════════════════════════════════════
def main():
    parser = argparse.ArgumentParser(
        description="A股回调低吸策略 v3.0 — 回测引擎",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python3 backtest_engine.py                           # 默认回测(2024起)
  python3 backtest_engine.py --fast                    # 快速(近1年)
  python3 backtest_engine.py -s 2025-01-01 -e 2026-01-01  # 指定年度
  python3 backtest_engine.py --save-json r.json        # 输出JSON
  python3 backtest_engine.py -v                         # 详细
  python3 backtest_engine.py --refetch                  # 重新拉取K线
  python3 backtest_engine.py --no-benchmark             # 跳过基准
  python3 backtest_engine.py --no-temp                  # 关闭温度过滤
        """
    )
    parser.add_argument("-s", "--start", default="2024-01-01",
                        help="回测起始日 (默认: 2024-01-01)")
    parser.add_argument("-e", "--end", default="",
                        help="回测截止日 (默认: 今天)")
    parser.add_argument("--fast", action="store_true",
                        help="快速模式: 仅回测最近1年")
    parser.add_argument("--save-json", type=str, default="",
                        help="保存JSON报告路径")
    parser.add_argument("--no-benchmark", action="store_true",
                        help="跳过基准对比")
    parser.add_argument("--no-temp", action="store_true",
                        help="关闭温度过滤（固定中性温度）")
    parser.add_argument("--no-sector", action="store_true",
                        help="关闭板块评分（全部50分）")
    parser.add_argument("--refetch", action="store_true",
                        help="重新拉取K线（忽略缓存）")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="详细输出")
    parser.add_argument("--stop-loss", type=float, default=0.08,
                        help="止损比例 (默认: 0.08)")
    parser.add_argument("--take-profit", type=float, default=0.15,
                        help="止盈比例 (默认: 0.15, 0=禁用)")
    parser.add_argument("--max-hold", type=int, default=20,
                        help="最长持有天数 (默认: 20)")
    parser.add_argument("--max-positions", type=int, default=10,
                        help="最大并发持仓 (默认: 10)")
    parser.add_argument("--capital", type=float, default=1_000_000,
                        help="初始资金 (默认: 1000000)")

    args = parser.parse_args()

    # ── 构建配置 ──
    end_date = args.end or datetime.now().strftime("%Y-%m-%d")

    if args.fast:
        # 快速模式：回测最近 1 年
        start_date = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")
    else:
        start_date = args.start

    config = BacktestConfig(
        start_date=start_date,
        end_date=end_date,
        initial_capital=args.capital,
        stop_loss=args.stop_loss,
        take_profit=args.take_profit if args.take_profit > 0 else 0.0,
        max_hold_days=args.max_hold,
        max_concurrent_positions=args.max_positions,
        use_market_temp=not args.no_temp,
        use_sector_score=not args.no_sector,
        benchmark=not args.no_benchmark,
        verbose=args.verbose,
        save_json=bool(args.save_json),
    )

    # ── 运行回测 ──
    engine = BacktestEngine(config)

    print("📡 [初始化] 预热数据缓存...")
    warmed = engine.warm_up(force_refetch=args.refetch)
    if not warmed:
        return 1

    results, analyzer = engine.run()
    if not results:
        print("❌ 回测失败")
        return 1

    # ── 输出报告 ──
    engine.print_report()

    # ── 信号日志对照验证 ──
    if not args.no_benchmark:
        validate_against_signal_log(engine)

    # ── 保存JSON ──
    if args.save_json:
        engine.save_json_report(args.save_json)
    elif config.save_json:
        engine.save_json_report()

    # 交易明细摘要
    closed = engine.engine.closed_trades
    if closed:
        print(f"\n  ── 📋 近期交易明细 (最近3笔) ──")
        for t in closed[-3:]:
            reason_emoji = {"stop_loss": "🔴", "take_profit": "🟢", "expiry": "🟡", "trailing_stop": "🟢"}
            emoji = reason_emoji.get(t.exit_reason, "⚪")
            pnl_str = f"+{t.pnl_pct:.1f}%" if t.pnl_pct >= 0 else f"{t.pnl_pct:.1f}%"
            print(f"    {emoji} {t.name}({t.code}) {t.tier} "
                  f"¥{t.entry_price:.2f}→¥{t.exit_price:.2f} "
                  f"{pnl_str} | {t.hold_days}日 | {t.exit_reason}")

    print(f"\n✅ 回测完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
