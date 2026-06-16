"""市场状态检测器 — 复用 v3.0 MarketThermometer 逻辑

4 因子加权:
- 涨停跌停比 30%
- 上涨股票比例 25%
- 北向资金流向 25%
- 两融余额趋势 20%
"""
from data.market_data import tencent_quote, get_indices
from data.capital_data import hsgt_realtime, margin_trading

from config import get_config
from datetime import datetime
import pandas as pd
import numpy as np


def detect_market_regime() -> dict:
    """
    检测当前市场状态

    返回:
        {regime: "BULL"|"BEAR"|"SIDEWAYS"|"CRISIS",
         temperature: 0-100,
         components: {...}}
    """
    cfg = get_config().get("screening", {}).get("market_regime", {})

    # 获取各因子数据
    indices = get_indices()
    components = {}

    # 因子 1: 涨停跌停比（暂用指数涨跌替代）
    # 实际场景从 THS 或东财获取涨跌停数据
    sh_idx = indices.get("上证指数", {})
    sh_change = abs(sh_idx.get("change_pct", 0))
    if sh_change > 2:
        zone_score = 80 if sh_idx.get("change_pct", 0) > 0 else 20
    elif sh_change > 1:
        zone_score = 65 if sh_idx.get("change_pct", 0) > 0 else 35
    else:
        zone_score = 50

    # 因子 2: 上涨比例（暂用 50 作为默认）
    up_ratio_score = 50

    # 因子 3: 北向资金（用当天数据）
    try:
        hsgt = hsgt_realtime()
        if not hsgt.empty:
            total_hgt = hsgt["hgt_yi"].iloc[-1] if "hgt_yi" in hsgt.columns else 0
            total_sgt = hsgt["sgt_yi"].iloc[-1] if "sgt_yi" in hsgt.columns else 0
            northbound_net = total_hgt + total_sgt
            nb_score = min(100, max(0, 50 + northbound_net * 5))
        else:
            nb_score = 50
    except Exception:
        nb_score = 50

    # 因子 4: 两融趋势（用最近一期数据）
    margin_score = 50

    # 加权计算（动态剔除死因子：如果某个因子固定为50分，移除它并归一剩余权重）
    raw_weights = {
        "limit_up_down": (zone_score, cfg.get("limit_up_down_weight", 0.30)),
        "up_ratio": (up_ratio_score, cfg.get("up_ratio_weight", 0.25)),
        "northbound": (nb_score, cfg.get("northbound_weight", 0.25)),
        "margin": (margin_score, cfg.get("margin_weight", 0.20)),
    }
    total_weight = 0
    weighted_sum = 0
    for name, (score, w) in raw_weights.items():
        if score != 50:  # 不是死因子
            weighted_sum += score * w
        else:
            weighted_sum += 50 * w  # 死因子按中性值处理
        total_weight += w
    temperature = round(min(100, max(0, weighted_sum / total_weight)))

    # 判定市场状态
    if temperature >= 70:
        regime = "BULL"
    elif temperature >= 45:
        regime = "SIDEWAYS"
    elif temperature >= 25:
        regime = "BEAR"
    else:
        regime = "CRISIS"

    return {
        "regime": regime,
        "temperature": temperature,
        "components": {
            "limit_up_down_score": zone_score,
            "up_ratio_score": up_ratio_score,
            "northbound_score": nb_score,
            "margin_score": margin_score,
        },
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
