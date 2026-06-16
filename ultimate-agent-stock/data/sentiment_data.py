"""情感数据层 — 新闻、资讯、公告"""
from .cache import em_once
from .market_data import em_get, normalize_code
from .capital_data import __datacenter

import requests
import pandas as pd


# ── 东财个股新闻 ──────────────────────────────────
@em_once
def eastmoney_stock_news(code: str, page_size: int = 20) -> list[dict]:
    """个股相关新闻"""
    code = normalize_code(code)
    url = "https://search-api-web.eastmoney.com/search/jsonp"
    params = {
        "cb": "jQuery", "param": f'{{"uid":"","keyword":"{code}","type":["cmsArticleWebOld"],"client":"web"}}',
        "pageNum": "1", "pageSize": str(page_size),
    }
    try:
        r = em_get(url, params=params, timeout=10)
        text = r.text
        import re, json
        match = re.search(r'jQuery\((.*)\)', text)
        if match:
            data = json.loads(match.group(1))
            articles = data.get("result", {}).get("cmsArticleWebOld", [])
            result = []
            for art in (articles or []):
                result.append({
                    "title": art.get("title", "").replace("<em>", "").replace("</em>", ""),
                    "content": (art.get("content", "") or "")[:200],
                    "time": art.get("date", ""),
                    "source": art.get("mediaName", ""),
                    "url": art.get("articleUrl", ""),
                })
            return result
    except Exception:
        pass
    return []


# ── 东财全球资讯 ──────────────────────────────────
def eastmoney_global_news(page_size: int = 50) -> list[dict]:
    """7×24 全球财经快讯"""
    url = "https://np-weblist.eastmoney.com/comm/web/getFastNewsList"
    import uuid
    params = {
        "client": "web", "biz": "web_724",
        "fastColumn": "102", "sortEnd": "",
        "pageSize": str(page_size),
        "req_trace": str(uuid.uuid4()),
    }
    try:
        r = em_get(url, params=params, timeout=15)
        data = r.json()
        items = data.get("data", {}).get("fastNewsList", []) if isinstance(data, dict) else []
        result = []
        for item in items:
            result.append({
                "title": item.get("title", ""),
                "summary": (item.get("summary", "") or "")[:200],
                "time": item.get("showTime", ""),
            })
        return result
    except Exception:
        return []


# ── 同花顺强势股（题材归因）────────────────────────
_THS_API_PRIMARY = "http://zx.10jqka.com.cn/event/api/getharden/date/{date}/orderby/date/orderway/desc/charset/GBK/"
_THS_API_BACKUP = "https://zx.10jqka.com.cn/hot_stock/data/"

def ths_hot_reason(date: str = None) -> pd.DataFrame:
    """同花顺当日强势股 + 题材归因"""
    from datetime import datetime, timedelta
    import time

    if date is None:
        date = (datetime.now() - timedelta(hours=16)).strftime("%Y-%m-%d")

    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://zx.10jqka.com.cn/"}

    def _parse_items(items: list) -> pd.DataFrame:
        records = []
        for item in items:
            code = item.get("code", "")
            if not code:
                continue
            # 兼容 getharden 和 hot_stock 两种返回格式
            chg_str = item.get("chg", item.get("zhangfu", "0"))
            try:
                zhangfu = float(str(chg_str).replace("%", ""))
            except (ValueError, AttributeError):
                zhangfu = 0.0
            records.append({
                "代码": code,
                "名称": item.get("name", ""),
                "收盘价": item.get("trade", item.get("close", 0)),
                "涨幅%": zhangfu,
                "题材归因": item.get("reason", ""),
            })
        return pd.DataFrame(records)

    # ── 主API ──
    url = _THS_API_PRIMARY
    try:
        r = requests.get(url.format(date=date), headers=headers, timeout=10)
        data = r.json()
        items = data.get("data") or []
        if items:
            df = _parse_items(items)
            if len(df) >= 10:
                return df
            print(f"  ⚠️ [ths_hot_reason] 主API返回{len(df)}条, 尝试备选API")
        else:
            print(f"  ⚠️ [ths_hot_reason] 主API返回空, 尝试备选API")
    except Exception as e:
        print(f"  ⚠️ [ths_hot_reason] 主API异常: {e}, 尝试备选API")

    # ── 备选API ──
    time.sleep(0.5)
    try:
        params = {"date": date.replace("-", ""), "type": "all",
                   "page": "0", "tag": "3", "need_tag": "yes"}
        r = requests.get(_THS_API_BACKUP, params=params, headers=headers, timeout=10)
        data = r.json()
        items = data.get("data", {}).get("list", []) if isinstance(data, dict) else []
        if items:
            df = _parse_items(items)
            if len(df) >= 10:
                return df
        print(f"  ⚠️ [ths_hot_reason] 备选API也失败 (返回{len(items)}条)")
    except Exception as e:
        print(f"  ⚠️ [ths_hot_reason] 备选API也异常: {e}")

    return pd.DataFrame()


