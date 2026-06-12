#!/usr/bin/env python3
"""
backtest_simulator.py — 回测交易模拟器

为 stock_strategy_v3.py 提供：
1. SignalReplayer — 在历史 K 线上逐日回放三层信号
2. TradeEngine    — A 股 T+1 交易模拟（持仓/订单/净值）
3. Signal, Position, Trade, PendingOrder — 核心数据结构

依赖 backtest_proxy.py（历史代理）和 stock_strategy_v3.py（评分逻辑）。
"""

import sys, math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

# ── 复用原策略评分逻辑 ──
from stock_strategy_v3 import (
    STRATEGY, BUILTIN_STOCKS, SECTOR_PREFERENCE,
    StockScorer, AutoSectorMapper, CACHE_DIR,
)

# ── 复用代理模块 ──
from backtest_proxy import (
    KlineCache, MarketThermometerProxy, SectorScorerProxy,
    filter_stock_pool, get_stock_name,
)

# 防止 AutoSectorMapper 的 sleep 在回测中拖慢
AutoSectorMapper.BOARD_TO_SECTOR  # 确保类已加载


# ═══════════════════════════════════════════════════════════
# 数据结构
# ═══════════════════════════════════════════════════════════

@dataclass
class Signal:
    """回测中产生的信号（与原策略 results[] 格式一致）"""
    code: str
    name: str
    sector: str
    tier: str
    tier_score: int
    sector_score: int
    composite: int
    price: float
    kelly_pct: float
    surge_score: dict = field(default_factory=lambda: {"score": 0, "class": "", "advice": ""})
    ambush_score: int = 0
    short_score: int = 0
    short_reasons: str = ""
    verify: dict = field(default_factory=lambda: {"level": "", "lights": "", "details": "", "green": 0})
    drawdown: float = 0.0
    vol_ratio: float = 0.0
    atr_ratio: float = 0.0
    atr_stop_pct: float = 0.08  # ATR动态止损，默认-8%


@dataclass
class Position:
    """开仓持仓记录"""
    code: str
    name: str
    sector: str
    tier: str
    composite: int
    entry_date: str
    entry_price: float
    quantity: int
    kelly_pct: float
    stop_loss_price: float
    take_profit_price: float = 0.0
    hold_days: int = 0
    entry_temp: int = 50  # 入场时的市场温度
    trailing_high: float = 0.0   # 移动止盈：持仓期间最高价
    trailing_active: bool = False  # 是否已激活移动止盈（涨超+10%后激活）


@dataclass
class Trade:
    """已平仓交易记录"""
    code: str
    name: str
    sector: str
    tier: str
    composite: int
    entry_date: str
    exit_date: str
    entry_price: float
    exit_price: float
    quantity: int
    pnl_pct: float
    pnl_amount: float  # 扣除费用后的净盈亏
    hold_days: int
    exit_reason: str  # 'stop_loss' | 'take_profit' | 'expiry'
    kelly_pct: float
    entry_temp: int = 50  # 入场时的市场温度


@dataclass
class PendingOrder:
    """待执行订单（跨交易日）"""
    code: str
    order_type: str  # 'buy' | 'sell'
    # 卖单
    reason: str = None
    # 买单
    name: str = ""
    sector: str = ""
    tier: str = ""
    composite: int = 0
    quantity: int = 0
    kelly_pct: float = 0
    entry_price_est: float = 0  # 信号产生时的收盘价（用于止损计算）
    stop_loss_price: float = 0
    take_profit_price: float = 0
    surge_score: dict = None
    ambush_score: int = 0
    short_score: int = 0
    short_reasons: str = ""
    verify: dict = None
    drawdown: float = 0.0
    vol_ratio: float = 0.0
    atr_ratio: float = 0.0
    atr_stop_pct: float = 0.08  # ATR动态止损
    entry_temp: int = 50


