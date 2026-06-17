"""
AI 产业链图谱 v1 — 上下游关系 + 卡位分析 + 独占性评分

产业链层级（从最上游到最下游）:
  Layer 0: 基础原材料（硅片/电子特气/金属材料）
  Layer 1: 芯片设计/制造（GPU/存储/模拟芯片）
  Layer 2: 核心元器件（光模块/PCB/MLCC/连接器）
  Layer 3: 算力基础设施（服务器/交换机/液冷/电力）
  Layer 4: AI 应用/软件（大模型/自动驾驶/机器人）

每个环节标注：
  - 卡位重要性: 1-5（5=产业链最核心瓶颈）
  - 国产替代空间: 1-5（5=急需国产化）
  - 独占/寡头程度: 1-5（5=全球寡头垄断）
  - 当前势头: 1-5（5=量价齐升最猛）
"""
from dataclasses import dataclass, field
from typing import Optional
import numpy as np


# ─── 产业链节点定义 ────────────────────────────────
@dataclass
class SupplyChainNode:
    """产业链上的一个环节"""
    id: str                      # 唯一标识
    name: str                    # 中文名
    layer: int                   # 层级 0-4
    sub_sector: str              # 细分赛道

    # 四维评分
    bottleneck_score: int = 0    # 卡位重要性 1-5
    localization_score: int = 0  # 国产替代空间 1-5
    monopoly_score: int = 0      # 独占/寡头程度 1-5
    momentum_score: int = 0      # 当前势头 1-5

    # 描述
    description: str = ""
    bottleneck_reason: str = ""  # 为什么卡脖子
    key_stocks: list = field(default_factory=list)  # 代表A股标的

    # 美股映射
    us_mapping: list = field(default_factory=list)

    @property
    def composite_score(self) -> float:
        """综合得分 = (卡位×0.35 + 独占×0.25 + 势头×0.25 + 国产替代×0.15)"""
        return (self.bottleneck_score * 0.35 +
                self.monopoly_score * 0.25 +
                self.momentum_score * 0.25 +
                self.localization_score * 0.15)


