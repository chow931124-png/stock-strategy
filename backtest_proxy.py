#!/usr/bin/env python3
"""
backtest_proxy.py — 回测历史代理模块

为 stock_strategy_v3.py 提供：
1. MarketThermometerProxy   — 用股票池行情数据模拟市场温度计
2. SectorScorerProxy        — 用板块内个股相对表现模拟板块评分
3. KlineCache               — 多级 K 线缓存（内存 + 磁盘 pickle）

这些 Proxy 解决了原策略依赖实时数据（同花顺热点、北向资金等）
而历史不可用的问题，让回测能在纯 K 线数据上运行。
"""

import os, time, random, json
import pickle
from pathlib import Path
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

# ── 复用原策略的数据与工具 ──
from stock_strategy_v3 import (
    BUILTIN_STOCKS, STRATEGY, SECTOR_PREFERENCE,
    DataEngine, CACHE_DIR, AutoSectorMapper,
)


# ═══════════════════════════════════════════════════════════
# KlineCache — 多级 K 线缓存
# ═══════════════════════════════════════════════════════════
class KlineCache:
    """
    三层 K 线缓存：
    层0: DataEngine 实时拉取（兜底）
    层1: 磁盘 pickle 缓存（跨运行持久化，cache/klines_backtest/）
    层2: 内存 dict（回测运行期快速访问）

    预热时自动识别所有股票的共有交易日，作为回测的交易日历。
    """

    def __init__(self, cache_dir: str = None):
        self.cache_dir = Path(cache_dir or CACHE_DIR) / "klines_backtest"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._memory: Dict[str, pd.DataFrame] = {}
        self._trading_dates: List[str] = []
        self._stock_pool: List[tuple] = BUILTIN_STOCKS  # 固定池（回测不用动态池）

    def _cache_path(self, code: str) -> Path:
        return self.cache_dir / f"{code}.pkl"

    def _normalize_dates(self, df: pd.DataFrame) -> pd.DataFrame:
        """统一 date 列为日期格式（去掉时间部分，统一为 YYYY-MM-DD 字符串）"""
        if df is None or df.empty:
            return df
        df = df.copy()
        if 'date' in df.columns:
            dt = pd.to_datetime(df['date'])
            df['date'] = dt.dt.strftime('%Y-%m-%d')
        return df

    def load(self, code: str, force_refetch: bool = False) -> Optional[pd.DataFrame]:
        """加载单只股票 K 线：内存 → 磁盘 → DataEngine，统一日期格式"""
        # 层2: 内存
        if code in self._memory and not force_refetch:
            return self._memory[code]

        # 层1: 磁盘
        cache_path = self._cache_path(code)
        if cache_path.exists() and not force_refetch:
            try:
                df = pd.read_pickle(cache_path)
                if isinstance(df, pd.DataFrame) and len(df) > 100:
                    df = self._normalize_dates(df)
                    self._memory[code] = df
                    return df
            except Exception:
                pass

        # 层0: DataEngine 实时拉取
        de = DataEngine()
        df = de.get_klines(code)
        if df is None or len(df) < 100:
            print(f"    [缓存] {code} DataEngine 数据不足({len(df) if df is not None else 0})")
            return None

        df = self._normalize_dates(df)

        # 存磁盘（含规范化后的日期）
        try:
            df.to_pickle(cache_path)
            print(f"    [缓存] {code} {len(df)} 天 → {cache_path.name}")
        except Exception as e:
            print(f"    [缓存] {code} 写入失败: {e}")

        self._memory[code] = df
        return df

    def warm_up(self, stock_list: List[tuple] = None, force: bool = False) -> int:
        """
        预热所有股票 K 线到缓存。
        返回成功加载的股票数。
        """
        pool = stock_list or self._stock_pool
        success = 0
        total = len(pool)

        print(f"\n📡 [缓存] 预热 {total} 只股票 K 线...")
        for i, (code, name, sector) in enumerate(pool):
            df = self.load(code, force_refetch=force)
            if df is not None:
                success += 1
            if (i + 1) % 10 == 0:
                print(f"    [{i+1}/{total}] 已缓存 {success} 只")
            time.sleep(random.uniform(0.05, 0.1))  # 温和节流

        print(f"    [缓存] 完成: {success}/{total} 只成功加载\n")
        return success

    def build_trading_calendar(self) -> List[str]:
        """
        从所有已缓存 K 线中提取共有交易日，排序后返回。
        以出现频率最高的日期作为 A 股交易日。
        """
        print("  [日历] 构建交易日历...")
        date_sets = []
        for code, df in self._memory.items():
            if df is not None and 'date' in df.columns:
                dates = set(df['date'].values)  # already string format
                date_sets.append(dates)

        if not date_sets:
            print("  [日历] 无数据！")
            return []

        # 取出现频率 > 60% 的日期作为交易日
        all_dates = defaultdict(int)
        for ds in date_sets:
            for d in ds:
                all_dates[d] += 1

        threshold = max(3, len(date_sets) * 0.6)
        trading_dates = sorted(d for d, cnt in all_dates.items() if cnt >= threshold)

        print(f"  [日历] 共 {len(trading_dates)} 个交易日 "
              f"({trading_dates[0][:10]} ~ {trading_dates[-1][:10]})")
        self._trading_dates = trading_dates
        return trading_dates

    def get_pool_snapshot(self, date: str) -> Dict[str, pd.Series]:
        """
        获取指定日期所有股票的最新行情行。
        返回 {code: pd.Series}，缺失的股票不包含在内。
        """
        snapshot = {}
        for code, df in self._memory.items():
            if df is None or 'date' not in df.columns:
                continue
            match = df[df['date'] == date]
            if len(match) > 0:
                snapshot[code] = match.iloc[-1]
        return snapshot

    def get_stock_pool(self) -> List[tuple]:
        return self._stock_pool


