"""逐笔成交数据 — Level-2 分析

数据源: mootdx client.transaction()（免费，不封IP）
用途: 判断主力动向、尾盘资金、挂单质量
"""
import numpy as np
import pandas as pd
from mootdx.quotes import Quotes
from datetime import datetime


def get_tick_trades(code: str, date: str = None) -> pd.DataFrame:
    """
    获取单只股票的逐笔成交数据

    返回: DataFrame [time, price, vol, buyorsell]
        buyorsell: 0=买, 1=卖, 2=中性
    """
    if date is None:
        date = datetime.now().strftime("%Y%m%d")
    client = Quotes.factory(market='std')
    df = client.transaction(symbol=code, date=date)
    if df is not None and not df.empty:
        return df
    return pd.DataFrame()


def analyze_tick(code: str, date: str = None) -> dict:
    """
    逐笔成交深度分析

    返回:
        {big_buy_vol, big_sell_vol, net_big, buy_sell_ratio,
         last_30min_big_net, last_30min_dir, suspicious}
    """
    df = get_tick_trades(code, date)
    if df.empty:
        return {}

    result = {}

    # 总成交量
    total_buy = df[df["buyorsell"] == 0]["vol"].sum()
    total_sell = df[df["buyorsell"] == 1]["vol"].sum()
    total = total_buy + total_sell
    result["total_vol"] = int(total)
    result["buy_sell_ratio"] = round(total_buy / total_sell, 2) if total_sell > 0 else 99

    # 大单分析（vol以手为单位：>200手 ≈ 20-30万元 = 主力行为）
    big_buy = df[(df["buyorsell"] == 0) & (df["vol"] > 200)]
    big_sell = df[(df["buyorsell"] == 1) & (df["vol"] > 200)]
    big_buy_vol = int(big_buy["vol"].sum())
    big_sell_vol = int(big_sell["vol"].sum())
    result["big_buy_vol"] = big_buy_vol
    result["big_sell_vol"] = big_sell_vol
    result["big_net"] = big_buy_vol - big_sell_vol
    result["big_count"] = len(big_buy) + len(big_sell)

    # 特大单（>2000手 ≈ 200-300万元 = 机构/游资）
    super_buy = df[(df["buyorsell"] == 0) & (df["vol"] > 2000)]
    super_sell = df[(df["buyorsell"] == 1) & (df["vol"] > 2000)]
    result["super_net"] = int(super_buy["vol"].sum()) - int(super_sell["vol"].sum())
    result["super_count"] = len(super_buy) + len(super_sell)

    # 尾盘 30 分钟大单方向（14:30-15:00）
    if "time" in df.columns and not df.empty:
        df_time = df.copy()
        # 取最后 30 分钟的成交
        all_times = sorted(df_time["time"].unique())
        if len(all_times) > 0:
            cutoff = all_times[-30] if len(all_times) >= 30 else all_times[0]
            last30 = df_time[df_time["time"] >= cutoff]
            last30_buy = last30[last30["buyorsell"] == 0]["vol"].sum()
            last30_sell = last30[last30["buyorsell"] == 1]["vol"].sum()
            last30_net = last30_buy - last30_sell

            result["last30_net"] = int(last30_net)
            # 尾盘大单
            last30_big = last30[(last30["vol"] > 200)]
            lb_buy = int(last30_big[last30_big["buyorsell"] == 0]["vol"].sum())
            lb_sell = int(last30_big[last30_big["buyorsell"] == 1]["vol"].sum())
            result["last30_big_net"] = lb_buy - lb_sell

            if last30_net > 200:
                result["last30_dir"] = "strong_buy"
            elif last30_net > 50:
                result["last30_dir"] = "buy"
            elif last30_net < -200:
                result["last30_dir"] = "strong_sell"
            elif last30_net < -50:
                result["last30_dir"] = "sell"
            else:
                result["last30_dir"] = "neutral"
        else:
            result["last30_dir"] = "neutral"
    else:
        result["last30_dir"] = "neutral"

    # 异常检测
    suspicious = []
    if result.get("big_net", 0) < -1000:
        suspicious.append("主力大幅净卖出")
    if result.get("last30_big_net", 0) < -300:
        suspicious.append("尾盘大单砸盘")
    if result.get("last30_big_net", 0) > 300:
        suspicious.append("尾盘大单抢筹")
    if result.get("super_count", 0) > 5 and result.get("super_net", 0) > 1000:
        suspicious.append("机构密集买入")
    if result.get("super_count", 0) > 5 and result.get("super_net", 0) < -1000:
        suspicious.append("机构密集卖出")

    # ═══════════════════════════════════════════
    # 大单频率分析（判断主力是在持续买入还是偶尔一单）
    # ═══════════════════════════════════════════
    if "time" in df.columns and len(df) >= 10:
        df_t = df[df["buyorsell"].isin([0, 1])].copy()
        if len(df_t) >= 10:
            # 每分钟大单笔数
            df_t["minute"] = df_t["time"].str[:5]
            minute_groups = df_t.groupby("minute")
            big_per_minute = []
            for m, g in minute_groups:
                big_count = len(g[g["vol"] > 50])
                big_net = g[g["vol"] > 50][g["buyorsell"] == 0]["vol"].sum() - g[g["vol"] > 50][g["buyorsell"] == 1]["vol"].sum()
                if big_count > 0:
                    big_per_minute.append({"minute": m, "count": big_count, "net": big_net})

            if big_per_minute:
                minutes_with_big = len(big_per_minute)
                total_big_minutes = len(minute_groups)
                # 大单出现的分钟占比
                big_freq_ratio = minutes_with_big / total_big_minutes if total_big_minutes > 0 else 0
                # 持续净买入的分钟数
                buy_minutes = sum(1 for b in big_per_minute if b["net"] > 0)
                sell_minutes = sum(1 for b in big_per_minute if b["net"] < 0)
                buy_ratio = buy_minutes / (buy_minutes + sell_minutes) if (buy_minutes + sell_minutes) > 0 else 0

                result["big_freq_ratio"] = round(big_freq_ratio, 2)
                result["big_buy_minutes"] = buy_minutes
                result["big_sell_minutes"] = sell_minutes
                result["big_minute_buy_ratio"] = round(buy_ratio, 2)

                # 判断
                if buy_ratio > 0.7 and big_freq_ratio > 0.3:
                    result["big_freq_signal"] = "持续买入"
                elif buy_ratio < 0.3 and big_freq_ratio > 0.3:
                    result["big_freq_signal"] = "持续卖出"
                elif big_freq_ratio < 0.1:
                    result["big_freq_signal"] = "零星成交"
                else:
                    result["big_freq_signal"] = "博弈"

    result["suspicious"] = suspicious
    return result


def format_tick_summary(analysis: dict) -> str:
    """格式化逐笔分析摘要"""
    if not analysis:
        return "逐笔数据: 无"

    parts = []
    net = analysis.get("big_net", 0)
    direction = "资金净流入" if net > 0 else "净流出"
    parts.append(f"主力{abs(net):.0f}万{direction}")

    last30 = analysis.get("last30_dir", "neutral")
    dir_map = {"strong_buy": "尾盘抢筹↑", "buy": "尾盘偏多",
               "strong_sell": "尾盘砸盘↓", "sell": "尾盘偏空",
               "neutral": "尾盘中性"}
    parts.append(dir_map.get(last30, ""))

    susp = analysis.get("suspicious", [])
    if susp:
        parts.append("⚠️" + "|".join(susp[:2]))

    return " | ".join(parts)