# ─── 构建产业链图谱 ────────────────────────────────
def build_ai_chain() -> dict[str, SupplyChainNode]:
    """构建完整的AI产业链图谱"""
    nodes = {}

    # ════════════════════════════════════════════
    # Layer 0: 基础原材料
    # ════════════════════════════════════════════
    nodes["硅片"] = SupplyChainNode(
        id="silicon_wafer", name="硅片/衬底", layer=0,
        sub_sector="半导体材料",
        bottleneck_score=4, localization_score=4, monopoly_score=3,
        momentum_score=3,
        description="半导体芯片的基石，12吋硅片全球80%被信越、SUMCO垄断",
        bottleneck_reason="大硅片国产化率<10%，沪硅产业12吋硅片产能爬坡中",
        key_stocks=["688126", "600703"],
        us_mapping=[],
    )
    nodes["电子特气"] = SupplyChainNode(
        id="specialty_gas", name="电子特气", layer=0,
        sub_sector="半导体材料",
        bottleneck_score=3, localization_score=3, monopoly_score=2,
        momentum_score=4,
        description="半导体制造必需气体，国产替代进展快",
        key_stocks=["002409", "300346"],
    )
    nodes["电子布/玻纤"] = SupplyChainNode(
        id="electronic_glass", name="电子布/玻纤", layer=0,
        sub_sector="PCB上游",
        bottleneck_score=3, localization_score=1, monopoly_score=3,
        momentum_score=5,
        description="PCB核心材料，年内第五轮提价，涨幅100%",
        bottleneck_reason="全球产能集中在中国，涨价周期中弹性最大",
        key_stocks=["600176", "600183"],
    )
    nodes["覆铜板/CCL"] = SupplyChainNode(
        id="ccl", name="覆铜板/CCL", layer=0,
        sub_sector="PCB上游",
        bottleneck_score=3, localization_score=1, monopoly_score=3,
        momentum_score=5,
        description="PCB核心基材，铜箔+玻纤布+环氧树脂涨价传导",
        key_stocks=["603002", "600183", "002916"],
    )
    nodes["战略金属/稀土"] = SupplyChainNode(
        id="strategic_metals", name="战略金属/稀土", layer=0,
        sub_sector="有色金属",
        bottleneck_score=4, localization_score=1, monopoly_score=4,
        momentum_score=4,
        description="AI+军工关键原材料，中国掌握全球70%以上稀土/钨/锑/镓供应",
        bottleneck_reason="中国对稀土/锗/镓实施出口管制，但全球供应链重构中",
        key_stocks=[
            # 稀土永磁
            "600010", "000970", "002056",
            # 钨
            "000657", "002378",
            # 钼
            "603993", "601958",
            # 锑/锗/镓
            "002155", "002428",
            # 铜/铝/锌等工业金属
            "601899", "601600", "000630", "601168", "000603",
            "600497", "600711", "000960", "600531", "000426",
        ],
        us_mapping=["MP"],
    )

    # ════════════════════════════════════════════
    # Layer 1: 芯片设计/制造
    # ════════════════════════════════════════════
    nodes["AI芯片"] = SupplyChainNode(
        id="ai_chip", name="AI芯片/GPU", layer=1,
        sub_sector="芯片设计",
        bottleneck_score=5, localization_score=5, monopoly_score=5,
        momentum_score=5,
        description="AI算力的最核心瓶颈，NVDA市占率>80%",
        bottleneck_reason="全球AI芯片被NVDA垄断，国产替代（寒武纪/海光）差距3-5年",
        key_stocks=["688041", "688256"],
        us_mapping=["NVDA", "AMD"],
    )
    nodes["存储芯片"] = SupplyChainNode(
        id="memory_chip", name="存储芯片", layer=1,
        sub_sector="芯片设计",
        bottleneck_score=4, localization_score=4, monopoly_score=4,
        momentum_score=5,
        description="HBM供不应求，预计2026年市场规模+250%",
        bottleneck_reason="HBM被SK海力士/Samsung垄断，国产替代刚起步",
        key_stocks=["002049", "603986", "688525"],
        us_mapping=["MU"],
    )
    nodes["模拟芯片"] = SupplyChainNode(
        id="analog_chip", name="模拟芯片", layer=1,
        sub_sector="芯片设计",
        bottleneck_score=3, localization_score=4, monopoly_score=3,
        momentum_score=5,
        description="TI/ADI等年内两次涨价，国产替代加速",
        key_stocks=["688798", "603501"],
    )
    nodes["先进封装"] = SupplyChainNode(
        id="advanced_packaging", name="先进封装", layer=1,
        sub_sector="芯片封测",
        bottleneck_score=4, localization_score=4, monopoly_score=3,
        momentum_score=4,
        description="Chiplet时代封装环节价值量飙升",
        key_stocks=["688012", "002156", "603005"],
    )
    nodes["RISC-V/国产CPU"] = SupplyChainNode(
        id="riscv_cpu", name="RISC-V/国产CPU", layer=1,
        sub_sector="芯片设计",
        bottleneck_score=4, localization_score=5, monopoly_score=3,
        momentum_score=4,
        description="AI自主可控最后一块拼图，RISC-V架构突破X86/ARM垄断",
        bottleneck_reason="国产CPU（龙芯/飞腾）与Intel/AMD差距3-5年，RISC-V生态快速崛起",
        key_stocks=["688041", "002049", "603986"],
    )

    # ════════════════════════════════════════════
    # Layer 2: 核心元器件
    # ════════════════════════════════════════════
    nodes["光模块"] = SupplyChainNode(
        id="optical_module", name="光模块", layer=2,
        sub_sector="光通信",
        bottleneck_score=4, localization_score=2, monopoly_score=4,
        momentum_score=5,
        description="AI数据中心互联核心，中国厂商全球市占率>60%",
        bottleneck_reason="中国光模块企业（旭创/新易盛/天孚）全球领先，但上游光芯片依赖进口",
        key_stocks=["300308", "300502", "300394", "688313"],
        us_mapping=["NVDA", "AVGO", "MRVL"],
    )
    nodes["PCB"] = SupplyChainNode(
        id="pcb", name="PCB/印制电路板", layer=2,
        sub_sector="PCB",
        bottleneck_score=4, localization_score=1, monopoly_score=3,
        momentum_score=5,
        description="AI服务器PCB价值量飙升233%，涨价潮持续",
        bottleneck_reason="高端PCB（HDI/封装基板）仍有技术壁垒，但中国龙头全球领先",
        key_stocks=["002916", "002463", "601138", "603228", "002579", "002384"],
        us_mapping=["NVDA"],
    )
    nodes["连接器"] = SupplyChainNode(
        id="connector", name="连接器/线束", layer=2,
        sub_sector="电子元器件",
        bottleneck_score=3, localization_score=2, monopoly_score=3,
        momentum_score=4,
        description="AI服务器内部互联需求暴增，高速背板连接器供不应求",
        key_stocks=["002475", "300570", "601137"],
    )
    nodes["封测材料"] = SupplyChainNode(
        id="packaging_material", name="封测材料/设备", layer=2,
        sub_sector="半导体材料",
        bottleneck_score=3, localization_score=4, monopoly_score=3,
        momentum_score=4,
        description="先进封装催生环氧塑封料/电镀液/测试座等耗材需求爆发",
        key_stocks=["002409", "300236", "300604"],
    )
    nodes["MLCC"] = SupplyChainNode(
        id="mlcc", name="MLCC/多层陶瓷电容", layer=2,
        sub_sector="被动元件",
        bottleneck_score=3, localization_score=3, monopoly_score=3,
        momentum_score=5,
        description="AI服务器推高用量，村田7月再涨价",
        key_stocks=["300408", "000636", "603005"],
    )
    nodes["光芯片"] = SupplyChainNode(
        id="optical_chip", name="光芯片/电芯片", layer=2,
        sub_sector="光通信",
        bottleneck_score=4, localization_score=5, monopoly_score=4,
        momentum_score=4,
        description="光模块的最核心瓶颈，EML/DSP芯片被美日垄断",
        bottleneck_reason="100G EML光芯片、DSP电芯片被Broadcom/Lumentum垄断",
        key_stocks=["688498", "300548"],
    )

    # ════════════════════════════════════════════
    # Layer 3: 算力基础设施
    # ════════════════════════════════════════════
    nodes["AI服务器"] = SupplyChainNode(
        id="ai_server", name="AI服务器", layer=3,
        sub_sector="服务器",
        bottleneck_score=3, localization_score=3, monopoly_score=3,
        momentum_score=4,
        description="AI算力的物理载体，需求爆发式增长",
        key_stocks=["601138", "000977", "688041"],
        us_mapping=["NVDA", "DELL"],
    )
    nodes["液冷散热"] = SupplyChainNode(
        id="liquid_cooling", name="液冷散热", layer=3,
        sub_sector="散热",
        bottleneck_score=3, localization_score=2, monopoly_score=4,
        momentum_score=5,
        description="单机柜功率从30kW→200kW，液冷是唯一解",
        bottleneck_reason="液冷技术壁垒高，但中国企业（英维克/高澜）全球领先",
        key_stocks=["603105", "600481", "688408"],
        us_mapping=["VRT"],
    )
    nodes["算力电力"] = SupplyChainNode(
        id="ai_power", name="算力电力/变压器", layer=3,
        sub_sector="电力设备",
        bottleneck_score=4, localization_score=2, monopoly_score=3,
        momentum_score=4,
        description="AI算力中心电力需求暴增，变压器供不应求",
        key_stocks=["600089", "600406", "601567"],
    )

    # ════════════════════════════════════════════
    # Layer 4: AI 应用
    # ════════════════════════════════════════════
    nodes["机器人"] = SupplyChainNode(
        id="robot", name="人形机器人", layer=4,
        sub_sector="机器人",
        bottleneck_score=2, localization_score=3, monopoly_score=2,
        momentum_score=4,
        description="AI+硬件的最佳载体，特斯拉Optimus带火全产业链",
        key_stocks=["688017", "603728", "002050"],
        us_mapping=["TSLA"],
    )

    return nodes


