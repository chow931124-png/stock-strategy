#!/usr/bin/env python3
"""
backtest_analytics.py — 回测绩效分析与报告生成

核心功能:
1. PerformanceAnalyzer — 计算所有绩效指标（收益/风险/分层/基准对比）
2. 报告输出 — 终端摘要 / JSON 导出
"""

import json
import math
from collections import Counter, defaultdict
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from backtest_simulator import Trade, TradeEngine


# ═══════════════════════════════════════════════════════════
# PerformanceAnalyzer — 绩效分析器
# ═══════════════════════════════════════════════════════════
class PerformanceAnalyzer:
    """
    从 TradeEngine 的运行结果计算所有绩效指标。

    指标覆盖:
    - 收益: 总收益、CAGR、月度收益
    - 风险: 最大回撤、波动率、VaR
    - 风险调整: 夏普、卡玛、索提诺
    - 交易统计: 胜率、盈亏比、连赢/连亏
    - 分层: 按 tier / sector / 市场温度
    - 基准对比: 沪深300
    """

    def __init__(self, engine: TradeEngine, benchmark_returns: List[float] = None):
        self.engine = engine
        self.benchmark_returns = benchmark_returns  # 与 equity 同步的日收益率
        self._results = {}

    def analyze(self) -> dict:
        """全量分析，返回所有指标"""
        results = {}

        # 基础数据
        equity = self.engine.daily_equity
        if not equity:
            return {"error": "无净值数据"}

        dates = [e[0] for e in equity]
        values = np.array([e[1] for e in equity])
        init_capital = self.engine.initial_capital

        # ── 日收益率序列 ──
        daily_returns = np.diff(values) / values[:-1]
        # 补充第一个收益率为0
        daily_returns = np.concatenate([[0.0], daily_returns])

        # ── 收益指标 ──
        total_return = float((values[-1] - init_capital) / init_capital)
        trading_days = len(dates)
        years = trading_days / 252
        cagr = float((1 + total_return) ** (1 / years) - 1) if years > 0 else 0

        results["总收益率"] = round(total_return * 100, 2)
        results["年化收益率"] = round(cagr * 100, 2)
        results["回测天数"] = trading_days
        results["回测年数"] = round(years, 2)

        # ── 风险指标 ──
        # 最大回撤
        peak = np.maximum.accumulate(values)
        drawdowns = (values - peak) / peak
        max_dd = float(np.min(drawdowns))
        max_dd_pct = abs(max_dd) * 100
        results["最大回撤"] = round(max_dd_pct, 2)

        # 波动率
        daily_vol = float(np.std(daily_returns))
        annual_vol = float(daily_vol * np.sqrt(252))
        results["日波动率"] = round(daily_vol * 100, 3)
        results["年化波动率"] = round(annual_vol * 100, 2)

        # VaR 95%
        var_95 = float(np.percentile(daily_returns, 5))
        results["VaR_95"] = round(var_95 * 100, 2)

        # 下行波动率
        downside = daily_returns[daily_returns < 0]
        downside_vol = float(np.std(downside)) * np.sqrt(252) if len(downside) > 0 else 0
        results["下行波动率"] = round(downside_vol * 100, 2)

        # ── 风险调整指标 ──
        rf = 0.02  # 无风险利率 2%
        excess = daily_returns - rf / 252
        if daily_vol > 0:
            sharpe = float(np.mean(excess) / daily_vol * np.sqrt(252))
        else:
            sharpe = 0.0
        results["夏普比率"] = round(sharpe, 2)

        calmar = cagr / abs(max_dd) if max_dd != 0 else 0
        results["卡玛比率"] = round(calmar, 2)

        if downside_vol > 0:
            sortino = float((cagr - rf) / downside_vol)
        else:
            sortino = 0.0
        results["索提诺比率"] = round(sortino, 2)

        # ── 交易统计 ──
        trades = self.engine.closed_trades
        if trades:
            total_trades = len(trades)
            wins = [t for t in trades if t.pnl_pct > 0]
            losses = [t for t in trades if t.pnl_pct <= 0]
            win_count = len(wins)
            loss_count = len(losses)
            win_rate = win_count / total_trades if total_trades > 0 else 0

            avg_win = float(np.mean([t.pnl_pct for t in wins])) if wins else 0
            avg_loss = float(np.mean([t.pnl_pct for t in losses])) if losses else 0
            total_profit = sum(t.pnl_amount for t in wins) if wins else 0
            total_loss = abs(sum(t.pnl_amount for t in losses)) if losses else 0
            profit_factor = total_profit / total_loss if total_loss > 0 else float('inf')

            avg_hold = float(np.mean([t.hold_days for t in trades]))
            max_hold = max(t.hold_days for t in trades)

            # 最大连赢/连亏
            max_cons_wins = 0
            max_cons_losses = 0
            cur_wins = 0
            cur_losses = 0
            for t in sorted(trades, key=lambda x: x.entry_date):
                if t.pnl_pct > 0:
                    cur_wins += 1
                    cur_losses = 0
                    max_cons_wins = max(max_cons_wins, cur_wins)
                else:
                    cur_losses += 1
                    cur_wins = 0
                    max_cons_losses = max(max_cons_losses, cur_losses)

            results["总交易"] = total_trades
            results["盈利次数"] = win_count
            results["亏损次数"] = loss_count
            results["胜率"] = round(win_rate * 100, 1)
            results["平均盈利"] = round(avg_win, 2)
            results["平均亏损"] = round(avg_loss, 2)
            results["盈亏比"] = round(profit_factor, 2)
            results["平均持有天数"] = round(avg_hold, 1)
            results["最长持有天数"] = max_hold
            results["最大连赢"] = max_cons_wins
            results["最大连亏"] = max_cons_losses
        else:
            results.update({
                "总交易": 0, "盈利次数": 0, "亏损次数": 0,
                "胜率": 0, "盈亏比": 0, "平均持有天数": 0,
            })

        # ── 分层统计 ──
        results["分层统计"] = self._breakdown_by_tier(trades)
        results["板块统计"] = self._breakdown_by_sector(trades)
        results["温度区间统计"] = self._breakdown_by_temp(trades)

        # ── 月度收益 ──
        results["月度收益"] = self._monthly_returns(dates, values)

        # ── 基准对比 ──
        if self.benchmark_returns is not None and len(self.benchmark_returns) == len(daily_returns):
            bench_total_return = float((np.prod(1 + np.array(self.benchmark_returns)) - 1))
            bench_cagr = float(
                (1 + bench_total_return) ** (1 / years) - 1) if years > 0 else 0
            results["基准总收益率"] = round(bench_total_return * 100, 2)
            results["基准年化收益率"] = round(bench_cagr * 100, 2)

            # 超额收益 (Alpha)
            results["超额收益"] = round((total_return - bench_total_return) * 100, 2)

            # Beta
            bench_returns_arr = np.array(self.benchmark_returns)
            cov = np.cov(daily_returns, bench_returns_arr)[0, 1]
            var_bench = np.var(bench_returns_arr)
            beta = cov / var_bench if var_bench > 0 else 0
            results["Beta"] = round(float(beta), 2)

            # Alpha (年化)
            alpha = cagr - rf - beta * (bench_cagr - rf)
            results["Alpha"] = round(float(alpha) * 100, 2)

            # 信息比率
            tracking_error = np.std(daily_returns - bench_returns_arr) * np.sqrt(252)
                    # 信息比率
            if tracking_error > 0:
                info_ratio = (cagr - bench_cagr) / tracking_error
                results["信息比率"] = round(float(info_ratio), 2)
            else:
                results["信息比率"] = 0.0

        self._results = results
        return results

    # ──────────────── 分层统计 ────────────────

    def _breakdown_by_tier(self, trades: List[Trade]) -> Dict:
        """按信号层级分解交易表现"""
        tiers = defaultdict(list)
        for t in trades:
            tiers[t.tier].append(t)

        result = {}
        tier_labels = {"💎 精选层", "🥈 增强层", "🥉 普通层"}
        for tier in tier_labels:
            ts = tiers.get(tier, [])
            if ts:
                wins = [t for t in ts if t.pnl_pct > 0]
                result[tier] = {
                    "交易次数": len(ts),
                    "胜率": round(len(wins) / len(ts) * 100, 1),
                    "平均收益": round(float(np.mean([t.pnl_pct for t in ts])), 2),
                    "平均盈利": round(float(np.mean([t.pnl_pct for t in wins])), 2) if wins else 0,
                    "平均亏损": round(float(np.mean([t.pnl_pct for t in ts if t.pnl_pct <= 0])), 2),
                    "总盈亏": round(sum(t.pnl_amount for t in ts), 2),
                    "盈亏比": round(
                        abs(sum(t.pnl_amount for t in wins) /
                            max(abs(sum(t.pnl_amount for t in ts if t.pnl_pct <= 0)), 1)),
                        2) if wins else 0,
                }
            else:
                result[tier] = {"交易次数": 0, "胜率": 0, "平均收益": 0}
        return result

    def _breakdown_by_sector(self, trades: List[Trade]) -> Dict:
        """按板块分解交易表现"""
        sectors = defaultdict(list)
        for t in trades:
            sectors[t.sector].append(t)

        result = {}
        for sector, ts in sorted(sectors.items(), key=lambda x: -len(x[1])):
            wins = [t for t in ts if t.pnl_pct > 0]
            result[sector] = {
                "交易次数": len(ts),
                "胜率": round(len(wins) / len(ts) * 100, 1),
                "平均收益": round(float(np.mean([t.pnl_pct for t in ts])), 2),
                "总盈亏": round(sum(t.pnl_amount for t in ts), 2),
            }
        return result

    def _breakdown_by_temp(self, trades: List[Trade]) -> Dict:
        """按入场时市场温度区间分解"""
        zones = {
            "亢奋区 (70+)": (70, 100),
            "正常区 (55-70)": (55, 70),
            "偏冷区 (40-55)": (40, 55),
            "低迷区 (25-40)": (25, 40),
            "冰点区 (<25)": (0, 25),
        }
        result = {}
        for zone_name, (lo, hi) in zones.items():
            ts = [t for t in trades if lo <= (t.entry_temp if hasattr(t, 'entry_temp') else 50) < hi]
            if ts:
                wins = [t for t in ts if t.pnl_pct > 0]
                result[zone_name] = {
                    "交易次数": len(ts),
                    "胜率": round(len(wins) / len(ts) * 100, 1),
                    "平均收益": round(float(np.mean([t.pnl_pct for t in ts])), 2),
                }
        return result

    def _monthly_returns(self, dates: List[str], values: np.ndarray) -> Dict:
        """按自然月计算收益率"""
        if len(dates) < 2:
            return {}
        df = pd.DataFrame({"date": pd.to_datetime(dates), "value": values})
        df = df.set_index("date")
        monthly = df.resample("ME").last()
        monthly_ret = monthly.pct_change().dropna()
        result = {}
        for idx, row in monthly_ret.iterrows():
            result[idx.strftime("%Y-%m")] = round(float(row["value"]) * 100, 2)
        return result

    # ──────────────── 报告输出 ────────────────

    def summary_text(self) -> str:
        """生成终端摘要报告"""
        r = self._results
        if not r or "error" in r:
            return "无回测结果"

        lines = []
        lines.append("")
        lines.append("╔══════════════════════════════════════════════════════════════╗")
        lines.append("║         A股回调低吸策略 v3.0 — 回测报告                    ║")
        lines.append("╚══════════════════════════════════════════════════════════════╝")
        lines.append("")

        # 基本参数
        lines.append(f"  回测期间: ~{r.get('回测天数', 0)} 交易日 ({r.get('回测年数', 0):.1f}年)")
        lines.append(f"  初始资金: ¥{self.engine.initial_capital:,.0f}")
        lines.append("")

        # 核心绩效
        lines.append("  ── 📊 核心绩效 ──")
        tr = r.get("总收益率", 0)
        tr_str = f"+{tr}%" if tr >= 0 else f"{tr}%"
        ar = r.get("年化收益率", 0)
        ar_str = f"+{ar}%" if ar >= 0 else f"{ar}%"
        lines.append(f"    总收益率:     {tr_str:>10s}")
        lines.append(f"    年化收益率:   {ar_str:>10s}")
        lines.append(f"    最大回撤:     -{r.get('最大回撤', 0):.2f}%")
        lines.append(f"    夏普比率:     {r.get('夏普比率', 0):>8.2f}")
        lines.append(f"    卡玛比率:     {r.get('卡玛比率', 0):>8.2f}")
        lines.append(f"    索提诺比率:   {r.get('索提诺比率', 0):>8.2f}")
        lines.append(f"    年化波动率:   {r.get('年化波动率', 0):.2f}%")
        lines.append("")

        # 交易统计
        lines.append("  ── 📈 交易统计 ──")
        lines.append(f"    总交易:       {r.get('总交易', 0):>6d} 笔")
        lines.append(f"    胜率:         {r.get('胜率', 0):>6.1f}%")
        lines.append(f"    盈亏比:       {r.get('盈亏比', 0):>8.2f}")
        lines.append(f"    平均盈利:     +{r.get('平均盈利', 0):.2f}%")
        lines.append(f"    平均亏损:     {r.get('平均亏损', 0):.2f}%")
        lines.append(f"    平均持有:     {r.get('平均持有天数', 0):.1f} 日")
        lines.append(f"    最大连赢:     {r.get('最大连赢', 0):>3d} 次")
        lines.append(f"    最大连亏:     {r.get('最大连亏', 0):>3d} 次")
        lines.append("")

        # 分层
        tier_stats = r.get("分层统计", {})
        if any(v.get("交易次数", 0) > 0 for v in tier_stats.values()):
            lines.append("  ── 🎯 分层表现 ──")
            for tier_label in ["💎 精选层", "🥈 增强层", "🥉 普通层"]:
                ts = tier_stats.get(tier_label, {})
                n = ts.get("交易次数", 0)
                if n > 0:
                    wr = ts.get("胜率", 0)
                    avg_r = ts.get("平均收益", 0)
                    avg_r_str = f"+{avg_r}%" if avg_r >= 0 else f"{avg_r}%"
                    lines.append(f"    {tier_label}: {n}次  胜率{wr:.1f}%  均收益{avg_r_str}")
            lines.append("")

        # 板块统计 Top/Bottom
        sector_stats = r.get("板块统计", {})
        if sector_stats:
            sorted_sec = sorted(sector_stats.items(),
                                key=lambda x: x[1]["交易次数"], reverse=True)
            top_sec = sorted_sec[:5]
            bottom_sec = [s for s in sorted_sec if s[1]["交易次数"] >= 3][-3:]
            lines.append("  ── 🏆 最佳板块 (TOP 5) ──")
            for name, st in top_sec:
                lines.append(f"    {name}: {st['交易次数']}次  胜率{st['胜率']:.1f}%  均收益+{st['平均收益']:.2f}%")
            if bottom_sec and bottom_sec != top_sec:
                lines.append("  ── 📉 最差板块 (BOTTOM 3) ──")
                for name, st in bottom_sec[:3]:
                    lines.append(f"    {name}: {st['交易次数']}次  胜率{st['胜率']:.1f}%  均收益{st['平均收益']:.2f}%")
            lines.append("")

        # 温度区间
        temp_stats = r.get("温度区间统计", {})
        if temp_stats:
            lines.append("  ── 🌡️ 按温度区间 ──")
            for zone, st in temp_stats.items():
                if st["交易次数"] >= 3:
                    lines.append(f"    {zone}: {st['交易次数']}次  胜率{st['胜率']:.1f}%  均收益{st['平均收益']:+.2f}%")
            lines.append("")

        # 基准对比
        if "基准总收益率" in r:
            lines.append("  ── 📊 基准对比 (沪深300) ──")
            btr = r.get("基准总收益率", 0)
            btr_str = f"+{btr}%" if btr >= 0 else f"{btr}%"
            alpha = r.get("超额收益", 0)
            alpha_str = f"+{alpha}%" if alpha >= 0 else f"{alpha}%"
            lines.append(f"    策略总收益:  {tr_str:>10s}  基准: {btr_str:>10s}")
            lines.append(f"    策略年化:    {ar_str:>10s}")
            lines.append(f"    超额收益:    {alpha_str:>10s}")
            lines.append(f"    Beta:        {r.get('Beta', 0):>8.2f}")
            lines.append(f"    信息比率:    {r.get('信息比率', 0):>8.2f}")
            lines.append("")

        # 月度收益
        monthly = r.get("月度收益", {})
        if monthly:
            lines.append("  ── 📅 月度收益 ──")
            # 按年分组展示
            by_year = defaultdict(list)
            for ym, ret in sorted(monthly.items()):
                year = ym[:4]
                by_year[year].append((ym[5:], ret))
            for year in sorted(by_year.keys()):
                parts = []
                for mon, ret in by_year[year]:
                    if ret >= 0:
                        parts.append(f"{mon}:+{ret:.1f}%")
                    else:
                        parts.append(f"{mon}:{ret:.1f}%")
                # 年累计
                year_rets = [r for _, r in by_year[year]]
                year_total = sum(year_rets)
                year_str = f"+{year_total:.1f}%" if year_total >= 0 else f"{year_total:.1f}%"
                lines.append(f"    {year}: {' | '.join(parts)} | 累计{year_str}")
                # 换行太长的折行
            lines.append("")

        # 尾部
        lines.append("  ── ⚠️ 重要说明 ──")
        lines.append("    1. 回测使用历史代理模拟市场温度和板块评分")
        lines.append("    2. 股票池仅包含当前存活的股票（幸存者偏差）")
        lines.append("    3. 未考虑停牌、分红、配股等事件")
        lines.append("    4. 假设次日开盘价均可成交（实际可能有滑点）")
        lines.append("    5. 仅供参考，不构成投资建议")
        lines.append("")

        return "\n".join(lines)

    def to_json(self, path: str = None) -> dict:
        """导出完整回测结果为 JSON 结构（可写入文件）"""
        r = self._results.copy()

        # 添加交易明细（简要）
        r["交易明细"] = []
        for t in self.engine.closed_trades:
            r["交易明细"].append({
                "code": t.code, "name": t.name,
                "sector": t.sector, "tier": t.tier,
                "entry_date": t.entry_date, "exit_date": t.exit_date,
                "entry_price": t.entry_price, "exit_price": t.exit_price,
                "quantity": t.quantity,
                "pnl_pct": t.pnl_pct, "pnl_amount": t.pnl_amount,
                "hold_days": t.hold_days, "exit_reason": t.exit_reason,
            })

        # 净值曲线
        r["净值曲线"] = [
            {"date": d, "equity": e}
            for d, e in self.engine.daily_equity
        ]

        # 引擎统计
        r["引擎统计"] = self.engine.summary()

        if path:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(r, f, ensure_ascii=False, indent=2)
            print(f"  [报告] JSON 报告已保存: {path}")

        return r