# ═══════════════════════════════════════════════════════════
# SignalReplayer — 信号回放器
# ═══════════════════════════════════════════════════════════
class SignalReplayer:
    """
    在历史某一天的行情快照上运行策略扫描，生成信号列表。

    与原策略 main() 的 "步骤3：个股扫描" 逻辑保持一致，
    但使用 Proxy 而非实时数据。
    """

    def __init__(self, stock_pool: List[tuple] = None):
        self.stock_pool = stock_pool or BUILTIN_STOCKS
        self.scorer = StockScorer()
        self.sector_proxy = SectorScorerProxy(self.stock_pool)

    def scan_day(self, date: str, snapshot: Dict[str, pd.Series],
                 temperature: int, sector_scores: Dict[str, int] = None) -> List[Signal]:
        """
        在指定日期的行情快照上运行策略扫描。

        参数
        ----
        date        : 交易日 YYYY-MM-DD
        snapshot    : get_pool_snapshot(date) 的输出
        temperature : 当日市场温度
        sector_scores : 当日板块评分（None 则自动计算）

        返回
        ----
        signals : 按综合分降序排列的信号列表
        """
        signals = []

        # 按温度过滤股票池
        pool = filter_stock_pool(self.stock_pool, temperature)

        # 如果没传板块评分，自动计算
        if sector_scores is None:
            sector_scores = self.sector_proxy.calc(snapshot, date)

        for code, name, fixed_sector in pool:
            # 跳过 300/301/688
            if code.startswith(('300', '301', '688')):
                continue

            row = snapshot.get(code)
            if row is None:
                continue

            # 流动性过滤（与 v3 一致：日均成交额 > 3000 万）
            amount = row.get('amount', 0)
            if pd.notna(amount) and amount > 0 and amount < 3000_0000:
                continue

            # 涨跌幅过滤：当日跌超 5% 不参与（check_base 已有此逻辑，
            # 但明确跳过大跌日可以提前止损）
            chg = row.get('change_pct', None)
            if pd.notna(chg) and chg < -9.5:  # 跌停日跳过
                continue

            # 动态板块映射
            dyn_sector, blocks, src = AutoSectorMapper.get_sector(code, name)
            sector = dyn_sector if dyn_sector != "其他" else fixed_sector

            sec_score = sector_scores.get(sector, 50)

            # 核心评分
            result = self.scorer.score(row, sec_score)
            if result is None:
                continue

            # 温度过滤（与原策略 main() 保持一致）
            if temperature < 30 and result["composite"] < 70:
                continue
            if temperature < 45 and result["tier"] == "🥉 普通层" and result["composite"] < 50:
                continue

            # 组装 Signal
            signal = Signal(
                code=code,
                name=name,
                sector=sector,
                tier=result["tier"],
                tier_score=result["tier_score"],
                sector_score=sec_score,
                composite=result["composite"],
                price=row["close"],
                kelly_pct=result["kelly_pct"],
                surge_score=result.get("surge_score", {"score": 0}),
                ambush_score=result.get("ambush_score", 0),
                short_score=result.get("short_term", {}).get("short_score", 0),
                short_reasons=result.get("short_term", {}).get("short_reasons", ""),
                verify=result.get("verify", {}),
                drawdown=row.get("drawdown", 0),
                vol_ratio=row.get("vol_ratio_20", 0),
                atr_ratio=row.get("atr_ratio", 0),
                atr_stop_pct=result.get("atr_stop_pct", 0.08),
            )
            signals.append(signal)

        # 按综合分降序
        signals.sort(key=lambda s: -s.composite)
        return signals