# ─── 产业链分析 ────────────────────────────────
def analyze_chain() -> dict:
    """全产业链分析报告"""
    nodes = build_ai_chain()

    # 按综合评分排序
    sorted_nodes = sorted(nodes.values(), key=lambda n: -n.composite_score)

    # 按层级分组
    by_layer = {}
    for n in nodes.values():
        by_layer.setdefault(n.layer, []).append(n)

    # 找出最大卡脖子环节
    bottlenecks = sorted(nodes.values(), key=lambda n: -n.bottleneck_score)[:5]

    return {
        "total_nodes": len(nodes),
        "nodes": nodes,
        "sorted_by_score": sorted_nodes,
        "by_layer": by_layer,
        "top_bottlenecks": bottlenecks,
    }


def get_chain_stocks() -> list[str]:
    """获取产业链所有A股标的"""
    nodes = build_ai_chain()
    stocks = set()
    for n in nodes.values():
        for s in n.key_stocks:
            stocks.add(s)
    return list(stocks)


def score_stock_in_chain(code: str) -> dict:
    """
    评估一只股票在产业链中的位置

    返回:
        {in_chain: bool, nodes: [...], max_bottleneck, avg_momentum, composite}
    """
    nodes = build_ai_chain()
    matched = []
    for n in nodes.values():
        if code in n.key_stocks:
            matched.append(n)

    if not matched:
        return {"in_chain": False}

    return {
        "in_chain": True,
        "nodes": [{"name": n.name, "layer": n.layer, "sub_sector": n.sub_sector,
                    "bottleneck": n.bottleneck_score, "monopoly": n.monopoly_score,
                    "momentum": n.momentum_score} for n in matched],
        "max_bottleneck": max(n.bottleneck_score for n in matched),
        "avg_momentum": round(np.mean([n.momentum_score for n in matched]), 1),
        "composite": round(np.mean([n.composite_score for n in matched]), 1),
    }