# ═══════════════════════════════════════════════════════════
# MarketThermometerProxy — 市场温度计历史代理
# ═══════════════════════════════════════════════════════════
class MarketThermometerProxy:
    """
    用股票池中的个股涨跌分布模拟市场温度。

    Proxy 维度（与原温度计对应）:
      ① 上涨占比（原 25% 权重）→ 池中涨幅>0 的比例
      ② 涨停跌停比（原 30% 权重）→ 涨幅>9.5% / 跌幅>9.5% 的比值
      ③ 极端惩罚（替代北向+两融）→ 跌超5% 的比例

    原温度计在 75/60/45/30 四挡决定仓位上限（1.0/0.6/0.3/0.0），
    本 Proxy 的输出范围与之兼容。
    """

    def calc_from_snapshot(self, snapshot: Dict[str, pd.Series]) -> int:
        """
        从行情快照计算当日市场温度。

        参数
        ----
        snapshot : {code: pd.Series}  — get_pool_snapshot() 的输出

        返回
        ----
        temperature : int (0 ~ 100)
        """
        changes = []
        for code, row in snapshot.items():
            chg = row.get('change_pct', None)
            if pd.notna(chg):
                changes.append(chg)

        if not changes:
            return 50  # 数据不足时返回中性

        total = len(changes)
        up_count = sum(1 for c in changes if c > 0)
        limit_up = sum(1 for c in changes if c >= 9.5)
        limit_down = sum(1 for c in changes if c <= -9.5)

        # ① 上涨占比评分（0-40 分）
        up_ratio = up_count / total
        up_score = min(40, int(up_ratio * 60))

        # ② 涨停跌停比评分（0-35 分）
        zt_ratio = limit_up / max(limit_down, 1)
        if zt_ratio >= 5:
            zt_score = 35
        elif zt_ratio >= 3:
            zt_score = 30
        elif zt_ratio >= 1.5:
            zt_score = 25
        elif zt_ratio >= 1:
            zt_score = 20
        elif zt_ratio >= 0.5:
            zt_score = 12
        else:
            zt_score = 5

        # ③ 极端惩罚分（0-25 分）— 跌超 5% 越多分越低
        bad = sum(1 for c in changes if c <= -5)
        bad_ratio = bad / total
        if bad_ratio > 0.20:
            extreme_score = 0
        elif bad_ratio > 0.10:
            extreme_score = 8
        elif bad_ratio > 0.05:
            extreme_score = 15
        else:
            extreme_score = 25

        raw = up_score + zt_score + extreme_score
        return min(100, max(0, raw))

    def get_position_limit(self, temperature: int) -> float:
        """
        与原 MarketThermometer.get_position_limit() 完全一致的仓位上限逻辑。
        """
        if temperature >= 75:
            return 0.5
        elif temperature >= 60:
            return 1.0
        elif temperature >= 45:
            return 0.6
        elif temperature >= 30:
            return 0.3
        else:
            return 0.0

    def get_market_state(self, temperature: int) -> str:
        if temperature >= 75:
            return "🔥 亢奋（注意风险）"
        elif temperature >= 60:
            return "✅ 正常（可操作）"
        elif temperature >= 45:
            return "🟡 偏冷（减半仓）"
        elif temperature >= 30:
            return "⚠️ 低迷（仅精选信号）"
        else:
            return "🔴 冰点（暂停策略）"