# ═══════════════════════════════════════════════════════════
# TradeEngine — 交易引擎
# ═══════════════════════════════════════════════════════════
class TradeEngine:
    """
    A 股 T+1 交易模拟引擎。

    规则:
    - T+1：当日买入，最早次日卖出
    - 买入价 = 信号日次日开盘价
    - 卖出价 = 触发日次日开盘价
    - 止损：持仓期间收盘价 < 买入价 × 0.92 → 次日开盘卖出
    - 止盈：持仓期间收盘价 > 买入价 × 1.15 → 次日开盘卖出
    - 到期：持有满 max_hold_days 交易日 → 次日开盘卖出
    - 成交检查：开盘涨停/跌停时取消买卖
    - 佣金万2.5 + 印花税千1（卖出）
    - 最小交易单位 100 股
    """

    def __init__(self, config):
        self.config = config
        self.cash = float(config.initial_capital)
        self.initial_capital = float(config.initial_capital)
        self.positions: Dict[str, Position] = {}
        self.closed_trades: List[Trade] = []
        self._pending_buys: List[PendingOrder] = []
        self._pending_sells: List[PendingOrder] = []
        self.daily_equity: List[Tuple[str, float]] = []

        # 代理
        self.temp_proxy = MarketThermometerProxy()

        # 统计
        self.total_buy_value = 0.0
        self.total_sell_value = 0.0

        # 用于诊断的计数器
        self.stats = {"buy_orders": 0, "sell_orders": 0,
                      "cancelled_buys": 0, "cancelled_sells": 0}

    # ──────────────── 订单执行 ────────────────

    def execute_sells(self, date: str, snapshot: Dict[str, pd.Series]):
        """
        执行待卖出订单：以 date 的开盘价卖出。
        必须在 execute_buys 之前调用（先回笼资金）。
        """
        if not self._pending_sells:
            return

        to_remove = []
        for order in self._pending_sells:
            pos = self.positions.get(order.code)
            if pos is None:
                to_remove.append(order)
                continue

            # 取开盘价
            row = snapshot.get(order.code)
            if row is None:
                continue  # 停牌，延后处理
            exit_price = float(row.get('open', row.get('close', 0)))
            if exit_price <= 0:
                continue

            # 跌停检查（不能卖出）
            prev_close = pos.entry_price  # 近似
            if exit_price <= prev_close * 0.9 and order.reason != 'expiry':
                # 跌停可能卖不出，但 expiry 强制出
                # 这里简化处理：继续持有，下次再试
                self.stats["cancelled_sells"] += 1
                continue

            # 计算费用
            fee = self._calc_sell_fee(exit_price, pos.quantity)

            # 平仓
            pnl_amount = (exit_price - pos.entry_price) * pos.quantity - fee
            pnl_pct = ((exit_price - pos.entry_price) / pos.entry_price * 100
                       if pos.entry_price > 0 else 0)

            trade = Trade(
                code=pos.code, name=pos.name, sector=pos.sector,
                tier=pos.tier, composite=pos.composite,
                entry_date=pos.entry_date, exit_date=date,
                entry_price=pos.entry_price, exit_price=exit_price,
                quantity=pos.quantity, pnl_pct=round(pnl_pct, 2),
                pnl_amount=round(pnl_amount, 2),
                hold_days=pos.hold_days,
                exit_reason=order.reason or "unknown",
                kelly_pct=pos.kelly_pct,
                entry_temp=pos.entry_temp,
            )
            self.closed_trades.append(trade)

            # 现金回笼
            self.cash += exit_price * pos.quantity - fee
            self.total_sell_value += exit_price * pos.quantity
            self.stats["sell_orders"] += 1

            # 移除持仓
            del self.positions[order.code]
            to_remove.append(order)

        for o in to_remove:
            self._pending_sells.remove(o)

    def execute_buys(self, date: str, snapshot: Dict[str, pd.Series]):
        """
        执行待买入订单：以 date 的开盘价买入。
        """
        if not self._pending_buys:
            return

        to_remove = []
        for order in self._pending_buys:
            # 检查是否已有持仓（可能是其他信号同期买入的）
            if order.code in self.positions:
                to_remove.append(order)
                continue

            # 取开盘价
            row = snapshot.get(order.code)
            if row is None:
                continue  # 停牌，延后
            buy_price = float(row.get('open', row.get('close', 0)))
            if buy_price <= 0:
                continue

            # 涨停检查（不能买入）
            prev_close = row.get('pre_close', None)
            if prev_close is not None and prev_close > 0:
                if buy_price >= prev_close * 1.095:
                    self.stats["cancelled_buys"] += 1
                    continue

            # 实际可买数量（100 股整数倍）
            actual_qty = (order.quantity // 100) * 100
            if actual_qty <= 0:
                to_remove.append(order)
                continue

            # 检查现金是否足够
            cost = buy_price * actual_qty
            fee = self._calc_buy_fee(buy_price, actual_qty)
            total_cost = cost + fee

            if total_cost > self.cash:
                # 资金不足，按现金比例缩仓
                actual_qty = int((self.cash * 0.95) // (buy_price * 100)) * 100
                if actual_qty <= 0:
                    to_remove.append(order)
                    continue
                cost = buy_price * actual_qty
                fee = self._calc_buy_fee(buy_price, actual_qty)
                total_cost = cost + fee

            if order.quantity != actual_qty:
                # 缩仓后调整止损价
                pass

            # 扣现金
            self.cash -= total_cost
            self.total_buy_value += cost

            # 开仓
            pos = Position(
                code=order.code, name=order.name, sector=order.sector,
                tier=order.tier, composite=order.composite,
                entry_date=date, entry_price=buy_price,
                quantity=actual_qty, kelly_pct=order.kelly_pct,
                stop_loss_price=buy_price * (1 - order.atr_stop_pct),
                take_profit_price=buy_price * (1 + self.config.take_profit)
                if self.config.take_profit > 0 else 0,
                entry_temp=order.entry_temp,
            )
            self.positions[order.code] = pos
            self.stats["buy_orders"] += 1
            to_remove.append(order)

        for o in to_remove:
            self._pending_buys.remove(o)

    # ──────────────── 信号处理 ────────────────

    def process_signals(self, date: str, signals: List[Signal],
                        temperature: int, date_idx: int,
                        trading_dates: List[str]):
        """
        处理当日信号，生成待买入订单（次日开盘执行）。
        """
        if not signals:
            return

        temp_limit = self.temp_proxy.get_position_limit(temperature)
        max_pos = self.config.max_concurrent_positions
        current_pos_count = len(self.positions)
        pending_buy_count = len(self._pending_buys)

        for sig in signals:
            # 并发上限
            if current_pos_count + pending_buy_count >= max_pos:
                break
            # 是否已在持仓或待买中
            if sig.code in self.positions:
                continue
            if any(p.code == sig.code for p in self._pending_buys):
                continue

            # 凯利仓位
            kelly_ratio = sig.kelly_pct * temp_limit
            target_amount = self.cash * kelly_ratio

            # 计算数量（次日开盘价未知，用信号日收盘估算）
            est_price = sig.price
            quantity = int(target_amount // (est_price * 100)) * 100
            if quantity <= 0:
                continue

            order = PendingOrder(
                code=sig.code, order_type='buy',
                name=sig.name, sector=sig.sector,
                tier=sig.tier, composite=sig.composite,
                quantity=quantity, kelly_pct=sig.kelly_pct,
                entry_price_est=est_price,
                stop_loss_price=est_price * (1 - sig.atr_stop_pct),
                take_profit_price=est_price * (1 + self.config.take_profit)
                if self.config.take_profit > 0 else 0,
                surge_score=sig.surge_score,
                ambush_score=sig.ambush_score,
                short_score=sig.short_score,
                short_reasons=sig.short_reasons,
                verify=sig.verify,
                drawdown=sig.drawdown,
                vol_ratio=sig.vol_ratio,
                atr_ratio=sig.atr_ratio,
                atr_stop_pct=sig.atr_stop_pct,
                entry_temp=temperature,
            )
            self._pending_buys.append(order)
            current_pos_count += 1

    # ──────────────── 持仓检查 ────────────────

    def check_positions(self, date: str, snapshot: Dict[str, pd.Series]):
        """
        检查现有持仓的止盈/止损/到期。
        触发条件 → 生成待卖出订单（次日开盘执行）。
        """
        if not self.positions:
            return

        to_close = []
        for code, pos in self.positions.items():
            pos.hold_days += 1

            row = snapshot.get(code)
            if row is None:
                continue  # 停牌，顺延

            current_close = float(row.get('close', 0))
            if current_close <= 0:
                continue

            # 止损检查
            if current_close <= pos.stop_loss_price:
                to_close.append((code, 'stop_loss'))
                continue

            # 止盈检查
            if (self.config.take_profit > 0
                    and pos.take_profit_price > 0
                    and current_close >= pos.take_profit_price):
                to_close.append((code, 'take_profit'))
                continue

            # 移动止盈检查（trailing stop）
            ta = getattr(self.config, 'trailing_stop_activate', 0.10)
            td = getattr(self.config, 'trailing_stop_drawdown', 0.06)
            # 更新持仓期间最高价
            pos.trailing_high = max(pos.trailing_high, current_close)
            # 检查是否激活：从入场价涨超 ta%
            if not pos.trailing_active:
                gain = (current_close - pos.entry_price) / pos.entry_price
                if gain >= ta:
                    pos.trailing_active = True
            # 激活后：从最高点回撤 td% 就卖
            if pos.trailing_active:
                drawdown_from_peak = (pos.trailing_high - current_close) / pos.trailing_high
                if drawdown_from_peak >= td:
                    to_close.append((code, 'trailing_stop'))
                    continue

            # 到期检查
            if pos.hold_days >= self.config.max_hold_days:
                to_close.append((code, 'expiry'))
                continue

        for code, reason in to_close:
            # 检查是否已有待卖订单
            if not any(p.code == code and p.order_type == 'sell'
                       for p in self._pending_sells):
                order = PendingOrder(
                    code=code, order_type='sell', reason=reason,
                    entry_temp=0,  # will be filled at sell time
                )
                self._pending_sells.append(order)

    # ──────────────── 净值计算 ────────────────

    def record_equity(self, date: str, snapshot: Dict[str, pd.Series]):
        """记录当日总资产 = 现金 + 持仓市值"""
        total_value = self.cash
        for code, pos in self.positions.items():
            row = snapshot.get(code)
            if row is not None:
                price = float(row.get('close', 0))
                total_value += pos.quantity * price
            else:
                # 停牌用最后已知价格（这里用 entry_price 近似）
                total_value += pos.quantity * pos.entry_price
        self.daily_equity.append((date, round(total_value, 2)))

    def get_current_equity(self, snapshot: Dict[str, pd.Series]) -> float:
        """获取当前总资产快照（不记录）"""
        total = self.cash
        for code, pos in self.positions.items():
            row = snapshot.get(code)
            if row is not None:
                total += pos.quantity * float(row['close'])
            else:
                total += pos.quantity * pos.entry_price
        return total

    # ──────────────── 费用计算 ────────────────

    def _calc_buy_fee(self, price: float, qty: int) -> float:
        """买入费用：佣金，最低 5 元"""
        amount = price * qty
        fee = amount * self.config.commission_rate
        return max(fee, self.config.min_commission)

    def _calc_sell_fee(self, price: float, qty: int) -> float:
        """卖出费用：佣金 + 印花税"""
        amount = price * qty
        commission = max(amount * self.config.commission_rate,
                         self.config.min_commission)
        stamp_tax = amount * self.config.stamp_tax_rate
        return commission + stamp_tax

    # ──────────────── 状态报告 ────────────────

    def summary(self) -> dict:
        """返回引擎运行统计"""
        return {
            "initial_capital": self.initial_capital,
            "final_cash": round(self.cash, 2),
            "open_positions": len(self.positions),
            "closed_trades": len(self.closed_trades),
            "pending_buys": len(self._pending_buys),
            "pending_sells": len(self._pending_sells),
            "total_buy_value": round(self.total_buy_value, 2),
            "total_sell_value": round(self.total_sell_value, 2),
            "buy_orders": self.stats["buy_orders"],
            "sell_orders": self.stats["sell_orders"],
            "cancelled_buys": self.stats["cancelled_buys"],
            "cancelled_sells": self.stats["cancelled_sells"],
            "cash_remaining": round(self.cash, 2),
        }

    def reset(self):
        """重置引擎状态（用于多次回测）"""
        self.cash = float(self.initial_capital)
        self.positions.clear()
        self.closed_trades.clear()
        self._pending_buys.clear()
        self._pending_sells.clear()
        self.daily_equity.clear()
        self.total_buy_value = 0.0
        self.total_sell_value = 0.0
        self.stats = {"buy_orders": 0, "sell_orders": 0,
                      "cancelled_buys": 0, "cancelled_sells": 0}


# ═══════════════════════════════════════════════════════════
# 工具函数：计算指标
# ═══════════════════════════════════════════════════════════
def calc_sharpe_ratio(returns: List[float], rf: float = 0.02) -> float:
    """计算夏普比率（日收益 → 年化）"""
    if len(returns) < 2:
        return 0.0
    arr = np.array(returns)
    excess = arr - rf / 252
    if np.std(excess) == 0:
        return 0.0
    sharpe = np.mean(excess) / np.std(excess) * np.sqrt(252)
    return float(sharpe)


def calc_max_drawdown(equity_curve: List[float]) -> Tuple[float, str, str]:
    """计算最大回撤及起止日期"""
    if len(equity_curve) < 2:
        return 0.0, "", ""
    arr = np.array(equity_curve)
    peak = np.maximum.accumulate(arr)
    dd = (arr - peak) / peak
    max_dd = float(np.min(dd))
    return abs(max_dd) * 100, "", ""  # 返回百分比


if __name__ == "__main__":
    print("=" * 50)
    print("backtest_simulator.py — 单元自检")
    print("=" * 50)
    print("模块加载成功")
    print(f"  Signal, Position, Trade, PendingOrder 数据结构就绪")
    print(f"  SignalReplayer, TradeEngine 类就绪")