def print_chain_report():
    """打印产业链分析报告"""
    nodes = build_ai_chain()
    sorted_nodes = sorted(nodes.values(), key=lambda n: -n.composite_score)

    print("\n" + "=" * 60)
    print("🤖 AI 产业链全景图")
    print("=" * 60)

    for layer in range(5):
        layer_nodes = [n for n in sorted_nodes if n.layer == layer]
        if not layer_nodes:
            continue
        layer_names = ["📦 基础原材料", "💠 芯片设计/制造", "🔧 核心元器件",
                       "🖥️ 算力基础设施", "🤖 AI 应用"]
        print(f"\n{layer_names[layer]}:")
        print("-" * 40)
        for n in layer_nodes:
            bar_b = "🟥" * n.bottleneck_score + "⬜" * (5 - n.bottleneck_score)
            bar_m = "🟧" * n.monopoly_score + "⬜" * (5 - n.monopoly_score)
            stocks_str = " ".join(n.key_stocks[:3])
            print(f"  {n.name:12s}  {n.composite_score:.1f}分")
            print(f"    卡位{bar_b}  独占{bar_m}  势头{'🟢'*n.momentum_score}{'⬜'*(5-n.momentum_score)}")
            print(f"    标的: {stocks_str}")

    # Top 卡脖子
    print(f"\n{'='*60}")
    print("🔴 最大卡脖子环节 Top 5:")
    print(f"{'='*60}")
    bottlenecks = sorted(nodes.values(), key=lambda n: -n.bottleneck_score)[:5]
    for n in bottlenecks:
        print(f"  {n.name:12s}  卡位{n.bottleneck_score}/5  理由: {n.bottleneck_reason}")
