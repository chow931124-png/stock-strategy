"""回调低吸扫描器 — 移植 v3.0 StockScorer 三层信号逻辑

三层筛选（与 v3.0 完全一致）:
  Base:    回调 5-35% + 量比 > 1.3 + 站上 MA5 + 非大跌日
  Enhanced: Base + ATR 3-8%（高弹性）
  Elite:    Base + ATR 5-8% + bias_MA20 < -2（均值回归确认）
"""
from agent_orch.agent_base import BaseScreener
from data.market_data import get_bars
from config import get_config
import numpy as np


class PullbackScreener(BaseScreener):
    """回调低吸扫描器（v3.0 三层信号）"""

    def __init__(self):
        super().__init__("PullbackScreener")

    async def screen(self, candidates: list[str], market_context: dict) -> list[str]:
        scored = []
        for code in candidates:
            try:
                row = self._compute_indicators(code)
                if row is None:
                    continue
                tier, score = self._tier_score(row)
                if tier:
                    scored.append((score, code, tier, row))
            except Exception:
                continue

        scored.sort(key=lambda x: -x[0])

        self.last_results = scored  # 供 debug
        return [code for _, code, _, _ in scored][:30]

    def _compute_indicators(self, code: str) -> dict | None:
        """计算与 v3.0 完全一致的指标"""
        klines = get_bars(code, category=4, offset=60)
        if len(klines) < 30:
            return None

        closes = np.array([k["close"] for k in klines])
        highs = np.array([k["high"] for k in klines])
        lows = np.array([k["low"] for k in klines])
        volumes = np.array([k["volume"] for k in klines])
        current = closes[-1]

        if current <= 0:
            return None

        # MA 系列
        ma5 = np.mean(closes[-5:]) if len(closes) >= 5 else np.mean(closes)
        ma20 = np.mean(closes[-20:]) if len(closes) >= 20 else np.mean(closes)
        ma60 = np.mean(closes[-60:]) if len(closes) >= 60 else np.mean(closes)

        # bias_ma20（偏离度，%）
        bias_ma20 = (current - ma20) / ma20 * 100

        # 60日最高点
        peak_60 = np.max(closes[-60:]) if len(closes) >= 60 else np.max(closes)
        drawdown = (current - peak_60) / peak_60 * 100  # 负数

        # 量比（近5日均量 / 近20日均量，与 v3.0 一致）
        vol_5 = np.mean(volumes[-5:]) if len(volumes) >= 5 else np.mean(volumes)
        vol_20 = np.mean(volumes[-20:]) if len(volumes) >= 20 else np.mean(volumes)
        vol_ratio = vol_5 / vol_20 if vol_20 > 0 else 1

        # ATR 14
        if len(closes) >= 15:
            tr = np.maximum(
                highs[-14:] - lows[-14:],
                np.abs(highs[-14:] - closes[-15:-1]),
                np.abs(lows[-14:] - closes[-15:-1]),
            )
            atr14 = np.mean(tr)
        else:
            atr14 = 0
        atr_ratio = atr14 / current * 100 if current > 0 else 0

        # 当日涨跌幅
        change_pct = (closes[-1] - closes[-2]) / closes[-2] * 100 if len(closes) >= 2 else 0

        # MACD（简化版）
        ema12 = closes[-1]
        ema26 = closes[-1]
        if len(closes) >= 12:
            ema12 = np.mean(closes[-12:])
        if len(closes) >= 26:
            ema26 = np.mean(closes[-26:])
        macd = ema12 - ema26

        return {
            "code": code,
            "close": current,
            "change_pct": change_pct,
            "ma5": ma5,
            "ma20": ma20,
            "ma60": ma60,
            "bias_ma20": bias_ma20,
            "drawdown": drawdown,
            "vol_ratio": vol_ratio,
            "atr_ratio": atr_ratio,
            "macd": macd,
            "peak_60": peak_60,
        }

    def _tier_score(self, row: dict) -> tuple:
        """
        三层评分（与 v3.0 完全一致）
        返回: (tier_name, score) 或 (None, 0)
        """
        cfg = get_config().get("screening", {}).get("pullback", {})

        # ── Base 层 ──
        dd_min = cfg.get("drawdown_min", 0.05) * 100   # 5%
        dd_max = cfg.get("drawdown_max", 0.35) * 100   # 35%
        vol_min = cfg.get("vol_ratio_min", 1.3)        # 1.3

        dd = abs(row.get("drawdown", 0))
        vr = row.get("vol_ratio", 0)
        chg = row.get("change_pct", 0)

        # 基础条件（与 v3.0 check_base 完全一致）
        if row["drawdown"] >= 0:  # 没跌
            return None, 0
        if not (dd_min <= dd <= dd_max):
            return None, 0
        if vr < vol_min:
            return None, 0
        if row["close"] < row["ma5"] * 0.98:  # 没站上 MA5（允许2%偏离）
            return None, 0
        if chg < -5:  # 还在大跌
            return None, 0

        # Base 评分
        score = 50
        # 回调深度加分：5-15% 最佳
        if dd <= 15:
            score += 15
        # 量比加分
        if vr >= 2.0:
            score += 10
        elif vr >= 1.5:
            score += 5
        # 站上 MA20 加分
        if row["close"] > row["ma20"]:
            score += 10
        # MACD 金叉加分
        if row["macd"] > 0:
            score += 10

        # ── Enhanced 层：ATR 弹性 3-8% ──
        atr_enhanced_min = cfg.get("atr_enhanced_min", 3.0)
        atr_ratio = row.get("atr_ratio", 0)
        if atr_enhanced_min <= atr_ratio < 8.0:
            score += 15
            # ── Elite 层：ATR 5-8% + bias_MA20 < -2 ──
            atr_elite_min = cfg.get("atr_elite_min", 5.0)
            atr_elite_max = cfg.get("atr_elite_max", 8.0)
            bias_max = cfg.get("bias_ma20_max", -2)
            if atr_elite_min <= atr_ratio < atr_elite_max and row.get("bias_ma20", 0) < bias_max:
                score += 15
                return ("elite", min(100, score))

            return ("enhanced", min(100, score))

        return ("base", min(100, score))
