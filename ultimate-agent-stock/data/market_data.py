"""
行情数据层 — 封装 a-stock-data 的行情类函数
数据源优先级: mootdx(TCP) > 腾讯(HTTP) > 东财/百度(HTTP)
"""
import time
import threading
import random
import requests
from typing import Optional
from mootdx.quotes import Quotes

from config import get_config

# ── 全局客户端 ────────────────────────────────────
_mootdx_client = None
_em_session = requests.Session()
_em_session.headers.update({
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
})
_em_last_call = [0.0]
_em_lock = threading.Lock()

# ── 东财限流请求 ──────────────────────────────────
def em_get(url: str, params: dict = None, headers: dict = None, timeout: int = 15, **kwargs):
    """东财统一请求入口：自动节流 + 复用 session（从 a-stock-data 移植）"""
    cfg = get_config()
    min_interval = cfg.get("data", {}).get("em_min_interval", 3.0)
    with _em_lock:
        wait = min_interval - (time.time() - _em_last_call[0])
        if wait > 0:
            time.sleep(wait + random.uniform(0.1, 0.5))
        try:
            return _em_session.get(url, params=params, headers=headers, timeout=timeout, **kwargs)
        finally:
            _em_last_call[0] = time.time()


def _get_mootdx():
    global _mootdx_client
    if _mootdx_client is None:
        _mootdx_client = Quotes.factory(market='std')
    return _mootdx_client


def get_prefix(code: str) -> str:
    """6位代码 → 市场前缀"""
    if code.startswith(("6", "9")):
        return "sh"
    elif code.startswith("8"):
        return "bj"
    else:
        return "sz"


def normalize_code(code: str) -> str:
    """统一为纯6位数字"""
    return code.upper().replace("SH", "").replace("SZ", "").replace("BJ", "").replace(".", "")


# ── K 线数据 ──────────────────────────────────────
def get_bars(code: str, category: int = 4, offset: int = 120) -> list:
    """
    获取 K 线数据
    主: MEM缓存(预加载用) → mootdx TCP → 备1: 腾讯ifzq → 备2: 百度

    category: 4=日线, 其他暂不支持fallback
    """
    # ── 检查预加载缓存（preload_klines 存入 MEM） ──
    mem_key = f"bars:{code}:{category}:{offset}"
    from data.cache import MEM as _mem  # avoid circular import
    cached = _mem.get(mem_key)
    if cached is not None:
        return cached

    # ── 主：mootdx TCP ──
    if category == 4:
        client = _get_mootdx()
        market = 1 if code.startswith(("6", "9")) else 0
        try:
            df = client.bars(symbol=code, category=category, offset=offset)
            if df is not None and not df.empty:
                result = []
                for _, k in df.iterrows():
                    result.append({
                        "datetime": str(k.get("datetime", "")),
                        "open": float(k.get("open", 0)),
                        "high": float(k.get("high", 0)),
                        "low": float(k.get("low", 0)),
                        "close": float(k.get("close", 0)),
                        "volume": float(k.get("volume", 0)),
                        "amount": float(k.get("amount", 0)),
                    })
                return result
        except Exception:
            pass
    else:
        # 非日线只走 mootdx
        client = _get_mootdx()
        market = 1 if code.startswith(("6", "9")) else 0
        try:
            df = client.bars(symbol=code, category=category, offset=offset)
            if df is not None and not df.empty:
                return [{
                    "datetime": str(k.get("datetime", "")),
                    "open": float(k.get("open", 0)),
                    "high": float(k.get("high", 0)),
                    "low": float(k.get("low", 0)),
                    "close": float(k.get("close", 0)),
                    "volume": float(k.get("volume", 0)),
                    "amount": float(k.get("amount", 0)),
                } for _, k in df.iterrows()]
        except Exception:
            pass
        return []

    # ── 备1：腾讯 ifzq K线（日线） ──
    try:
        prefix = get_prefix(code)
        url = f"http://ifzq.gtimg.cn/appstock/app/fqkline/get?param={prefix}{code},d1,,,{offset}"
        r = requests.get(url, timeout=10)
        data = r.json()
        # 两种格式兼容
        raw = data.get("data", {})
        klines = raw.get(f"{prefix}{code}", raw) if isinstance(raw, dict) else []
        if isinstance(klines, dict):
            klines = klines.get("d", klines.get("d1", []))
        if isinstance(klines, list) and len(klines) >= 10:
            result = []
            for k in klines:
                if not isinstance(k, list) or len(k) < 6:
                    try:
                        if isinstance(k, dict):
                            k = [k.get("date",""), k.get("open",0), k.get("close",0),
                                 k.get("high",0), k.get("low",0), k.get("volume",0)]
                        else:
                            continue
                    except Exception:
                        continue
                result.append({
                    "datetime": str(k[0]),
                    "open": float(k[1]),
                    "close": float(k[2]),
                    "high": float(k[3]),
                    "low": float(k[4]),
                    "volume": float(k[5]) if k[5] else 0,
                    "amount": 0,
                })
            if len(result) >= 10:
                return result
    except Exception:
        pass

    # ── 备2：百度 K线（日线） ──
    try:
        bk = baidu_kline_with_ma(code)
        if bk and bk.get("klines") and len(bk["klines"]) >= 10:
            return [
                {
                    "datetime": k["time"],
                    "open": k["open"],
                    "high": k["high"],
                    "low": k["low"],
                    "close": k["close"],
                    "volume": k["volume"],
                    "amount": k.get("amount", 0),
                }
                for k in bk["klines"]
            ]
    except Exception:
        pass

    return []


