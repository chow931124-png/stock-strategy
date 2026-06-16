#!/usr/bin/env python3
"""
猎手引擎 v3.0 —— Web 仪表盘
============================
在浏览器中可视化展示扫描结果：
  - 🌡️ 市场温度计
  - 🏆 板块评分 + 轮动检测
  - 💎 三层精选信号
  - ⚡ 短线信号榜
  - 🔥 热门板块突破观察
  - 🚀 埋伏信号榜
  - 🛡️ 组合风控

用法:
  streamlit run web_dashboard.py
  # 或直接运行:
  python3 web_dashboard.py
"""

import os, sys, json, time
from datetime import datetime
from pathlib import Path
from collections import Counter

# 确保能找到 stock_strategy_v3.py
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ── 判断是否在 Streamlit 中运行 ──
try:
    import streamlit as st
    IN_STREAMLIT = True
except ImportError:
    IN_STREAMLIT = False

# ── 核心扫描函数（供Streamlit调用）──
def run_strategy():
    """导入策略模块并执行一次完整扫描，返回JSON序列化结果"""
    from stock_strategy_v3 import (
        MarketThermometer, SectorScorer, StockScorer, DataEngine,
        AutoSectorMapper, PolicyCatalystDetector, detect_sector_rotation,
        apply_self_learning_feedback, portfolio_risk_filter,
        get_nvda_change, get_fcx_change, CACHE_DIR,
        BUILTIN_STOCKS, SECTOR_PREFERENCE, STRATEGY,
    )

    result = {}

    # 1. 市场温度计
    thermo = MarketThermometer()
    temp = thermo.calc_temperature()
    result["temperature"] = {
        "score": temp["temperature"],
        "state": thermo.get_market_state(),
        "position_limit": thermo.get_position_limit(),
        "detail": {k: round(v, 2) if isinstance(v, float) else v
                   for k, v in temp.get("data", {}).items()},
    }

    # 2. 板块评分
    scorer = SectorScorer()
    sector_scores = scorer.score_sectors()
    result["sectors"] = dict(sorted(sector_scores.items(), key=lambda x: -x[1]))

    # 政策催化
    catalyst = PolicyCatalystDetector()
    cat = catalyst.detect()
    if cat:
        result["catalyst"] = cat
        for sec, cscore in cat.items():
            if sec in sector_scores:
                sector_scores[sec] = min(100, sector_scores[sec] + int(cscore * 0.3))

    # 自学习
    sector_scores = apply_self_learning_feedback(sector_scores)

    # 板块轮动
    rotation = detect_sector_rotation(sector_scores)
    if rotation.get("available"):
        result["rotation"] = {
            "gaining": [{"name": s, "delta": d, "score": sc} for s, d, sc in rotation.get("gaining", [])],
            "cooling": [{"name": s, "delta": d, "score": sc} for s, d, sc in rotation.get("cooling", [])],
        }

    # 3. 个股扫描
    de = DataEngine()
    stock_scorer = StockScorer()
    results = []
    all_scanned = []

    top_sectors = sorted(sector_scores.items(), key=lambda x: -x[1])[:5]
    allowed = SECTOR_PREFERENCE["prefer"] + SECTOR_PREFERENCE.get("neutral", [])
    stock_list = BUILTIN_STOCKS

    if temp["temperature"] < 30:
        stock_list = [(c, n, s) for c, n, s in stock_list if s in SECTOR_PREFERENCE["prefer"]]
    elif temp["temperature"] < 45:
        stock_list = [(c, n, s) for c, n, s in stock_list if s in allowed]

    total = len(stock_list)
    for i, (code, name, fixed_sector) in enumerate(stock_list):
        if code.startswith(('300', '301', '688')):
            continue
        quote = de.get_tencent_quote(code)
        stock_name = quote.get("name", name) or ""
        if any(kw in stock_name for kw in ['ST', '*ST', '退市']):
            continue
        df = de.get_klines(code)
        if df is None or len(df) < 120:
            continue
        row = df.iloc[-1]

        sector, blocks, src = AutoSectorMapper.get_sector(code, quote.get("name", ""))
        sector = sector if sector != "其他" else fixed_sector
        sec_score = sector_scores.get(sector, 50)

        # 计算
        score_result = stock_scorer.score(row, sec_score)
        short_info = stock_scorer.calc_short_term(row, sec_score)

        entry = {
            "code": code,
            "name": quote.get("name", name) or code,
            "sector": sector,
            "sector_score": sec_score,
            "price": round(float(row["close"]), 2),
            "drawdown": round(float(row["drawdown"]), 1) if pd.notna(row.get("drawdown")) else None,
            "vol_ratio": round(float(row["vol_ratio_20"]), 2) if pd.notna(row.get("vol_ratio_20")) else None,
            "short_score": short_info.get("short_score", 0) if short_info else 0,
            "short_reasons": short_info.get("short_reasons", "") if short_info else "",
        }

        if score_result:
            entry.update({
                "tier": score_result["tier"],
                "composite": score_result["composite"],
                "final_sort": score_result.get("final_sort", 0),
                "surge_score": score_result.get("surge_score", {}).get("score", 0),
                "ambush_score": score_result.get("ambush_score", 0),
                "kelly_pct": score_result.get("kelly_pct", 0.15),
                "atr_stop_pct": score_result.get("atr_stop_pct", 0.08),
                "verify": score_result.get("verify", {}).get("level", ""),
            })
            results.append(entry)

        all_scanned.append(entry)
        time.sleep(random.uniform(0.05, 0.1))

    # 排序
    results.sort(key=lambda r: -r.get("final_sort", r.get("composite", 0)))

    # 风控
    old_count = len(results)
    results = portfolio_risk_filter(results, max_total_positions=8)

    # 热门板块突破
    hot_breakout = []
    result_codes = {r["code"] for r in results}
    for s in all_scanned:
        if s["code"] not in result_codes:
            all_results_list = list(results) + [s]
    for r in all_scanned:
        if r.get("tier", "无信号") == "无信号" and r.get("sector_score", 0) >= 80 and r.get("short_score", 0) >= 60:
            hot_breakout.append(r)

    result["signals"] = results
    result["hot_breakout"] = sorted(hot_breakout, key=lambda x: -x["short_score"])[:5]
    result["total_scanned"] = len(all_scanned)
    result["total_with_signals"] = len(results)
    result["timestamp"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    return result


# ── Streamlit UI ──
def render_dashboard():
    st.set_page_config(
        page_title="猎手引擎 v3.0",
        page_icon="🎯",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    st.title("🎯 猎手引擎 v3.0 — A股回调低吸策略")
    st.caption("数据源: 同花顺热点 | 东财板块 | mootdx K线 | 腾讯财经")

    # 侧边栏
    with st.sidebar:
        st.header("控制面板")
        if st.button("🔄 执行全量扫描", type="primary", use_container_width=True):
            with st.spinner("扫描进行中..."):
                result = run_strategy()
                st.session_state["scan_result"] = result
                st.session_state["scan_time"] = datetime.now().strftime("%H:%M:%S")
                st.rerun()

        if st.button("🧹 清除缓存", use_container_width=True):
            from stock_strategy_v3 import AutoSectorMapper
            AutoSectorMapper.clear_cache()
            st.success("缓存已清除")

        if "scan_time" in st.session_state:
            st.divider()
            st.info(f"⏱️ 上次扫描: {st.session_state['scan_time']}")
            st.caption("点击上方按钮重新扫描")

    # 检查是否有数据
    if "scan_result" not in st.session_state:
        st.info("👈 点击左侧「执行全量扫描」开始分析")
        return

    data = st.session_state["scan_result"]

    # ─── 顶部概览 ───
    col1, col2, col3, col4 = st.columns(4)
    t = data["temperature"]
    col1.metric("🌡️ 市场温度", f"{t['score']}/100", t["state"])
    col2.metric("📊 建议仓位", f"{t['position_limit']*100:.0f}%")
    col3.metric("📈 有效信号", data["total_with_signals"])
    col4.metric("🔍 扫描范围", data["total_scanned"])

    # ─── 温度详情 ───
    with st.expander("🌡️ 温度计详情", expanded=False):
        detail = t.get("detail", {})
        cols = st.columns(len(detail))
        for i, (k, v) in enumerate(detail.items()):
            cols[i].metric(k, v)

    # ─── 板块评分 + 轮动 ───
    st.subheader("🏆 板块评分")
    secs = data.get("sectors", {})
    rotation = data.get("rotation")

    # 板块评分条
    top5 = sorted(secs.items(), key=lambda x: -x[1])[:10]
    for name, score in top5:
        pct = score / 100
        color = "green" if score >= 70 else ("orange" if score >= 50 else "red")
        st.markdown(f"**{name}** ({score}分)")
        st.progress(pct, text=f"{score}分")

    # 板块轮动
    if rotation:
        st.subheader("🔄 板块轮动检测")
        gcols = st.columns(2)
        with gcols[0]:
            st.markdown("**🔥 资金流入**")
            for item in rotation.get("gaining", []):
                st.markdown(f"- {item['name']} **+{item['delta']}** → {item['score']}分")
        with gcols[1]:
            st.markdown("**🧊 资金流出**")
            for item in rotation.get("cooling", []):
                st.markdown(f"- {item['name']} **{item['delta']}** → {item['score']}分")

    # ─── 信号展示 ───
    signals = data.get("signals", [])
    hot_breakout = data.get("hot_breakout", [])

    if signals:
        st.subheader("💎 策略信号")

        # 按层级分 tab
        tab_names = []
        tab_data = []
        for tn in ["💎 精选层", "🥈 增强层", "🥉 普通层"]:
            grp = [r for r in signals if r.get("tier") == tn]
            if grp:
                tab_names.append(tn)
                tab_data.append(grp)

        if tab_data:
            tabs = st.tabs(tab_names)
            for i, tdata in enumerate(tab_data):
                with tabs[i]:
                    for r in tdata:
                        cols = st.columns([2, 1, 1, 1, 1, 1])
                        cols[0].markdown(f"**{r['name']}** ({r['code']})")
                        cols[1].markdown(f"综合 {r.get('composite', 0)}")
                        cols[2].markdown(f"板块 {r.get('sector_score', 0)}")
                        cols[3].markdown(f"短线 {r.get('short_score', 0)}")
                        s = r.get('surge_score', 0)
                        cols[4].markdown(f"🔥{s}" if s >= 30 else "")
                        cols[5].markdown(f"凯利 {r.get('kelly_pct', 0.15)*100:.0f}%")

        # 重点 TOP3
        st.subheader("🎯 重点观察 TOP 3")
        top3 = signals[:3]
        for r in top3:
            with st.container(border=True):
                cols = st.columns([2, 1, 1, 1, 2])
                cols[0].markdown(f"**{r['name']}** ({r['code']})")
                cols[1].markdown(f"📊 {r.get('composite', 0)}分")
                cols[2].markdown(f"📉 回撤 {r.get('drawdown', 0):+.0f}%")
                cols[3].markdown(f"🏆 {r.get('sector_score', 0)}分")
                stop = r.get('atr_stop_pct', 0.08) * 100
                cols[4].markdown(f"🛑 止损-{stop:.0f}% | 持有≤20日")

    # 热门板块突破
    if hot_breakout:
        st.subheader("🔥 热门板块突破观察")
        cols = st.columns(len(hot_breakout))
        for i, h in enumerate(hot_breakout):
            with cols[i]:
                with st.container(border=True):
                    st.metric(h["name"], f"{h['short_score']}分", h["sector"])
                    st.caption(h.get("short_reasons", ""))

    # 风控
    if signals:
        sectors_in_signals = Counter(r.get("sector", "其他") for r in signals)
        st.subheader("🛡️ 组合风控")
        st.markdown(f"总信号: **{len(signals)}** 只 | 最多 **8** 只 | 同行业最多 **2** 只")
        st.json(dict(sectors_in_signals.most_common()))

    # ── 底部 ──
    st.divider()
    st.caption(f"猎手引擎 v3.0 | {data['timestamp']} | ⚠️ 仅供参考，不构成投资建议")


# ── 入口 ──
if __name__ == "__main__":
    if IN_STREAMLIT:
        render_dashboard()
    else:
        print("请使用 streamlit run web_dashboard.py 启动")
        print("或者: python3 -m streamlit run web_dashboard.py")
        print("\n--- 快速调用扫描（测试用）---")
        import random, pandas as pd
        from stock_strategy_v3 import pd  # 确保 pd 可用
        result = run_strategy()
        print(f"扫描完成: {result['total_with_signals']} 个信号, {len(result['hot_breakout'])} 个突破观察")
        if not IN_STREAMLIT:
            # 输出JSON到文件
            output_path = Path(__file__).parent / "scan_result.json"
            output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            print(f"结果已保存到: {output_path}")
