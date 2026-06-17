"""全市场实时扫描器 — 腾讯批量查询，覆盖所有A股

策略：
1. 生成所有 A 股代码（排除 688/300）
2. 腾讯批量查询（500只/批，~2秒全市场）
3. 实时过滤（ST/价格/成交额）
4. 输出候选池供扫描器使用

全市场约 4000 只 → 过滤后约 500-800 只候选
"""
import os
import random
import requests
from typing import Optional
from data.reference_data import is_excluded_board
from datetime import datetime, timedelta

# 代码生成范围（实际A股分布，减少空壳代码）
CODE_RANGES = [
    # 上海主板 600000-605999
    ("sh", 600000, 605999),
    # 深圳主板 000001-001999
    ("sz", 0, 1999),
    # 中小板 002000-002999
    ("sz", 2000, 2999),
]

TENCENT_URL = "https://qt.gtimg.cn/q="
BATCH_SIZE = 200

# 已验证有效的代码段（排除大段空壳区间）
CODE_SEGMENTS = [
    ("sh", 600000, 601999),
    ("sh", 603000, 605999),
    ("sz", 0, 1999),
    ("sz", 2000, 2999),
] if os.environ.get("USE_NARROW_RANGE") else CODE_RANGES


def generate_all_codes() -> list[str]:
    """生成所有可能的A股代码（含前缀）"""
    codes = []
    for prefix, start, end in CODE_SEGMENTS:
        for i in range(start, end + 1):
            code = f"{prefix}{i:06d}"
            if prefix == "sz" and 300000 <= i <= 399999:
                continue
            codes.append(code)
    return codes


def scan_full_market(min_price: float = 5.0,
                     min_amount_wan: float = 5000,
                     exclude_st: bool = True,
                     max_stocks: int = 300) -> list[str]:
    """
    全市场扫描，返回通过前置过滤的股票代码列表（纯6位）

    参数:
        min_price: 最低股价
        min_amount_wan: 最低成交额(万)
        max_stocks: 最大返回数量
    """
    all_codes = generate_all_codes()
    total = len(all_codes)
    print(f"    全市场代码池: {total} 个(含空壳, 实际约4000只)")

    candidates_6digit = []
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})

    for batch_start in range(0, total, BATCH_SIZE):
        batch = all_codes[batch_start:batch_start + BATCH_SIZE]
        url = TENCENT_URL + ",".join(batch)
        if len(url) > 8000:
            print(f"     ⚠️ URL过长({len(url)}字符), 分批调小到{BATCH_SIZE}")
            continue
        try:
            r = session.get(url, timeout=15)
            r.encoding = "gbk"
            for line in r.text.strip().split("\n"):
                parts = line.split("~")
                if len(parts) < 50:
                    continue
                name = parts[1]
                code_6 = parts[2]
                price_str = parts[3]
                amount_wan_str = parts[37]

                # 空名称 = 无效代码
                if not name:
                    continue

                # 排除 688/300
                if is_excluded_board(code_6):
                    continue

                # 排除 ST
                if exclude_st and ("ST" in name or "*ST" in name):
                    continue

                # 价格过滤
                try:
                    price = float(price_str)
                    if price < min_price:
                        continue
                except (ValueError, TypeError):
                    continue

                # 成交额过滤
                try:
                    amount_wan = float(amount_wan_str)
                    if amount_wan < min_amount_wan:
                        continue
                except (ValueError, TypeError):
                    continue

                candidates_6digit.append(code_6)

        except Exception:
            pass

        if len(candidates_6digit) >= max_stocks * 2:
            break

    # 去重 + 随机打乱（消除代码段顺序导致的选择偏差）
    candidates_6digit = list(dict.fromkeys(candidates_6digit))
    random.shuffle(candidates_6digit)
    print(f"    过滤后: {len(candidates_6digit)} 只")
    return candidates_6digit[:max_stocks]