# ═══════════════════════════════════════════════════════════
# SectorScorerProxy — 板块评分历史代理
# ═══════════════════════════════════════════════════════════
class SectorScorerProxy:
    """
    用板块内个股相对于全池的表现来评分。

    评分维度（每个 0-25，总分 0-100）:
      ① 多日动量: 板块 N 日平均收益 - 全池平均收益，相对越强分越高
      ② 当日涨跌比: 板块内上涨股票的比例
      ③ 领头羊: 板块内最高涨幅（衡量龙头带动）
      ④ 稳定性: 板块内收益标准差（越小越稳定=越健康）

    13 个赛道的划分沿用 BUILTIN_STOCKS 的分类。
    """

    def __init__(self, stock_pool: List[tuple] = None):
        pool = stock_pool or BUILTIN_STOCKS
        # sector -> [code, ...]
        self.sector_stocks: Dict[str, List[str]] = defaultdict(list)
        # code -> sector
        self.code_sector: Dict[str, str] = {}
        for code, name, sector in pool:
            self.sector_stocks[sector].append(code)
            self.code_sector[code] = sector
        self.sectors = list(self.sector_stocks.keys())
        # 缓存加速
        self._cache: Dict[str, Dict[str, int]] = {}

    def calc(self, snapshot: Dict[str, pd.Series],
             date: str, lookback: int = 5) -> Dict[str, int]:
        """
        计算所有赛道的评分 (0-100)。

        参数
        ----
        snapshot : 该日的行情快照 {code: pd.Series}
        date     : 交易日（仅用于缓存键）
        lookback : N 日动量窗口

        返回
        ----
        {sector: score}
        """
        # 查缓存
        cache_key = f"{date}_lb{lookback}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        # ── 收集全池逐股数据 ──
        # {code: n_day_return}
        stock_returns = {}
        # {code: daily_change_pct}
        stock_changes = {}
        pool_returns = []

        for code, row in snapshot.items():
            n_day_ret = row.get('gain_60d' if lookback >= 20 else f'strength_{lookback}d',
                                None)
            # 如果预计算字段不存在，手动算
            if pd.isna(n_day_ret) and 'close' in row.index:
                # 需要向前找 lookback 天的 close
                # 但 snapshot 只有一行，这里 fallback 到 change_pct
                n_day_ret = row.get('change_pct', 0) * lookback * 0.5  # 粗糙估算

            chg = row.get('change_pct', None)

            # 用 change_pct 作为兜底动量
            if pd.isna(n_day_ret) or n_day_ret is None:
                n_day_ret = chg if pd.notna(chg) else 0

            stock_returns[code] = n_day_ret
            stock_changes[code] = chg if pd.notna(chg) else 0
            if pd.notna(n_day_ret):
                pool_returns.append(n_day_ret)

        if not pool_returns:
            result = {s: 50 for s in self.sectors}
            self._cache[cache_key] = result
            return result

        pool_mean = float(np.mean(pool_returns))

        # ── 逐赛道打分 ──
        scores = {}
        for sector, codes in self.sector_stocks.items():
            sec_rets = [stock_returns[c] for c in codes if c in stock_returns]
            sec_chgs = [stock_changes[c] for c in codes if c in stock_changes]
            total_in_sector = len(codes)

            if not sec_rets:
                scores[sector] = 50
                continue

            # ① 多日动量 (0-25)
            sec_mean = float(np.mean(sec_rets))
            relative = sec_mean - pool_mean
            # relative 通常在 -0.05~0.05，用乘数 250 映射到 -12.5~+12.5，再移到 12.5 基线
            momentum = max(0, min(25, 12.5 + relative * 250))

            # ② 涨跌比 (0-25)
            pos = sum(1 for c in sec_chgs if c > 0)
            ratio = pos / len(sec_chgs) if sec_chgs else 0.5
            change_score = ratio * 25

            # ③ 领头羊强度 (0-25)
            max_chg = max(sec_chgs) if sec_chgs else 0
            # 涨幅 10% ≈ 25 分，5% ≈ 15 分，2% ≈ 8 分
            leader_score = max(0, min(25, max_chg * 2.5))

            # ④ 稳定性 (0-25)
            if len(sec_rets) >= 3:
                std = float(np.std(sec_rets)) * 100  # 转百分比
                # std 越小越稳定: 0%→25分, 3%→10分, 5%→5分, 10%→0分
                stability = max(0, min(25, 25 - std * 5))
            else:
                stability = 12

            total_score = momentum + change_score + leader_score + stability
            scores[sector] = int(min(100, max(0, total_score)))

        # 缓存
        self._cache[cache_key] = scores
        return scores

    def clear_cache(self):
        self._cache = {}