# ── 腾讯实时行情 ──────────────────────────────────
TENCENT_URL = "https://qt.gtimg.cn/q="

def tencent_quote(codes: list[str]) -> dict[str, dict]:
    """
    实时行情 → 主:腾讯 → 备:新浪
    返回 {code: {name, price, mcap_yi, ...}}
    """
    if not codes:
        return {}

    codes_norm = [normalize_code(c) for c in codes]
    symbols = [f"{get_prefix(c)}{c}" for c in codes_norm]
    url = TENCENT_URL + ",".join(symbols)

    # ── 主：腾讯 ──
    for attempt in range(3):
        try:
            r = requests.get(url, timeout=15)
            r.encoding = "gbk"
            break
        except Exception:
            if attempt < 2:
                time.sleep(1)
            else:
                r = None
    result = {}

    if r is not None:
        for line in r.text.strip().split("\n"):
            if not line.strip():
                continue
            try:
                parts = line.split("~")
                if len(parts) < 46:
                    continue
                code = normalize_code(parts[2])
                result[code] = {
                    "name": parts[1], "code": code,
                    "price": float(parts[3]) if parts[3] else 0,
                    "last_close": float(parts[4]) if parts[4] else 0,
                    "open": float(parts[5]) if parts[5] else 0,
                    "volume": int(parts[6]) if parts[6] else 0,
                    "amount_wan": float(parts[37]) if parts[37] else 0,
                    "high": float(parts[33]) if parts[33] else 0,
                    "low": float(parts[34]) if parts[34] else 0,
                    "change_amt": float(parts[31]) if parts[31] else 0,
                    "change_pct": float(parts[32]) if parts[32] else 0,
                    "turnover_pct": float(parts[38]) if parts[38] else 0,
                    "pe_ttm": float(parts[39]) if parts[39] else 0,
                    "amplitude_pct": float(parts[43]) if parts[43] else 0,
                    "mcap_yi": float(parts[44]) if parts[44] else 0,
                    "float_mcap_yi": float(parts[45]) if parts[45] else 0,
                    "pb": float(parts[46]) if len(parts) > 46 and parts[46] else 0,
                    "limit_up": float(parts[47]) if len(parts) > 47 and parts[47] else 0,
                    "limit_down": float(parts[48]) if len(parts) > 48 and parts[48] else 0,
                    "vol_ratio": float(parts[49]) if len(parts) > 49 and parts[49] else 0,
                    "pe_static": float(parts[50]) if len(parts) > 50 and parts[50] else 0,
                    "servertime": parts[30],
                }
            except (ValueError, IndexError):
                continue

    if len(result) >= len(codes) * 0.5:
        return result

    # ── 备：新浪 ──
    try:
        sina_symbols = [f"{get_prefix(c)}{c}".upper() for c in codes_norm]
        sina_url = "http://hq.sinajs.cn/list=" + ",".join(sina_symbols)
        sr = requests.get(sina_url, headers={
            "User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn",
        }, timeout=10)
        sr.encoding = "gbk"
        for line in sr.text.strip().split("\n"):
            if not line or "hq_str_" not in line:
                continue
            try:
                # var hq_str_sh600519="name,open,last_close,current,high,low,..."
                eq = line.index("=")
                csv = line[eq+1:].strip().strip("\";")
                fields = csv.split(",")
                if len(fields) < 30:
                    continue
                code = normalize_code(line[line.index("_")+1:eq].strip())
                price = float(fields[3]) if fields[3] else 0
                last_close = float(fields[2]) if fields[2] else 0
                chg_pct = ((price - last_close) / last_close * 100) if last_close > 0 else 0
                mcap = float(fields[24]) if len(fields) > 24 and fields[24] else 0
                vol = int(fields[8]) if fields[8] else 0
                amount_wan = float(fields[9]) / 10000 if len(fields) > 9 and fields[9] else 0
                result[code] = {
                    "name": fields[0], "code": code,
                    "price": price, "last_close": last_close,
                    "open": float(fields[1]) if fields[1] else 0,
                    "high": float(fields[4]) if fields[4] else 0,
                    "low": float(fields[5]) if fields[5] else 0,
                    "volume": vol, "amount_wan": amount_wan,
                    "change_pct": round(chg_pct, 2),
                    "turnover_pct": float(fields[23]) if len(fields) > 23 and fields[23] else 0,
                    "mcap_yi": mcap,
                    "pe_ttm": float(fields[26]) if len(fields) > 26 and fields[26] else 0,
                    "servertime": f"{fields[30]} {fields[31]}" if len(fields) > 31 else "",
                }
            except (ValueError, IndexError, AttributeError):
                continue
    except Exception:
        pass

    return result


