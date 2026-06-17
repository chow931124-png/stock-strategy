"""Agent 基类 — 所有分析器和扫描器的统一接口"""
from dataclasses import dataclass, field
from typing import Optional
from abc import ABC, abstractmethod


@dataclass
class AnalystReport:
    """分析报告标准格式"""
    stock_code: str
    stock_name: str = ""
    analyst_type: str = ""
    score: float = 50.0          # 0-100
    confidence: float = 0.5      # 0-1
    signal: str = "NEUTRAL"      # POSITIVE / NEUTRAL / NEGATIVE
    reasoning: str = ""
    key_metrics: dict = field(default_factory=dict)

    # 三时间框架独立打分
    short_term_score: float = 50.0
    medium_term_score: float = 50.0
    long_term_score: float = 50.0


@dataclass
class StockPick:
    """最终选股输出"""
    code: str
    name: str = ""
    reason: str = ""
    confidence: float = 0.5
    score: float = 0.0
    score_by_analyst: dict = field(default_factory=dict)

    # 行情
    last_close: Optional[float] = None
    current_price: Optional[float] = None
    change_pct: Optional[float] = None

    # 短期专有
    entry_zone: Optional[tuple] = None
    stop_loss: Optional[float] = None
    target_price: Optional[float] = None
    expected_hold_days: Optional[int] = None

    # 中期专有
    trend_direction: Optional[str] = None
    sector_tailwind: Optional[str] = None
    key_events: Optional[list] = None

    # 长期专有
    moat_summary: Optional[str] = None
    fair_value: Optional[float] = None
    growth_3y: Optional[float] = None

    # 因子标签（web展示用）
    overseas_boost: float = 0.0
    chain_boost: float = 0.0


@dataclass
class UltimatePortfolio:
    """最终投资组合"""
    market_regime: str = "UNKNOWN"
    market_temperature: int = 50
    briefing_context: str = ""

    short_term: list = field(default_factory=list)    # 1-2 stocks
    medium_term: list = field(default_factory=list)   # 1-3 stocks
    long_term: list = field(default_factory=list)     # 1 stock


class BaseAgent(ABC):
    """所有 Agent 的抽象基类"""

    def __init__(self, name: str = ""):
        self.name = name or self.__class__.__name__

    @abstractmethod
    async def run(self, context: dict) -> dict:
        """执行 Agent 任务"""
        ...


class BaseAnalyst(BaseAgent):
    """分析师基类"""

    async def analyze(self, stock_code: str, market_context: dict) -> AnalystReport:
        """分析单只股票"""
        ...

    async def run(self, context: dict) -> dict:
        stocks = context.get("stocks", [])
        market = context.get("market_context", {})
        reports = []
        for code in stocks:
            report = await self.analyze(code, market)
            reports.append(report)
        return {"analyst_type": self.name, "reports": reports}


class BaseScreener(BaseAgent):
    """扫描器基类"""

    @abstractmethod
    async def screen(self, candidates: list[str], market_context: dict) -> list[str]:
        """从候选池中筛选"""
        ...

    async def run(self, context: dict) -> dict:
        candidates = context.get("candidates", [])
        market = context.get("market_context", {})
        selected = await self.screen(candidates, market)
        return {"screener_type": self.name, "selected": selected}