# ═══════════════════════════════════════════════════════════
# 辅助工具：加载沪深300历史数据
# ═══════════════════════════════════════════════════════════
def load_benchmark_returns(dates: List[str]) -> Optional[List[float]]:
    """
    获取沪深300在指定日期区间的日收益率。
    用于基准对比。

    使用东方财富HTTP接口获取沪深300历史K线。
    """
    try:
        # 用 mootdx TCP 获取沪深300日K线（不过HTTP代理）
        from mootdx.quotes import Quotes
        client = Quotes.factory(market='std')
        df = client.index(symbol='000300', category=4)
        if df is None or len(df) < 100:
            print("  [基准] 沪深300 mootdx无数据，跳过基准对比")
            return None

        # 构建日期->收盘价映射
        df['date_str'] = pd.to_datetime(df['datetime']).dt.strftime('%Y-%m-%d')
        closing = dict(zip(df['date_str'], df['close']))
        if len(closing) < 100:
            print(f"  [基准] 沪深300 有效数据不足({len(closing)}天)，跳过")
            return None

        prev_close = None
        returns = []
        valid_count = 0
        for d in dates:
            if d in closing:
                curr = closing[d]
                if prev_close is not None and prev_close > 0:
                    ret = (curr - prev_close) / prev_close
                    returns.append(ret)
                    valid_count += 1
                else:
                    returns.append(0.0)
                prev_close = curr
            else:
                returns.append(0.0)

        print(f"  [基准] 沪深300 数据加载成功，{valid_count}/{len(dates)} 日有数据")
        return returns
    except Exception as e:
        print(f"  [基准] 加载失败: {e}")
        return None


if __name__ == "__main__":
    print("=" * 50)
    print("backtest_analytics.py — 单元自检")
    print("=" * 50)
    print("PerformanceAnalyzer 类就绪")
    print("load_benchmark_returns 工具就绪")