# ═══════════════════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════════════════
def get_stock_name(code: str, stock_pool: List[tuple] = None) -> str:
    """从股票池查找股票名称"""
    pool = stock_pool or BUILTIN_STOCKS
    for c, name, _ in pool:
        if c == code:
            return name
    return code


def filter_stock_pool(stock_list: List[tuple],
                      temperature: int = 50,
                      prefer_list: List[str] = None,
                      neutral_list: List[str] = None,
                      avoid_list: List[str] = None) -> List[tuple]:
    """
    根据市场温度过滤股票池（与原策略 main() 逻辑一致）。

    温度 >= 45:  全池可用
    温度 30-45:  仅 prefer + neutral
    温度 < 30:   仅 prefer
    """
    if temperature >= 45:
        return stock_list

    if prefer_list is None:
        prefer_list = SECTOR_PREFERENCE.get("prefer", [])
    if neutral_list is None:
        neutral_list = SECTOR_PREFERENCE.get("neutral", [])

    if temperature >= 30:
        allowed = set(prefer_list + neutral_list)
        return [(c, n, s) for c, n, s in stock_list if s in allowed]
    else:
        allowed = set(prefer_list)
        return [(c, n, s) for c, n, s in stock_list if s in allowed]


if __name__ == "__main__":
    # 简单测试
    print("=" * 50)
    print("backtest_proxy.py — 单元自检")
    print("=" * 50)

    # 测试 KlineCache
    cache = KlineCache()
    ok = cache.warm_up(force=False)
    if ok > 0:
        dates = cache.build_trading_calendar()
        if dates:
            # 测试温度计
            snap = cache.get_pool_snapshot(dates[-10])
            if snap:
                temp = MarketThermometerProxy()
                t = temp.calc_from_snapshot(snap)
                print(f"  温度计测试: 日期 {dates[-10]} → {t} 分")
                print(f"  仓位上限: {temp.get_position_limit(t)*100:.0f}%")
                print(f"  市场状态: {temp.get_market_state(t)}")

            # 测试板块评分
            scorer = SectorScorerProxy()
            scores = scorer.calc(snap, dates[-10])
            if scores:
                top = sorted(scores.items(), key=lambda x: -x[1])[:5]
                print(f"\n  板块评分 TOP5:")
                for s, sc in top:
                    print(f"    {s}: {sc} 分")