# ── 百度 K 线（带 MA）─────────────────────────────
def baidu_kline_with_ma(code: str, start_time: str = "") -> dict:
    """
    百度股市通 K 线，自带 MA5/MA10/MA20
    返回: {code, name, klines: [{time, open, close, high, low, volume, amount,
                                 ma5, ma10, ma20}]}
    """
    code = normalize_code(code)
    url = "https://gushitong.baidu.com/stock/ab-{}/".format(code)
    params = {"start": start_time} if start_time else {}
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        r = requests.get(url, params=params, headers=headers, timeout=10)
        data = r.json()
        klines = data.get("data", {}).get("kline", {}).get("list", [])
        return {
            "code": code,
            "name": data.get("data", {}).get("stockName", ""),
            "klines": [
                {
                    "time": k.get("time"),
                    "open": float(k.get("open", 0)),
                    "close": float(k.get("close", 0)),
                    "high": float(k.get("high", 0)),
                    "low": float(k.get("low", 0)),
                    "volume": float(k.get("volume", 0)),
                    "amount": float(k.get("amount", 0)),
                    "ma5": float(k.get("ma5avgprice", 0)),
                    "ma10": float(k.get("ma10avgprice", 0)),
                    "ma20": float(k.get("ma20avgprice", 0)),
                }
                for k in klines
                if k.get("time")
            ]
        }
    except Exception:
        return {"code": code, "name": "", "klines": []}


# ── 指数行情 ──────────────────────────────────────
INDEX_CODES = {
    "上证指数": "000001",
    "沪深300": "000300",
    "创业板指": "399006",
    "科创50": "000688",
}

def get_indices() -> dict[str, dict]:
    """获取主要指数行情"""
    codes = list(INDEX_CODES.values())
    quotes = tencent_quote(codes)
    result = {}
    name_map = {v: k for k, v in INDEX_CODES.items()}
    for code, q in quotes.items():
        result[name_map.get(code, code)] = q
    return result
