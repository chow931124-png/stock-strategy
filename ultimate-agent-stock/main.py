#!/usr/bin/env python3
"""
究极 Agent 选股系统 — CLI 入口

用法:
    python main.py briefing       # 盘前情报简报 (7:30)
    python main.py scan           # 收盘后选股扫描 (15:30)
    python main.py noon           # 午间速览 (12:30)
    python main.py daily          # 日报汇总 (21:00)
    python main.py auto           # 简报 + 选股 + 持仓
    python main.py positions      # 持仓检查
    python main.py push --time morning  # 定时推送(配合cron)
    python main.py web            # 启动 Web Dashboard
    python main.py report         # 绩效报告
    python main.py status         # 系统状态
"""
import sys
import asyncio
import argparse
from pathlib import Path
from datetime import datetime

# 确保项目根目录在 sys.path 中
ROOT_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(ROOT_DIR))

from config import get_config


def main():
    parser = argparse.ArgumentParser(
        description="究极 Agent 选股系统",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
    python main.py briefing       盘前情报简报 (7:30)
    python main.py scan           收盘后选股扫描 (15:30)
    python main.py positions      持仓检查
    python main.py push --time morning  定时推送(配合cron)
    python main.py report         绩效报告
    python main.py track          信号回溯（历史推荐表现）
    python main.py calibrate      参数校准
    python main.py web            启动 Web Dashboard
        """,
    )
    parser.add_argument("command", nargs="?", default="status",
                       help="briefing|scan|auto|push|noon|daily|positions|web|report|calibrate|status")
    parser.add_argument("--port", type=int, default=8080, help="Web Dashboard 端口")
    parser.add_argument("--code", type=str, default="", help="指定股票代码")
    parser.add_argument("--mode", type=str, default="auto",
                       choices=["auto", "fast", "close", "scan", "full", "light", "deep"],
                       help="扫描模式: fast(盘中快扫) close(尾盘) scan(收盘) auto(自动)")
    parser.add_argument("--time", type=str, default="morning",
                       choices=["0800", "0945", "1130", "1440", "1700", "2030"], help="推送时段")
    parser.add_argument("--push", action="store_true", help="推送结果到微信")

    args = parser.parse_args()

    config = get_config()
    print(f"📊 ultimate-agent-stock v{config.get('app', {}).get('version', '0.1.0')}")
    print(f"   运行模式: {args.command}")
    print(f"   时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()

    if args.command == "briefing":
        asyncio.run(cmd_briefing(args))
    elif args.command == "scan":
        asyncio.run(cmd_scan(args))
    elif args.command == "noon":
        asyncio.run(cmd_noon(args))
    elif args.command == "daily":
        asyncio.run(cmd_daily(args))
    elif args.command == "push":
        asyncio.run(cmd_push(args))
    elif args.command == "auto":
        asyncio.run(cmd_auto(args))
    elif args.command == "positions":
        asyncio.run(cmd_positions(args))
    elif args.command == "web":
        cmd_web(args)
    elif args.command == "report":
        asyncio.run(cmd_report(args))
    elif args.command == "calibrate":
        asyncio.run(cmd_calibrate(args))
    elif args.command == "backtest":
        asyncio.run(cmd_backtest(args))
    elif args.command == "track":
        asyncio.run(cmd_track(args))
    elif args.command == "status":
        cmd_status(args)
    else:
        parser.print_help()


# ── 情报简报 ──────────────────────────────────────
async def cmd_briefing(args):
    print("🔍 正在生成市场情报简报...\n")

    from intelligence.oversea_scanner import OverseaScanner, summary_oversea
    from intelligence.premarket_hotspots import PremarketHotspots
    from intelligence.policy_radar import PolicyRadar
    from intelligence.sector_radar import SectorRadar
    from intelligence.portfolio_intel import PortfolioIntel
    from intelligence.briefing_writer import BriefingWriter
    from agent_orch.context import reset_intel_context

    reset_intel_context()

    agents = [
        OverseaScanner(),
        PremarketHotspots(),
        PolicyRadar(),
        SectorRadar(),
        PortfolioIntel(),
    ]

    # 加载持仓数据
    import json
    positions_path = ROOT_DIR / "data_store" / "positions.json"
    positions = []
    if positions_path.exists():
        with open(positions_path) as f:
            positions = json.load(f)
        print(f"  📋 已加载 {len(positions)} 笔持仓")
    else:
        print(f"  📋 无持仓数据")

    # 运行所有情报 Agent
    context = {
        "positions": positions,
    }

    for agent in agents:
        try:
            print(f"  ⏳ {agent.name}...", end=" ", flush=True)
            result = await agent.run(context)
            print(f"✅")
        except Exception as e:
            print(f"❌ {e}")

    # 生成简报
    writer = BriefingWriter()
    briefing = writer.generate()
    print("\n" + briefing)

    # 推送短版
    short = writer.generate_short()
    print("\n\n📱 推送版本:")
    print(short)

    # 微信推送（--push 标志或自动模式）
    if getattr(args, 'push', False):
        from phase5_push.wechat_push import wechat_push
        from phase5_push.push_templates import briefing_push
        title, content = briefing_push(briefing)
        ok = wechat_push(title, content)
        if ok:
            print(f"  ✅ 已推送微信: {title}")
        else:
            print(f"  ❌ 微信推送失败")

    return briefing


# ── 选股扫描 ──────────────────────────────────────
async def cmd_scan(args):
    # ── 进度报告（供 Web 轮询） ──
    def _report(step, pct, detail=""):
        try:
            pf = Path(__file__).parent / "data_store" / "scan_progress.json"
            pf.parent.mkdir(parents=True, exist_ok=True)
            pf.write_text(json.dumps({"step": step, "pct": pct, "detail": detail}))
        except Exception:
            pass

    print("🔍 正在执行全市场选股扫描...\n")

    # Phase 1: 市场状态检测
    _report("市场状态检测", 5)
    print("  📊 检测市场状态...")
    from phase1_scan.market_regime import detect_market_regime
    regime = detect_market_regime()
    print(f"     市场温度: {regime.get('temperature', 50)}/100")
    print(f"     市场状态: {regime.get('regime', '?')}")

    # 🔥 全市场实时扫描
    _report("全市场初筛", 10)
    print("  🔎 实时全市场扫描（排除 688/300/ST/低价/低成交）...")
    from data.market_scanner import scan_full_market
    candidates = scan_full_market(min_price=5, min_amount_wan=5000, max_stocks=300)
    if not candidates:
        from phase1_scan.pre_filter import pre_filter
        candidates = pre_filter()
        print(f"     备用候选池: {len(candidates)} 只")
    else:
        print(f"     基础候选池: {len(candidates)} 只")

    # iwencai 动态扩展
    _report("iwencai扩展", 15)
    print("  🚀 iwencai 全市场扫描...")
    from phase1_scan.screeners.iwencai_screener import IwencaiScreener
    iwencai = IwencaiScreener()
    iwencai_new = await iwencai.run({"candidates": candidates, "market_context": regime})
    iwencai_codes = iwencai_new.get("selected", [])
    expanded_pool = list(set(candidates + iwencai_codes))
    print(f"     iwencai新增: {len(iwencai_codes)} 只 (总候选: {len(expanded_pool)} 只)")

    # 运行所有扫描器
    _report("运行扫描器", 20)
    print("  🏃 运行扫描器...")
    from phase1_scan.screeners.pullback_screener import PullbackScreener
    from phase1_scan.screeners.breakout_screener import BreakoutScreener
    from phase1_scan.screeners.momentum_screener import MomentumScreener
    from phase1_scan.screeners.ths_hot_screener import THSHotScreener
    from phase1_scan.screeners.ambush_screener import AmbushScreener
    from phase1_scan.screeners.short_term_trader import ShortTermTrader

    import asyncio
    pullback = PullbackScreener()
    breakout = BreakoutScreener()
    momentum = MomentumScreener()
    ths = THSHotScreener()
    ambush = AmbushScreener()
    # 确定扫描模式
    scan_mode = getattr(args, 'mode', 'auto')
    if scan_mode == 'auto':
        hr = datetime.now().hour
        if 9 <= hr < 14:
            scan_mode = 'fast'
        elif 14 <= hr < 15:
            scan_mode = 'close'
        else:
            scan_mode = 'scan'
    elif scan_mode == 'scan':
        scan_mode = 'scan'
    elif scan_mode in ('full', 'light', 'deep'):
        scan_mode = 'close'  # 旧参数兼容

    trader = ShortTermTrader()
    trader._scan_mode = scan_mode  # 传给 screen()

    pool_ctx = {"candidates": expanded_pool, "market_context": regime}
    all_results = await asyncio.gather(
        pullback.run(pool_ctx),
        breakout.run(pool_ctx),
        momentum.run(pool_ctx),
        ths.run(pool_ctx),
        ambush.run(pool_ctx),
        trader.run(pool_ctx),
    )

    pb_selected = all_results[0].get("selected", [])
    br_selected = all_results[1].get("selected", [])
    mo_selected = all_results[2].get("selected", [])
    ths_selected = all_results[3].get("selected", [])
    am_selected = all_results[4].get("selected", [])
    trader_selected = all_results[5].get("selected", [])

    print(f"     PullbackScreener:  {len(pb_selected)} 只")
    print(f"     BreakoutScreener:  {len(br_selected)} 只")
    print(f"     MomentumScreener:  {len(mo_selected)} 只")
    print(f"     THSHotScreener:    {len(ths_selected)} 只")
    print(f"     AmbushScreener:    {len(am_selected)} 只")
    _report("扫描器完成", 35, f"候选{len(ths_selected)+len(pb_selected)+len(br_selected)+len(mo_selected)+len(am_selected)+len(trader_selected)}只")

    # 合并候选池（去重 + 🚫 硬排688/300）
    _report("合并候选池", 40)
    from data.reference_data import is_excluded_board
    raw_pool = set(pb_selected + br_selected + mo_selected + ths_selected + am_selected + trader_selected + iwencai_codes)
    pool = [c for c in raw_pool if not is_excluded_board(c)]
    if not pool:
        pool = [c for c in candidates if not is_excluded_board(c)][:30]
    excluded = len(raw_pool) - len(pool)
    print(f"     最终候选池: {len(pool)} 只{' (已排除'+str(excluded)+'只688/300)' if excluded else ''}")

    # 🔥 题材归因匹配
    print("  🏷️ 题材归因匹配...")
    _report("题材归因", 45)
    from data.theme_matcher import ThemeMatcher
    matcher = ThemeMatcher()
    matcher.update_from_ths()
    matcher.update_from_intel()
    top_themes = matcher.get_top_themes(5)
    if top_themes:
        theme_strs = []
        for t in top_themes:
            theme_strs.append(f"{t['theme']}({t['intensity']})")
        print(f"     热点题材: {' | '.join(theme_strs)}")
    # 用 iwencai 补充热点题材的股票（也硬排688/300）
    theme_new_codes = matcher.find_missing_stocks(set(pool))
    theme_new_codes = [c for c in theme_new_codes if not is_excluded_board(c)]
    if theme_new_codes:
        print(f"     题材补充: +{len(theme_new_codes)} 只")
        pool = list(set(pool + theme_new_codes))

    # 构建扫描器得分索引
    screener_results = {}
    for code in set(pb_selected + br_selected + mo_selected + ths_selected + am_selected + trader_selected + iwencai_codes + theme_new_codes):
        if is_excluded_board(code):
            continue
        score = 0
        if code in pb_selected: score += 20
        if code in br_selected: score += 15
        if code in mo_selected: score += 15
        if code in ths_selected: score += 25
        if code in am_selected: score += 20
        if code in trader_selected: score += 30  # 短线博弈权重最高
        screener_results[code] = min(70, score)

    screener_list = [{"code": k, "score": v} for k, v in screener_results.items()]

    # 获取热点题材（供评分引擎使用）
    hot_themes = ths.last_themes if hasattr(ths, 'last_themes') else {}
    # 加上题材匹配引擎的得分
    theme_scores = {code: matcher.score_stock(code) for code in pool}
    for item in screener_list:
        code = item["code"]
        item["theme_score"] = theme_scores.get(code, 0)

    # Phase 3: 三框架选股
    _report("三框架评分", 55)
    print("\n  📋 三框架选股评分...")
    from data.market_data import tencent_quote
    from phase3_portfolio.three_frame_scorer import ThreeFrameScorer
    from agent_orch.context import get_intel_context

    quotes = tencent_quote(pool)
    print(f"     行情数据: {len(quotes)} 只")

    intel = get_intel_context()
    scorer = ThreeFrameScorer()
    portfolio = scorer.score_all(
        pool, regime, intel_context=intel,
        screener_results=screener_list, pre_quotes=quotes,
        hot_themes=hot_themes, theme_scores=theme_scores,
    )

    print()
    _print_portfolio(portfolio)
    trader.print_setups()

    # 🔗 产业链追踪
    try:
        from phase1_scan.screeners.chain_tracker import ChainTracker
        chain = ChainTracker()
        await chain.run({"candidates": [], "market_context": regime})
        chain.print_report()
        # 保存到文件供 Web 读取
        if hasattr(chain, "last_reports") and chain.last_reports:
            import json as _j
            cf = Path(__file__).parent / "data_store" / "latest_chain_reports.json"
            with open(cf, "w") as _pf:
                _j.dump(chain.last_reports[:10], _pf, ensure_ascii=False, indent=2, default=str)
    except Exception:
        pass

    # 🔬 LLM 分析师验证（按优先级调用）
    _report("分析师验证", 75)
    print("\n  🔬 LLM 分析师验证...")
    try:
        top_codes = []
        for sp in portfolio.short_term: top_codes.append(sp.code)

        if top_codes:
            from phase2_analysis.analyst_factory import run_analysts
            # 短期爆发调用全部3个分析师，中期/长期只调风险
            analyst_results = {}
            for code in top_codes:
                results = await run_analysts([code], regime)
                if code in results:
                    analyst_results[code] = results[code]

            # 中期只调风险分析师
            from phase2_analysis.analysts.risk import RiskAnalyst
            risk = RiskAnalyst()
            for sp in portfolio.medium_term:
                if sp.code not in analyst_results:
                    report = await risk.analyze(sp.code, regime)
                    analyst_results[sp.code] = {"RiskAnalyst": report}

            portfolio._analyst_reports = analyst_results

            for code, reports in analyst_results.items():
                name = ""
                for sp in (portfolio.short_term + portfolio.medium_term + portfolio.long_term):
                    if sp.code == code:
                        name = sp.name
                        break
                for atype, report in reports.items():
                    if report and report.reasoning and len(report.reasoning) > 20:
                        label = {"PriceMoneyAnalyst": "技术", "ValueMoatAnalyst": "价值", "RiskAnalyst": "风控"}
                        lbl = label.get(atype, atype)
                        print(f"     {code} {name} [{lbl}]: {report.reasoning[:60]}")
    except Exception:
        print("     （分析师调用跳过）")

    # 记录到信号回溯系统 + 更新历史推荐表现
    _report("保存结果", 90)
    try:
        from self_learn.signal_tracker import record_scan, update_tracking
        n = record_scan(portfolio, regime)
        updated = update_tracking()
        print(f"\n  📝 已记录 {n} 条新推荐，更新 {updated} 条历史跟踪")
    except Exception:
        pass

    # 持久化选股结果到文件（供 Web 读取）
    _report("持久化", 95)
    try:
        import json
        from pathlib import Path
        f = Path(__file__).parent / "data_store" / "latest_portfolio.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        with open(f, "w") as pf:
            json.dump({
                "market_regime": portfolio.market_regime,
                "market_temperature": portfolio.market_temperature,
                "short_term": [{"code": s.code, "name": s.name, "score": s.score, "current_price": s.current_price, "change_pct": s.change_pct, "last_close": s.last_close,
                                "reason": s.reason,
                                "stop_loss": s.stop_loss, "target_price": s.target_price,
                                "expected_hold_days": s.expected_hold_days,
                                "score_by_analyst": s.score_by_analyst,
                                "entry_zone": list(s.entry_zone) if s.entry_zone else None}
                               for s in portfolio.short_term],
                "medium_term": [{"code": s.code, "name": s.name, "score": s.score, "current_price": s.current_price, "change_pct": s.change_pct, "last_close": s.last_close,
                                 "reason": s.reason, "score_by_analyst": s.score_by_analyst}
                                for s in portfolio.medium_term],
                "long_term": [{"code": s.code, "name": s.name, "score": s.score, "current_price": s.current_price, "change_pct": s.change_pct, "last_close": s.last_close,
                               "reason": s.reason, "score_by_analyst": s.score_by_analyst}
                              for s in portfolio.long_term],
            }, pf, ensure_ascii=False, indent=2)

        # 保存短线交易信号
        import json as _j
        if hasattr(trader, 'last_setups') and trader.last_setups:
            sf = Path(__file__).parent / "data_store" / "latest_trader_setups.json"
            with open(sf, "w") as pf:
                _j.dump(trader.last_setups[:10], pf, ensure_ascii=False, indent=2, default=str)

        # 另存分析师报告
        analyst_reports = getattr(portfolio, '_analyst_reports', {})
        if analyst_reports:
            af = Path(__file__).parent / "data_store" / "latest_analyst_reports.json"
            with open(af, "w") as pf:
                json.dump({
                    code: {
                        atype: {
                            "reasoning": getattr(r, "reasoning", ""),
                            "score": getattr(r, "score", 50),
                            "signal": getattr(r, "signal", "NEUTRAL"),
                        }
                        for atype, r in reports.items() if r
                    }
                    for code, reports in analyst_reports.items()
                }, pf, ensure_ascii=False, indent=2)
    except Exception:
        pass

    _report("完成", 100, "")
    return portfolio


def _print_portfolio(portfolio):
    """格式化输出三框架投资组合（增强版）"""
    from datetime import datetime
    analyst_reports = getattr(portfolio, '_analyst_reports', {})

    print(f"📊 {datetime.now().strftime('%Y-%m-%d')} 选股结果")
    print(f"   市场: {portfolio.market_regime}  温度: {portfolio.market_temperature}")
    print()

    for title, frame, picks in [
        ("🔥 短期爆发 (3-10天)", "short_term", portfolio.short_term),
        ("📈 中期趋势 (10-30天)", "medium_term", portfolio.medium_term),
        ("🌳 长期持有 (3个月+)", "long_term", portfolio.long_term),
    ]:
        print(f"  {title}")
        print("  ─────────────────────")
        if picks:
            for i, sp in enumerate(picks, 1):
                bar = "█" * int(sp.score / 10) + "░" * (10 - int(sp.score / 10))
                print(f"  {i}. {sp.code} {sp.name:8s}  得分 {sp.score}  {bar}")

                dims = []
                if sp.score_by_analyst:
                    for k, v in sp.score_by_analyst.items():
                        dims.append(f"{k} {v}")
                if dims:
                    print(f"     {' | '.join(dims)}")
                if sp.reason:
                    print(f"     🏷️ {sp.reason}")

                if analyst_reports and sp.code in analyst_reports:
                    for atype, r in analyst_reports[sp.code].items():
                        if r and r.reasoning and len(r.reasoning) > 20:
                            label = {"PriceMoneyAnalyst": "技术", "ValueMoatAnalyst": "价值", "RiskAnalyst": "风控"}
                            lbl = label.get(atype, atype)
                            print(f"     [{lbl}] {r.reasoning[:120]}")

                if frame == "short_term" and sp.entry_zone:
                    print(f"     入场 {sp.entry_zone[0]:.2f}-{sp.entry_zone[1]:.2f}  止损 {sp.stop_loss}  目标 {sp.target_price}  持有 {sp.expected_hold_days}天")

                # 行情信息
                if sp.last_close and sp.current_price:
                    chg = sp.change_pct or (sp.current_price - sp.last_close) / sp.last_close * 100
                    print(f"     昨收 {sp.last_close:.2f} → 现 {sp.current_price:.2f}  ({chg:+.2f}%)")
        else:
            print("     (暂无)")
        print()



# ── 全自动模式 ────────────────────────────────────
async def cmd_auto(args):
    print("🚀 全自动模式: 情报简报 → 选股扫描 → 持仓管理\n")

    # 先跑简报
    briefing = await cmd_briefing(args)
    print("\n" + "=" * 60 + "\n")

    # 再跑选股（简报的 intel context 会自动传递给选股模块）
    scan_result = await cmd_scan(args)
    print("\n" + "=" * 60 + "\n")

    # 持仓检查
    print("📋 持仓操作建议")
    print("─" * 30)
    from phase4_position.position_store import load_positions
    from phase4_position.position_trader import generate_suggestions
    positions = load_positions()
    if positions:
        suggestions = generate_suggestions(positions)
        for s in suggestions:
            emoji = {"HOLD": "➖", "ADD": "➕", "REDUCE": "🔻",
                     "TAKE_PROFIT": "⭐", "STOP_LOSS": "🚨", "CLOSE": "❌"}.get(s.action, "➖")
            bar = "█" * min(int(abs(s.change_pct)/2), 10) + "░" * (10 - min(int(abs(s.change_pct)/2), 10))
            print(f"  {emoji} {s.code} {s.name:8s}  {s.change_pct:+.1f}% {bar}  {s.action}")
        dangers = sum(1 for x in suggestions if x.action in ("STOP_LOSS", "CLOSE"))
        warns = sum(1 for x in suggestions if x.action in ("REDUCE", "TAKE_PROFIT"))
        print(f"  🚨 {dangers}笔止损  ⚠️ {warns}笔关注")
    else:
        print("  (无持仓数据)")

    print(f"\n✅ 全自动模式完成 ({datetime.now().strftime('%H:%M:%S')})")


# ── 持仓检查 ──────────────────────────────────────
async def cmd_positions(args):
    print("📋 持仓检查\n")
    from phase4_position.position_store import load_positions
    from phase4_position.position_trader import generate_suggestions

    positions = load_positions()
    if not positions:
        print("   (无持仓数据)")
        return

    print(f"  共 {len(positions)} 笔持仓\n")

    suggestions = generate_suggestions(positions)

    action_emoji = {
        "HOLD": "➖", "ADD": "➕", "REDUCE": "🔻",
        "TAKE_PROFIT": "⭐", "STOP_LOSS": "🚨", "CLOSE": "❌",
    }
    urgency_label = {
        "NOW": "立即", "TODAY": "今日", "THIS_WEEK": "本周", "OBSERVE": "观察",
    }

    for s in suggestions:
        emoji = action_emoji.get(s.action, "➖")
        bar_len = int(abs(s.change_pct) / 2)
        bar = "█" * min(bar_len, 10) + "░" * (10 - min(bar_len, 10))
        color = "🟢" if s.change_pct >= 0 else "🔴"
        print(f"  {emoji} {s.code} {s.name:8s}  {color} {s.change_pct:+.1f}% {bar}")
        print(f"     {s.action:12s} · {urgency_label.get(s.urgency, s.urgency)}")
        print(f"     {s.reason}")
        if s.stop_loss_price:
            print(f"     止损: {s.stop_loss_price}")
        if s.take_profit_price:
            print(f"     止盈: {s.take_profit_price}")
        if s.quantity_change_pct is not None and s.quantity_change_pct < 0:
            print(f"     建议减仓: {abs(s.quantity_change_pct):.0f}%")
        print()

    dangers = sum(1 for s in suggestions if s.action in ("STOP_LOSS", "CLOSE"))
    warns = sum(1 for s in suggestions if s.action in ("REDUCE", "TAKE_PROFIT"))
    print(f"  🚨 {dangers} 笔需立即操作  ⚠️ {warns} 笔需关注  ✅ {len(suggestions)-dangers-warns} 笔正常")


# ── 信号回溯 ──────────────────────────────────────
async def cmd_track(args):
    """查看历史推荐表现"""
    from self_learn.signal_tracker import print_track_record, update_tracking
    print("📊 信号回溯\n")
    try:
        n = update_tracking()
        print(f"  已更新 {n} 条推荐\n")
    except Exception:
        pass
    print_track_record(days=30)

    # 不再推送，改为 Web 端展示


# ── 定时推送 ──────────────────────────────────────

async def cmd_push(args):
    """推送入口 --time 0800|0945|1130|1440|1700|2030"""
    from phase5_push.wechat_push import wechat_push
    from phase5_push.dingtalk_push import dingtalk_push
    import json, asyncio
    from pathlib import Path
    time_slot = args.time or "0800"

    def _both(t, c):
        w = wechat_push(t, c)
        d = dingtalk_push(t, c)
        return w, d

    OK = chr(0x2705)
    NO = chr(0x274c)

    if time_slot == "0800":
        print(chr(0x1f305) + " 盘前简报 (8:00)")
        briefing = await cmd_briefing(args)
        from phase5_push.push_templates import briefing_push
        t, c = briefing_push(briefing)
        w, d = _both(t, c)
        print(f"  微信: {OK if w else NO}  钉钉: {OK if d else NO}")

    elif time_slot == "0945":
        print(chr(0x26a1) + " 早盘异动 (9:45)")
        from phase1_scan.market_regime import detect_market_regime
        from data.capital_data import industry_comparison
        from data.market_data import get_indices
        regime = detect_market_regime()
        indices = get_indices()
        industry = industry_comparison(top_n=5)
        sh = indices.get(chr(0x4e0a)+chr(0x8bc1)+chr(0x6307)+chr(0x6570), {})
        tops = industry.get("sectors", [])[:3]
        lines = ["#究极选股系统 早盘异动"]
        r_r = regime.get('regime', '?')
        r_t = regime.get('temperature', '?')
        lines.append(f"市场: {r_r} {r_t}{chr(0xb0)}")
        lines.append(f"上证: {sh.get('price','?')} ({sh.get('change_pct',0):+.2f}%)")
        if tops:
            names = [s.get('name','') for s in tops]
            lines.append("领涨: " + " ".join(names))
        ok = dingtalk_push(chr(0x26a1)+" 早盘异动", "\n".join(lines))
        print(f"  钉钉: {OK if ok else NO}")

    elif time_slot == "1130":
        print(chr(0x2600)+chr(0xfe0f) + " 午间复盘 (11:30)")
        from phase1_scan.market_regime import detect_market_regime
        from data.market_data import get_indices
        regime = detect_market_regime()
        indices = get_indices()
        sh = indices.get(chr(0x4e0a)+chr(0x8bc1)+chr(0x6307)+chr(0x6570), {})
        lines = ["#究极选股系统 午间复盘"]
        lines.append(f"市场: {regime.get('regime','?')} {regime.get('temperature','?')}{chr(0xb0)}")
        lines.append(f"上证: {sh.get('price','?')} ({sh.get('change_pct',0):+.2f}%)")
        from phase4_position.position_store import load_positions
        from phase4_position.position_trader import generate_suggestions
        positions = load_positions()
        if positions:
            lines.append("")
            lines.append("[持仓]")
            for s in generate_suggestions(positions):
                lines.append(f"  {s.change_pct:+.1f}% {s.code} {s.action}")
        wechat_push(chr(0x2600)+chr(0xfe0f)+" 午间复盘", "\n".join(lines))

    elif time_slot == "1440":
        print(chr(0x2764)+chr(0xfe0f) + " 短线猎手 (14:40)")
        from phase1_scan.market_regime import detect_market_regime
        from data.market_scanner import scan_full_market
        from data.reference_data import is_excluded_board
        from data.market_data import tencent_quote
        from phase1_scan.screeners.short_term_trader import ShortTermTrader
        regime = detect_market_regime()
        codes = scan_full_market(min_price=5, min_amount_wan=3000, max_stocks=200)
        pool = [c for c in codes if not is_excluded_board(c)]
        quotes = tencent_quote(pool)
        trader = ShortTermTrader()
        await trader.run({"candidates": pool, "market_context": regime})
        Path("data_store/latest_trader_setups.json").write_text(
            json.dumps(trader.last_setups[:10], ensure_ascii=False, indent=2, default=str))
        lines = ["#究极选股系统 尾盘机会"]
        for s in trader.last_setups[:6]:
            if "买入" in s["entry_advice"]:
                icon = chr(0x1f7e2)
            else:
                icon = chr(0x1f7e1)
            lines.append(f"{icon} {s['code']} {s['name']} {s['score']}分 {s['entry_advice']}")
            lines.append(f"  仓{s['position_pct']} 止{s['stop_loss']}")
        ok = dingtalk_push(chr(0x1f3af)+" 尾盘机会", "\n".join(lines))
        print(f"  钉钉: {OK if ok else NO}  信号:{len(trader.last_setups)}")

    elif time_slot == "1700":
        print(chr(0x1f4ca) + " 收盘报告 (17:00)")
        scan_result = await cmd_scan(args)
        from phase1_scan.market_regime import detect_market_regime
        from data.market_scanner import scan_full_market
        from data.reference_data import is_excluded_board
        from data.market_data import tencent_quote
        from phase1_scan.screeners.short_term_trader import ShortTermTrader
        regime = detect_market_regime()
        codes = scan_full_market(min_price=5, min_amount_wan=3000, max_stocks=200)
        pool = [c for c in codes if not is_excluded_board(c)]
        quotes = tencent_quote(pool)
        trader = ShortTermTrader()
        await trader.run({"candidates": pool, "market_context": regime})
        Path("data_store/latest_trader_setups.json").write_text(
            json.dumps(trader.last_setups[:10], ensure_ascii=False, indent=2, default=str))
        lines = ["#究极选股系统 收盘报告"]
        if scan_result:
            for s in scan_result.short_term: lines.append(f"{chr(0x1f525)}{s.code} {s.name} {s.score}")
            for s in scan_result.medium_term: lines.append(f"{chr(0x1f4c8)}{s.code} {s.name} {s.score}")
            for s in scan_result.long_term: lines.append(f"{chr(0x1f333)}{s.code} {s.name} {s.score}")
        if trader.last_setups:
            lines.append("")
            for s in trader.last_setups[:4]:
                icon = chr(0x1f7e2) if "买入" in s["entry_advice"] else chr(0x1f7e1)
                lines.append(f"{icon} {s['code']} {s['name']} {s['score']}分 {s['entry_advice']}")
        w, d = _both(chr(0x1f4ca)+" 收盘报告", "\n".join(lines))
        print(f"  微信: {OK if w else NO}  钉钉: {OK if d else NO}")

    elif time_slot == "2030":
        print(chr(0x1f319) + " 复盘 (20:30)")
        from self_learn.signal_tracker import update_tracking, get_conn
        n = update_tracking()
        conn = get_conn()
        rows = conn.execute("SELECT date, code, name, frame, return_pct, price_at_scan, current_price FROM scan_records WHERE return_pct IS NOT NULL AND date >= date('now','-7 days') ORDER BY date DESC LIMIT 15").fetchall()
        conn.close()
        lines = ["#究极选股系统 复盘"]
        wins, losses = 0, 0
        for r in rows:
            ret = r["return_pct"]
            icon = chr(0x1f7e2) if ret and ret > 0 else chr(0x1f534)
            lines.append(f"{icon} {r['code']} {r['name']} {ret:+.1f}%")
            if ret and ret > 0: wins += 1
            elif ret: losses += 1
        total = wins + losses
        if total > 0:
            lines.append(f"")
            lines.append(f"胜率 {wins}/{total} = {wins/total*100:.0f}%")
        content = "\n".join(lines)
        w, d = _both(chr(0x1f319)+" 复盘", content)
        print(f"  微信: {OK if w else NO}  钉钉: {OK if d else NO}")

# ── 午间速览 ──────────────────────────────────────
async def cmd_noon(args):
    """午间速览（占位）"""
    print("☀️ 午间速览 (12:30)")
    print("   暂未实现 — 待构建")


# ── 日报汇总 ──────────────────────────────────────
async def cmd_daily(args):
    """日报汇总（占位）"""
    print("🌙 日报汇总 (21:00)")
    print("   暂未实现 — 待构建")


# ── Web Dashboard ──────────────────────────────────
def cmd_web(args):
    print(f"🌐 启动 Web Dashboard (端口 {args.port})...")
    from web.app import run
    run(port=args.port)


# ── 绩效报告 ──────────────────────────────────────
async def cmd_report(args):
    from self_learn.performance_analyzer import analyze_performance, print_performance_report
    print("📈 绩效报告\n")
    perf = analyze_performance()
    print_performance_report(perf)


# ── 参数校准 ──────────────────────────────────────
async def cmd_calibrate(args):
    from self_learn.calibrator import calibrate
    print("📐 参数校准\n")
    result = calibrate(output_report=True)
    if result.get("adjustments"):
        print(f"\n⚠️  建议更新 config.yaml 中的 {len(result['adjustments'])} 项参数")


# ── 系统状态 ──────────────────────────────────────
def cmd_status(args):
    config = get_config()
    print("系统配置检查:")
    print(f"  LLM Provider: {config.get('llm', {}).get('provider', '未配置')}")
    print(f"  LLM Model: {config.get('llm', {}).get('model', '未配置')}")
    llm_key = config.get("llm", {}).get("api_key", "")
    print(f"  LLM API Key: {'✅ 已配置' if llm_key else '❌ 未配置'}")
    print(f"  ServerChan Key: {'✅ 已配置' if config.get('push', {}).get('wechat', {}).get('key') else '❌ 未配置'}")
    print()
    print("模块状态:")
    print(f"  📡 data/              ✅ 数据层（a-stock-data 封装）")
    print(f"  🧠 agent_orch/       ✅ 编排层 + 全局上下文")
    print(f"  📰 intelligence/     ✅ 情报简报（6 Agent）")
    print(f"  🔎 phase1_scan/      ✅ 扫描器 + 市场状态 + 前置过滤")
    print(f"  📊 phase3_portfolio/ ✅ 三框架选股（短/中/长）")
    print(f"  🔬 phase2_analysis/  ✅ 3个LLM分析师（价格资金/价值壁垒/风险）")
    print(f"  📋 phase4_position/  ✅ 持仓管理（六维检核 + 操作建议）")
    print(f"  🔔 phase5_push/      ✅ 微信推送（ServerChan）")
    print(f"  🌐 web/             ✅ FastAPI Dashboard")
    print(f"  📚 self_learn/       ✅ 自学习（交易记录+绩效分析+参数校准）")
    print()
    print("数据源健康检查:")
    from agent_orch.health_checker import HealthChecker
    hc = HealthChecker()
    health = hc.check_all()
    for name, info in health.get("data_sources", {}).items():
        icon = "✅" if info.get("ok") else "❌"
        extra = f" ({info.get('reason', '')})" if not info.get("ok") and info.get("reason") else ""
        print(f"  {icon} {name}{extra}")
    llm_info = health.get("llm", {})
    icon = "✅" if llm_info.get("ok") else "❌"
    extra = f" ({llm_info.get('reason', '')})" if not llm_info.get("ok") and llm_info.get("reason") else ""
    print(f"  {icon} deepseek{extra}")
    print(f"  综合: {health.get('overall', 'UNKNOWN')}")


if __name__ == "__main__":
    main()
