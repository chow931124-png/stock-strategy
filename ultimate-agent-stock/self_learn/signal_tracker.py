"""信号回溯追踪器 — 记录系统推荐 → 跟踪表现 → 生成报告"""
import sqlite3, json
from datetime import datetime, timedelta
from pathlib import Path
from data.market_data import tencent_quote

ROOT_DIR = Path(__file__).parent.parent.resolve()
DEFAULT_DB = ROOT_DIR / "data_store" / "trade_log.db"


def get_conn():
    path = str(DEFAULT_DB)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS scan_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            code TEXT NOT NULL,
            name TEXT,
            frame TEXT NOT NULL,
            score REAL,
            confidence REAL,
            price_at_scan REAL,
            market_regime TEXT,
            market_temp INTEGER,
            last_check_date TEXT,
            current_price REAL,
            return_pct REAL,
            status TEXT DEFAULT 'active',
            created_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    return conn


def record_scan(portfolio, regime: dict):
    """记录一次 scan 的推荐结果"""
    conn = get_conn()
    date = datetime.now().strftime("%Y-%m-%d")
    count = 0

    # 今天已推荐的股票不要重复记录
    existing = set()
    for row in conn.execute("SELECT code FROM scan_records WHERE date = ?", (date,)):
        existing.add(row["code"])

    # 批量预取行情（一次调用）
    all_picks = []
    for frame, picks in [("short_term", portfolio.short_term),
                          ("medium_term", portfolio.medium_term),
                          ("long_term", portfolio.long_term)]:
        for pick in picks:
            if pick.code not in existing:
                all_picks.append((frame, pick))
    if all_picks:
        batch_quotes = tencent_quote([p.code for _, p in all_picks])
    for frame, pick in all_picks:
        q = batch_quotes.get(pick.code, {})
        price = q.get("price", 0)
        conn.execute("""
            INSERT INTO scan_records
            (date, code, name, frame, score, confidence, price_at_scan,
             market_regime, market_temp)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, (date, pick.code, pick.name, frame, pick.score, pick.confidence,
              price, regime.get("regime", ""), regime.get("temperature", 50)))
        count += 1

    conn.commit()
    conn.close()
    return count


def update_tracking():
    """
    更新历史推荐的最新表现

    ★ 修复：跳过今天的记录（刚推荐，价格未变，算出来 0.0% 污染统计）
    ★ 只更新至少 1 天前的推荐
    """
    conn = get_conn()
    today = datetime.now().strftime("%Y-%m-%d")
    rows = conn.execute("""
        SELECT id, code, price_at_scan, date FROM scan_records
        WHERE status = 'active' AND date < ?
    """, (today,)).fetchall()

    if not rows:
        return 0

    # 批量查询行情（一次调用）
    batch_quotes = tencent_quote([r["code"] for r in rows])
    updated = 0
    for row in rows:
        entry = row["price_at_scan"]
        if entry <= 0:
            continue
        q = batch_quotes.get(row["code"], {})
        current = q.get("price", 0)
        if current <= 0:
            continue
        ret = (current - entry) / entry * 100
        conn.execute("""
            UPDATE scan_records SET last_check_date = ?, current_price = ?, return_pct = ?
            WHERE id = ?
        """, (today, current, round(ret, 1), row["id"]))
        updated += 1

    conn.commit()
    conn.close()
    return updated


def get_track_record(days: int = 30) -> dict:
    """
    获取近期推荐的历史表现

    ★ 修复：统计排除今天的记录（还没有实际跟踪数据）
    每笔推荐增加 days_since 字段
    """
    conn = get_conn()
    today = datetime.now().strftime("%Y-%m-%d")
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    result = {}
    for frame in ["short_term", "medium_term", "long_term"]:
        rows = conn.execute("""
            SELECT date, code, name, score, price_at_scan,
                   current_price, return_pct, status
            FROM scan_records
            WHERE frame = ? AND date >= ?
            ORDER BY date DESC
            LIMIT 50
        """, (frame, cutoff)).fetchall()

        picks = []
        for r in rows:
            pick = dict(r)
            pick["return_pct"] = pick.get("return_pct")
            # 计算推荐天数
            try:
                d1 = datetime.strptime(pick["date"], "%Y-%m-%d")
                d2 = datetime.now()
                pick["days_since"] = (d2 - d1).days
            except Exception:
                pick["days_since"] = 0
            picks.append(pick)
        result[frame] = picks

    # ★ 修复：只统计有实际 return_pct 的记录（排除今天的）
    all_rows = conn.execute("""
        SELECT return_pct FROM scan_records
        WHERE date >= ? AND date < ? AND return_pct IS NOT NULL
    """, (cutoff, today)).fetchall()

    returns = [r["return_pct"] for r in all_rows if r["return_pct"] is not None]
    wins = sum(1 for r in returns if r > 0)
    total = len(returns)

    result["summary"] = {
        "total": total,
        "wins": wins,
        "win_rate": round(wins / total * 100, 1) if total > 0 else 0,
        "avg_return": round(sum(returns) / total, 1) if total > 0 else 0,
        "max_return": round(max(returns), 1) if returns else 0,
        "min_return": round(min(returns), 1) if returns else 0,
        # 今天推荐的条数（单独显示）
        "today_count": len([p for frame in result.values()
                           if isinstance(frame, list)
                           for p in frame if p.get("days_since", 0) == 0]),
    }

    conn.close()
    return result


def get_stock_track_record(days: int = 30) -> dict:
    """
    查询最近 days 天内推荐过的股票及其平均收益率

    返回:
        {code: {"avg_return": 平均收益率, "times": 推荐次数, "wins": 盈利次数}}
    """
    conn = get_conn()
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    rows = conn.execute("""
        SELECT code, return_pct FROM scan_records
        WHERE date >= ? AND return_pct IS NOT NULL
        ORDER BY date DESC
    """, (cutoff,)).fetchall()
    conn.close()

    stats = {}
    for r in rows:
        code = r["code"]
        ret = r["return_pct"]
        if code not in stats:
            stats[code] = {"returns": [], "times": 0, "wins": 0}
        stats[code]["returns"].append(ret)
        stats[code]["times"] += 1
        if ret > 0:
            stats[code]["wins"] += 1

    result = {}
    for code, s in stats.items():
        avg_ret = sum(s["returns"]) / len(s["returns"])
        result[code] = {
            "avg_return": round(avg_ret, 2),
            "times": s["times"],
            "wins": s["wins"],
        }
    return result


def print_track_record(days: int = 30):
    record = get_track_record(days)
    s = record["summary"]

    print(f"\n📊 信号回溯报告（最近 {days} 天）")
    print("=" * 50)
    print(f"  总推荐: {s['total']} 次", end="")
    if s.get("today_count"):
        print(f" (今天 +{s['today_count']} 条待跟踪)", end="")
    print()
    if s['total'] > 0:
        print(f"  胜率:   {s['win_rate']}% ({s['wins']}/{s['total']})")
        print(f"  平均收益: {s['avg_return']:+.1f}%")
        print(f"  最好: {s['max_return']:+.1f}%  最差: {s['min_return']:+.1f}%")

    for frame, label in [("short_term", "短期爆发"), ("medium_term", "中期趋势"),
                          ("long_term", "长期持有")]:
        picks = record.get(frame, [])
        if not picks:
            continue
        print(f"\n  {label}:")
        for p in picks[:5]:
            ret = p.get("return_pct")
            ds = p.get("days_since", 0)
            if ret is not None and ds > 0:
                ret_str = f"{ret:+.1f}%"
                color = "🟢" if ret > 0 else "🔴" if ret < 0 else "⚪"
            else:
                ret_str = f"待跟踪(ds)"
                color = "⚪"
            print(f"    {color} {p['date']} {p['code']:6s} 推荐价{p['price_at_scan']:.2f}  →  {ret_str}")
