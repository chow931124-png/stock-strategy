#!/usr/bin/env python3
"""
self_learn.py — 自学习引擎

闭环式学习回路：
  信号产生 → 记录到 signal_log.json
          → 5天后自动回检涨跌
          → 10天后自动回检涨跌
          → 统计各层级/板块/个股的真实胜率
          → 校准凯利公式参数
          → 输出学习报告

用法:
  python3 self_learn.py              # 回检所有未检查信号 + 输出报告
  python3 self_learn.py --report     # 只看当前学习成果
  python3 self_learn.py --calibrate  # 校准参数并保存建议

整合到策略中：
  stock_strategy_v3.py 每次跑完后自动调用 self_learn.run()
"""

import json, sys, os
from datetime import datetime, timedelta
from pathlib import Path
from collections import defaultdict, Counter
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

# ── 复用缓存和 K 线数据 ──
CACHE_DIR = Path("/app/cache") if Path("/app/cache").exists() else Path(__file__).parent / "cache"
SIGNAL_LOG_PATH = CACHE_DIR / "signal_log.json"
LEARN_DATA_PATH = CACHE_DIR / "self_learn.json"
CALIBRATION_PATH = CACHE_DIR / "calibration.json"


class SelfLearnEngine:
    """
    自学习引擎核心。

    数据流:
      signal_log.json  ← 每日信号记录
            ↓ 回检
      self_learn.json  ← 检查结果 + 统计
            ↓ 分析
      calibration.json ← 校准建议（参数调整）
    """

    def __init__(self):
        self.signal_log = self._load_json(SIGNAL_LOG_PATH, {})
        self.learn_data = self._load_json(LEARN_DATA_PATH, {
            "stocks": {}, "tiers": {}, "sectors": {},
            "last_check_date": "", "total_checks": 0,
        })
        self.calibration = self._load_json(CALIBRATION_PATH, {})
        self.kline_cache: Dict[str, pd.DataFrame] = {}

    # ──────────── 数据加载 ────────────

    def _load_json(self, path: Path, default):
        if path.exists():
            try:
                return json.loads(path.read_text())
            except:
                pass
        return default

    def _save_json(self, path: Path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2))

    def _load_kline_cache(self):
        """从回测缓存加载 K 线数据（如果有），否则跳过"""
        cache_dir = CACHE_DIR / "klines_backtest"
        if not cache_dir.exists():
            return
        loaded = 0
        for pkl in sorted(cache_dir.glob("*.pkl")):
            try:
                df = pd.read_pickle(pkl)
                if 'date' in df.columns:
                    df['date'] = pd.to_datetime(df['date']).dt.strftime('%Y-%m-%d')
                self.kline_cache[pkl.stem] = df
                loaded += 1
            except:
                pass
        if loaded > 0:
            print(f"  [自学习] 加载 {loaded} 只股票 K 线缓存")

    # ──────────── 核心：回检信号 ────────────

    def check_signals(self) -> int:
        """
        检查 signal_log.json 中所有未回检的信号。
        用现有 K 线缓存查 5 日/10 日后的价格表现。

        返回本次新检查的信号数。
        """
        self._load_kline_cache()
        checked = 0
        today_str = datetime.now().strftime('%Y-%m-%d')

        for date_str in sorted(self.signal_log.keys()):
            signals = self.signal_log[date_str]
            for sig in signals:
                if not isinstance(sig, dict):
                    continue
                code = sig.get("code", "")
                if not code:
                    continue

                # 跳过已检查的
                if sig.get("check_5d") is not None and sig.get("check_10d") is not None:
                    continue

                entry_price = sig.get("price", 0)
                if entry_price <= 0:
                    continue

                # 查 K 线
                df = self._get_stock_klines(code)
                if df is None or len(df) < 10:
                    continue

                # 找信号日的位置
                if date_str not in df['date'].values:
                    continue
                idx = df[df['date'] == date_str].index[0]

                # 5 日后（T+5 收盘价）
                check_5d_idx = idx + 5
                check_5d = None
                if check_5d_idx < len(df):
                    price_5d = df.iloc[check_5d_idx]['close']
                    check_5d = round((price_5d - entry_price) / entry_price * 100, 2)

                # 10 日后（T+10 收盘价）
                check_10d_idx = idx + 10
                check_10d = None
                if check_10d_idx < len(df):
                    price_10d = df.iloc[check_10d_idx]['close']
                    check_10d = round((price_10d - entry_price) / entry_price * 100, 2)

                sig["check_5d"] = check_5d
                sig["check_10d"] = check_10d
                checked += 1

        if checked > 0:
            self._save_json(SIGNAL_LOG_PATH, self.signal_log)
            self.learn_data["last_check_date"] = today_str
            self.learn_data["total_checks"] = self.learn_data.get("total_checks", 0) + checked
            print(f"  [自学习] 回检了 {checked} 条信号")

        return checked

    def _get_stock_klines(self, code: str) -> Optional[pd.DataFrame]:
        """从内存缓存或 DataEngine 获取 K 线"""
        # 优先用缓存
        if code in self.kline_cache:
            return self.kline_cache[code]
        # 从磁盘缓存加载
        cache_file = CACHE_DIR / "klines_backtest" / f"{code}.pkl"
        if cache_file.exists():
            try:
                df = pd.read_pickle(cache_file)
                df['date'] = pd.to_datetime(df['date']).dt.strftime('%Y-%m-%d')
                self.kline_cache[code] = df
                return df
            except:
                pass
        return None

    # ──────────── 统计计算 ────────────

    def compute_stats(self) -> dict:
        """
        计算分层/板块/个股的统计数据。

        返回:
        {
          "by_tier": {"💎 精选层": {"total": N, "wins_5d": N, "wr_5d": %%, "avg_5d": %%, ...}, ...},
          "by_sector": {...},
          "by_stock": {...},
          "calibration_suggestions": {...},
          "total_signals_checked": N,
        }
        """
        # 收集所有已检查的信号
        records = []  # [{code, name, sector, tier, date, check_5d, check_10d, composite}]
        for date_str in self.signal_log:
            for sig in self.signal_log[date_str]:
                if not isinstance(sig, dict):
                    continue
                if sig.get("check_5d") is None:
                    continue
                records.append({
                    "code": sig["code"],
                    "name": sig.get("name", ""),
                    "sector": sig.get("sector", ""),
                    "tier": sig.get("tier", ""),
                    "date": date_str,
                    "r5": sig["check_5d"],
                    "r10": sig.get("check_10d"),
                    "composite": sig.get("composite", 0),
                })

        if not records:
            return {"total_signals_checked": 0, "message": "尚无已检查的信号，请先运行回检"}

        stats = {}
        stats["total_signals_checked"] = len(records)

        # ── 按层级 ──
        by_tier = defaultdict(list)
        for r in records:
            by_tier[r["tier"]].append(r)

        tier_stats = {}
        for tier, rs in sorted(by_tier.items()):
            r5s = [r["r5"] for r in rs if r["r5"] is not None]
            r10s = [r["r10"] for r in rs if r["r10"] is not None]
            tier_stats[tier] = {
                "total": len(rs),
                "wins_5d": sum(1 for v in r5s if v > 0),
                "wr_5d": round(sum(1 for v in r5s if v > 0) / len(r5s) * 100, 1) if r5s else 0,
                "avg_5d": round(float(np.mean(r5s)), 2) if r5s else 0,
                "wins_10d": sum(1 for v in r10s if v > 0),
                "wr_10d": round(sum(1 for v in r10s if v > 0) / len(r10s) * 100, 1) if r10s else 0,
                "avg_10d": round(float(np.mean(r10s)), 2) if r10s else 0,
                "max_win_5d": round(max(r5s), 2) if r5s else 0,
                "max_loss_5d": round(min(r5s), 2) if r5s else 0,
            }
        stats["by_tier"] = tier_stats

        # ── 按板块 ──
        by_sector = defaultdict(list)
        for r in records:
            by_sector[r["sector"]].append(r)

        sector_stats = {}
        for sector, rs in sorted(by_sector.items(), key=lambda x: -len(x[1])):
            r5s = [r["r5"] for r in rs if r["r5"] is not None]
            if len(rs) >= 3:  # 只显示有 3 笔以上数据的板块
                sector_stats[sector] = {
                    "total": len(rs),
                    "wr_5d": round(sum(1 for v in r5s if v > 0) / len(r5s) * 100, 1) if r5s else 0,
                    "avg_5d": round(float(np.mean(r5s)), 2) if r5s else 0,
                }
        stats["by_sector"] = sector_stats

        # ── 按个股（只展示 3 笔以上的） ──
        by_stock = defaultdict(list)
        for r in records:
            by_stock[r["code"]].append(r)

        stock_stats = {}
        for code, rs in sorted(by_stock.items(), key=lambda x: -len(x[1])):
            r5s = [r["r5"] for r in rs if r["r5"] is not None]
            if len(rs) >= 3:
                stock_stats[code] = {
                    "name": rs[0]["name"],
                    "sector": rs[0]["sector"],
                    "total": len(rs),
                    "wr_5d": round(sum(1 for v in r5s if v > 0) / len(r5s) * 100, 1) if r5s else 0,
                    "avg_5d": round(float(np.mean(r5s)), 2) if r5s else 0,
                }
        stats["by_stock"] = stock_stats

        # ── 校准建议 ──
        stats["calibration"] = self._generate_calibration(tier_stats)

        # 保存到 learn_data
        self.learn_data["stats"] = stats
        self._save_json(LEARN_DATA_PATH, self.learn_data)

        return stats

    def _generate_calibration(self, tier_stats: dict) -> dict:
        """
        基于真实胜率生成凯利参数校准建议。
        只在数据量 >= 30 笔/层级时才生成。
        """
        suggestions = {}

        # 原预设 vs 真实
        PRESET_WIN_RATES = {
            "💎 精选层": 0.90,
            "🥈 增强层": 0.69,
            "🥉 普通层": 0.55,
        }

        for tier, data in tier_stats.items():
            if data["total"] < 15:
                suggestions[tier] = {
                    "note": f"数据不足({data['total']}/15笔)，暂不校准",
                    "preset_wr": PRESET_WIN_RATES.get(tier, 0.5),
                    "real_wr_5d": data["wr_5d"],
                }
                continue

            real_wr = data["wr_5d"] / 100  # 转小数
            preset_wr = PRESET_WIN_RATES.get(tier, 0.5)

            # 如果真实胜率和预设偏差超过 10%，建议校准
            if abs(real_wr - preset_wr) > 0.10:
                suggestions[tier] = {
                    "note": "建议校准",
                    "preset_wr": preset_wr,
                    "real_wr_5d": data["wr_5d"],
                    "suggested_wr": round(real_wr, 2),
                    "suggested_kelly_multiplier": round(real_wr * 0.6, 2),
                }
            else:
                suggestions[tier] = {
                    "note": "偏差在允许范围内",
                    "preset_wr": preset_wr,
                    "real_wr_5d": data["wr_5d"],
                }

        return suggestions

    # ──────────── 报告输出 ────────────

    def report(self, stats: dict = None) -> str:
        """生成可读的自学习报告"""
        if stats is None:
            stats = self.learn_data.get("stats", {})
            if not stats:
                stats = self.compute_stats()

        if stats.get("total_signals_checked", 0) == 0:
            return "📖 尚无已检查的信号。先跑一次 self_learn.py 回检历史数据。"

        lines = []
        lines.append("")
        lines.append("╔════════════════════════════════════════════════════════╗")
        lines.append("║      🧠 自学习引擎报告                               ║")
        lines.append("╚════════════════════════════════════════════════════════╝")
        lines.append("")
        lines.append(f"  已检测信号: {stats['total_signals_checked']} 笔")
        lines.append(f"  最后更新: {self.learn_data.get('last_check_date', '未知')}")
        lines.append("")

        # 分层胜率
        tier_data = stats.get("by_tier", {})
        if tier_data:
            lines.append("  ── 📊 分层真实胜率 (T+5) ──")
            for tier in ["💎 精选层", "🥈 增强层", "🥉 普通层"]:
                td = tier_data.get(tier)
                if td and td["total"] > 0:
                    preset = {"💎 精选层": 90, "🥈 增强层": 69, "🥉 普通层": 55}.get(tier, 50)
                    actual = td["wr_5d"]
                    delta = actual - preset
                    delta_str = f"+{delta:.0f}" if delta > 0 else f"{delta:.0f}"
                    emoji = "✅" if delta > -5 else "⚠️"
                    lines.append(f"    {tier}: {td['total']}笔  预设{preset}% → 真实{actual}% ({delta_str}%) {emoji}")
                    lines.append(f"           均收益{td['avg_5d']:+.2f}%  最大赢{td['max_win_5d']:+.2f}%  最大亏{td['max_loss_5d']:+.2f}%")
            lines.append("")

        # 板块排名
        sector_data = stats.get("by_sector", {})
        if sector_data:
            lines.append("  ── 🏆 板块胜率排名 (≥3笔) ──")
            sorted_sec = sorted(sector_data.items(), key=lambda x: -x[1]["wr_5d"])
            for sector, sd in sorted_sec[:8]:
                lines.append(f"    {sector}: {sd['total']}笔  胜率{sd['wr_5d']:.0f}%  均收益{sd['avg_5d']:+.2f}%")
            lines.append("")

        # 校准建议
        cal = stats.get("calibration", {})
        if cal:
            lines.append("  ── 🔧 参数校准建议 ──")
            for tier, c in cal.items():
                if "suggested_wr" in c:
                    lines.append(f"    {tier}: 胜率 {c['preset_wr']*100:.0f}% → 建议 {c['suggested_wr']*100:.0f}%")
                    lines.append(f"           凯利乘数: 0.6 → 建议 {c.get('suggested_kelly_multiplier', 0.6)}")
                else:
                    lines.append(f"    {tier}: {c['note']} (真实{c['real_wr_5d']:.0f}%)")
            lines.append("")

        # 个股 Top/Bottom
        stock_data = stats.get("by_stock", {})
        if stock_data:
            sorted_stock = sorted(stock_data.items(), key=lambda x: -x[1]["wr_5d"])
            lines.append("  ── ⭐ 最靠谱个股 (TOP 5) ──")
            for code, sd in sorted_stock[:5]:
                lines.append(f"    {sd['name']}({code}) [{sd['sector']}] {sd['total']}笔  胜率{sd['wr_5d']:.0f}%  均+{sd['avg_5d']:.2f}%")
            lines.append("")

        return "\n".join(lines)

    # ──────────── 一键运行 ────────────

    def run(self) -> dict:
        """完整执行：回检 → 统计 → 报告 → 保存"""
        print("\n🧠 [自学习] 开始...")
        checked = self.check_signals()
        stats = self.compute_stats()
        print(f"  [自学习] 累计已检查: {stats.get('total_signals_checked', 0)} 笔")
        print(f"  [自学习] 完成\n")
        return stats


# ═══════════════════════════════════════════════════════════
# CLI 独立运行
# ═══════════════════════════════════════════════════════════
def main():
    import argparse
    parser = argparse.ArgumentParser(description="自学习引擎")
    parser.add_argument("--report", action="store_true", help="只看报告")
    parser.add_argument("--calibrate", action="store_true", help="显示校准建议")
    args = parser.parse_args()

    engine = SelfLearnEngine()

    if args.report:
        stats = engine.learn_data.get("stats", {})
        if not stats:
            stats = engine.compute_stats()
        print(engine.report(stats))
    elif args.calibrate:
        stats = engine.compute_stats()
        cal = stats.get("calibration", {})
        if cal:
            print("\n🔧 校准建议:")
            for tier, c in cal.items():
                if "suggested_wr" in c:
                    print(f"  {tier}: {c['preset_wr']*100:.0f}% → {c['suggested_wr']*100:.0f}% (建议修改)")
                else:
                    print(f"  {tier}: {c['note']}")
        else:
            print("尚无校准建议，数据不足")
    else:
        stats = engine.run()
        print(engine.report(stats))


if __name__ == "__main__":
    main()
