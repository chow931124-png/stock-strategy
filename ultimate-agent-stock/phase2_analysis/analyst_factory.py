"""分析师工厂 — 控制哪些分析师运行及运行顺序"""
import asyncio
from phase2_analysis.analysts.price_money import PriceMoneyAnalyst
from phase2_analysis.analysts.value_moat import ValueMoatAnalyst
from phase2_analysis.analysts.risk import RiskAnalyst


async def run_analysts(stock_codes: list[str], market_context: dict,
                       include_supply_chain: bool = False) -> dict:
    """
    对候选股运行所有分析师（并发调用）

    参数:
        stock_codes: 待分析的股票代码列表
        market_context: 市场状态
        include_supply_chain: 是否包含供应链分析（仅AI/半导体板块）

    返回:
        {code: {analyst_type: AnalystReport, ...}}
    """
    analysts = [
        PriceMoneyAnalyst(),
        ValueMoatAnalyst(),
        RiskAnalyst(),
    ]

    # 限制并发数，避免LLM接口过载
    sem = asyncio.Semaphore(min(3, len(stock_codes) * len(analysts)))

    async def _analyze_one(code, analyst):
        async with sem:
            try:
                return await analyst.analyze(code, market_context)
            except Exception:
                return None

    # 每只股票 × 每个分析师 并发执行
    tasks = []
    task_map = []  # (code, analyst_name) -> task
    for code in stock_codes:
        for analyst in analysts:
            tasks.append(_analyze_one(code, analyst))
            task_map.append((code, analyst.name))

    reports = await asyncio.gather(*tasks)

    results = {}
    for (code, name), report in zip(task_map, reports):
        if code not in results:
            results[code] = {}
        results[code][name] = report

    return results