# ── 数据源健康检查 ──────────────────────────────────
def check_data_sources() -> list:
    """验证所有外部数据源是否可用, 返回 [(名称, 状态, 详情), ...]"""
    from datetime import datetime, timedelta
    import time

    results = []

    # 1. 同花顺热点
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        url = "http://zx.10jqka.com.cn/event/api/getharden/date/{}/orderby/date/orderway/desc/charset/GBK/"
        r = requests.get(url.format(today), headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        items = r.json().get("data") or []
        if len(items) >= 10:
            results.append(("同花顺热点", "✅", f"{len(items)}条数据"))
        else:
            results.append(("同花顺热点", "⚠️", f"仅{len(items)}条"))
    except Exception as e:
        results.append(("同花顺热点", "❌", str(e)[:60]))

    # 2. 腾讯行情
    try:
        r = requests.get("https://qt.gtimg.cn/q=sh000001", timeout=10)
        r.encoding = "gbk"
        if "上证指数" in r.text:
            results.append(("腾讯行情", "✅", "上证指数正常"))
        else:
            results.append(("腾讯行情", "⚠️", "返回异常"))
    except Exception as e:
        results.append(("腾讯行情", "❌", str(e)[:60]))

    # 3. 东财板块
    try:
        url = "https://push2.eastmoney.com/api/qt/clist/get"
        params = {"pn": "1", "pz": "5", "po": "1", "np": "1",
                  "fields": "f12,f14", "fs": "m:90+t:2"}
        r = requests.get(url, params=params, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        d = r.json()
        if d.get("data", {}).get("diff", []):
            results.append(("东财板块", "✅", "正常"))
        else:
            results.append(("东财板块", "⚠️", "返回空"))
    except Exception as e:
        results.append(("东财板块", "❌", str(e)[:60]))

    # 4. iwencai
    try:
        r = requests.get("https://www.iwencai.com/", timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        results.append(("iwencai", "✅" if r.ok else "⚠️", f"HTTP {r.status_code}"))
    except Exception as e:
        results.append(("iwencai", "❌", str(e)[:60]))

    return results


# ── 限售解禁 ──────────────────────────────────────
def lockup_expiry(code: str, trade_date: str = "", forward_days: int = 90) -> dict:
    """限售解禁日历"""
    code = normalize_code(code)
    filters = f'(SECURITY_CODE="{code}")'
    data_list = __datacenter("RPT_LOCKUP_LOCKUPINFO", filter_str=filters, page_size=90)
    today = __today_str()
    from datetime import datetime, timedelta

    result = {"history": [], "upcoming": []}
    for d in data_list:
        item = {
            "date": d.get("DECLARE_DATE", ""),
            "type": d.get("UNLOCK_TYPE", ""),
            "shares": float(d.get("UNLOCK_SHARES", 0)),
            "ratio": float(d.get("UNLOCK_SHARES_RATIO", 0)),
        }
        if item["date"] and item["date"] <= today:
            result["history"].append(item)
        else:
            result["upcoming"].append(item)
    return result


def __today_str() -> str:
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d")


# ── 巨潮公告 ──────────────────────────────────────
def cninfo_announcements(code: str, page_size: int = 30) -> list[dict]:
    """巨潮公告全文检索"""
    code = normalize_code(code)
    # 获取 orgId
    org_id = __get_org_id(code)
    if not org_id:
        return []
    url = "http://www.cninfo.com.cn/new/disclosure/stock"
    params = {
        "stockCode": code, "orgId": org_id,
        "pageNum": "1", "pageSize": str(page_size),
        "tabName": "fulltext", "plate": "",
    }
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "http://www.cninfo.com.cn/"}
    try:
        r = requests.get(url, params=params, headers=headers, timeout=15)
        data = r.json()
        items = data.get("data", []) if isinstance(data, dict) else []
        result = []
        for item in items:
            result.append({
                "title": item.get("announcementTitle", ""),
                "date": item.get("announcementDate", ""),
                "type": item.get("categoryName", ""),
                "url": f"http://www.cninfo.com.cn/new/disclosure/detail?stockCode={code}&orgId={org_id}",
            })
        return result
    except Exception:
        return []


ORG_ID_CACHE = {}

def __get_org_id(code: str) -> str:
    """动态解析巨潮 orgId"""
    if code in ORG_ID_CACHE:
        return ORG_ID_CACHE[code]
    url = f"http://www.cninfo.com.cn/new/information/topInfo/query?stockCode={code}"
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        data = r.json()
        org_id = data.get("orgId", "")
        ORG_ID_CACHE[code] = org_id
        return org_id
    except Exception:
        return ""


# ── 东财概念板块归属 ─────────────────────────────
def eastmoney_concept_blocks(code: str) -> dict:
    """个股所属板块/概念"""
    code = normalize_code(code)
    secid = f"1.{code}" if code.startswith(("6", "9")) else f"0.{code}"
    url = "https://push2.eastmoney.com/api/qt/slist/get"
    params = {
        "secid": secid,
        "fields": "f12,f14,f2,f3,f62,f184,f66",
        "fltt": "2",
    }
    try:
        r = em_get(url, params=params, timeout=10)
        data = r.json().get("data", {})
        return {
            "code": code,
            "boards": data.get("diff", []),
        }
    except Exception:
        return {"code": code, "boards": []}
