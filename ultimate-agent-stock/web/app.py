"""Web Dashboard — FastAPI 应用（含多用户）"""
import sys, json, os, time, asyncio
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.resolve()))

from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# ── 扫描进度追踪 ──
SCAN_PROGRESS_FILE = Path(__file__).parent.parent / "data_store" / "scan_progress.json"
SCAN_RUNNING = {"status": False, "task_id": None}

def _write_progress(step: str, pct: int, detail: str = ""):
    """写入扫描进度供前端轮询"""
    try:
        SCAN_PROGRESS_FILE.parent.mkdir(parents=True, exist_ok=True)
        SCAN_PROGRESS_FILE.write_text(json.dumps({
            "step": step, "pct": min(100, pct), "detail": detail,
            "timestamp": time.time(),
        }))
    except Exception:
        pass

from agent_orch.context import get_intel_context, reset_intel_context
from phase4_position.position_trader import generate_suggestions
from phase1_scan.market_regime import detect_market_regime
from self_learn.signal_tracker import get_track_record
from agent_orch.agent_base import UltimatePortfolio, StockPick
from auth.user_manager import login as auth_login, logout as auth_logout
from auth.user_manager import get_user, create_user, get_user_positions
from auth.user_manager import save_user_positions

_PORTFOLIO_FILE = Path(__file__).parent.parent / "data_store" / "latest_portfolio.json"

def _load_portfolio():
    try:
        if _PORTFOLIO_FILE.exists():
            with open(_PORTFOLIO_FILE) as f:
                data = json.load(f)
            if data:
                portfolio = UltimatePortfolio(
                    market_regime=data.get("market_regime", ""),
                    market_temperature=data.get("market_temperature", 50),
                    short_term=[StockPick(**s) for s in data.get("short_term", [])],
                    medium_term=[StockPick(**s) for s in data.get("medium_term", [])],
                    long_term=[StockPick(**s) for s in data.get("long_term", [])],
                )
                return portfolio
    except Exception:
        pass
    return None

def _save_portfolio(p):
    try:
        _PORTFOLIO_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(_PORTFOLIO_FILE, "w") as f:
            json.dump({
                "market_regime": p.market_regime,
                "market_temperature": p.market_temperature,
                "short_term": [{"code": s.code, "name": s.name, "score": s.score, "current_price": s.current_price, "change_pct": s.change_pct, "last_close": s.last_close,
                                "reason": s.reason, "entry_zone": list(s.entry_zone) if s.entry_zone else None,
                                "stop_loss": s.stop_loss, "target_price": s.target_price,
                                "expected_hold_days": s.expected_hold_days,
                                "score_by_analyst": s.score_by_analyst} for s in p.short_term],
                "medium_term": [{"code": s.code, "name": s.name, "score": s.score, "current_price": s.current_price, "change_pct": s.change_pct, "last_close": s.last_close,
                                 "reason": s.reason, "score_by_analyst": s.score_by_analyst} for s in p.medium_term],
                "long_term": [{"code": s.code, "name": s.name, "score": s.score, "current_price": s.current_price, "change_pct": s.change_pct, "last_close": s.last_close,
                               "reason": s.reason, "score_by_analyst": s.score_by_analyst} for s in p.long_term],
            }, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

_ANALYST_REPORTS_FILE = Path(__file__).parent.parent / "data_store" / "latest_analyst_reports.json"

_TRADER_FILE = Path(__file__).parent.parent / "data_store" / "latest_trader_setups.json"

def _load_trader_setups() -> list:
    try:
        if _TRADER_FILE.exists():
            with open(_TRADER_FILE) as f:
                return json.load(f)
    except Exception:
        pass
    return []

def _load_analyst_reports() -> dict:
    try:
        if _ANALYST_REPORTS_FILE.exists():
            with open(_ANALYST_REPORTS_FILE) as f:
                return json.load(f)
    except Exception:
        pass
    return {}

app = FastAPI(title="究极 Agent 选股")

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))
static = HERE / "static"
if static.exists():
    app.mount("/static", StaticFiles(directory=str(static)), name="static")


def _get_current_user(request: Request) -> dict:
    """从 cookie 获取当前用户"""
    token = request.cookies.get("session")
    if not token:
        return None
    return get_user(token)


# ═══════════════════════════════════════════
# 登录页
# ═══════════════════════════════════════════
@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    user = _get_current_user(request)
    error = request.query_params.get("error", "")
    return templates.TemplateResponse("login.html", {
        "request": request,
        "user": user,
        "error": error,
    })


@app.post("/login")
async def login_submit(request: Request):
    data = await request.form()
    username = data.get("username", "").strip()
    password = data.get("password", "").strip()
    remember = data.get("remember") == "on"
    token = auth_login(username, password)
    if token:
        resp = RedirectResponse(url="/", status_code=303)
        max_age = 86400 * 30 if remember else 86400 * 1
        resp.set_cookie(key="session", value=token, max_age=max_age, httponly=True)
        return resp
    return RedirectResponse(url="/login?error=用户名或密码错误", status_code=303)


@app.post("/logout")
async def logout_submit(request: Request):
    token = request.cookies.get("session")
    if token:
        auth_logout(token)
    resp = RedirectResponse(url="/login", status_code=303)
    resp.delete_cookie("session")
    return resp


# ═══════════════════════════════════════════
# 主页
# ═══════════════════════════════════════════
@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    user = _get_current_user(request)
    if not user:
        return RedirectResponse(url="/login")

    regime = detect_market_regime()
    intel = get_intel_context()
    positions = get_user_positions(user["username"])
    suggestions = generate_suggestions(positions) if positions else []

    portfolio = _load_portfolio()
    analyst_reports = _load_analyst_reports()
    trader_setups = _load_trader_setups()

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "regime": regime,
        "intel": intel,
        "positions": positions,
        "suggestions": suggestions,
        "portfolio": portfolio,
        "user": user,
        "analyst_reports": analyst_reports,
        "trader_setups": trader_setups,
        "chain_reports": _load_chain_reports(),
        "sector_stocks": _SECTOR_STOCKS,
    })


@app.get("/briefing", response_class=HTMLResponse)
async def briefing_page(request: Request):
    user = _get_current_user(request)
    if not user:
        return RedirectResponse(url="/login")
    intel = get_intel_context()
    return templates.TemplateResponse("briefing.html", {
        "request": request,
        "intel": intel,
        "user": user,
    })


@app.get("/positions", response_class=HTMLResponse)
async def positions_page(request: Request):
    user = _get_current_user(request)
    if not user:
        return RedirectResponse(url="/login")
    positions = get_user_positions(user["username"])
    suggestions = generate_suggestions(positions) if positions else []
    return templates.TemplateResponse("positions.html", {
        "request": request,
        "positions": positions,
        "suggestions": suggestions,
        "user": user,
    })


@app.get("/track", response_class=HTMLResponse)
async def track_page(request: Request):
    user = _get_current_user(request)
    if not user:
        return RedirectResponse(url="/login")
    record = get_track_record(days=30)
    return templates.TemplateResponse("track.html", {
        "request": request,
        "record": record,
        "user": user,
    })


# ═══════════════════════════════════════════
# API
# ═══════════════════════════════════════════
@app.post("/api/positions/add")
async def add_position(request: Request):
    user = _get_current_user(request)
    if not user:
        return RedirectResponse(url="/login")
    data = await request.form()
    positions = get_user_positions(user["username"])
    code = data.get("code", "").strip().zfill(6)
    name = data.get("name", "").strip()
    if code and (not name or name in ("查询失败", "未识别", "查询中…")):
        from data.market_data import tencent_quote
        q = tencent_quote([code])
        info = q.get(code, {})
        name = info.get("name", name)
    new_pos = {"code": code, "name": name,
               "price": float(data.get("price", 0)),
               "shares": int(data.get("shares", 0)),
               "entry_date": data.get("entry_date", "")}
    if code and new_pos["price"] > 0:
        positions = [p for p in positions if p["code"] != code]
        positions.append(new_pos)
        save_user_positions(user["username"], positions)
    return RedirectResponse(url="/positions", status_code=303)


@app.post("/api/positions/delete")
async def delete_position(request: Request):
    user = _get_current_user(request)
    if not user:
        return RedirectResponse(url="/login")
    data = await request.form()
    code = data.get("code", "").strip()
    positions = get_user_positions(user["username"])
    positions = [p for p in positions if p["code"] != code]
    save_user_positions(user["username"], positions)
    return RedirectResponse(url="/positions", status_code=303)


@app.post("/api/push-dingtalk")
async def push_dingtalk():
    """推送今日选股到钉钉"""
    from main import cmd_scan, cmd_briefing
    import argparse
    from phase5_push.dingtalk_push import dingtalk_push, dingtalk_markdown

    briefing_args = argparse.Namespace(push=False, time=None, mode="full", code="")
    briefing = await cmd_briefing(briefing_args)

    scan_args = argparse.Namespace(push=False, time=None, mode="full", code="")
    portfolio = await cmd_scan(scan_args)

    content = f"📊 今日选股报告\n\n"
    content += f"🔥 短期爆发:\n"
    for sp in portfolio.short_term:
        content += f"  {sp.code} {sp.name} 评分{sp.score}\n"
    content += f"\n📈 中期趋势:\n"
    for sp in portfolio.medium_term:
        content += f"  {sp.code} {sp.name} 评分{sp.score}\n"
    content += f"\n🌳 长期持有:\n"
    for sp in portfolio.long_term:
        content += f"  {sp.code} {sp.name} 评分{sp.score}\n"

    ok = dingtalk_push("📊 选股报告", content)
    return RedirectResponse(url="/", status_code=303)



# 产业链推荐
# 板块→股票静态映射
from data.sector_stocks import SECTOR_STOCKS as _SECTOR_STOCKS

_CHAIN_FILE = Path(__file__).parent.parent / "data_store" / "latest_chain_reports.json"
def _load_chain_reports() -> list:
    try:
        if _CHAIN_FILE.exists():
            with open(_CHAIN_FILE) as f:
                return json.load(f)
    except: pass
    return []

@app.post("/api/quick-add-position")
async def quick_add_position(request: Request):
    user = _get_current_user(request)
    if not user:
        return RedirectResponse(url="/login")
    data = await request.form()
    code = data.get("code", "").strip().zfill(6)
    name = data.get("name", "").strip()
    price_str = data.get("price", "0")
    try: price = float(price_str)
    except: price = 0

    from data.market_data import tencent_quote
    if code and not name:
        q = tencent_quote([code])
        info = q.get(code, {})
        name = info.get("name", name)
    if price <= 0:
        q = tencent_quote([code])
        info = q.get(code, {})
        price = info.get("price", 0)

    if code and price > 0:
        from datetime import datetime
        positions = get_user_positions(user["username"])
        new_pos = {"code": code, "name": name, "price": round(price, 3),
                   "shares": int(data.get("shares", 100)), "entry_date": datetime.now().strftime("%Y-%m-%d")}
        positions = [p for p in positions if p["code"] != code]
        positions.append(new_pos)
        save_user_positions(user["username"], positions)
    return RedirectResponse(url="/", status_code=303)

@app.post("/api/run-briefing")
async def run_briefing():
    from main import cmd_briefing
    import argparse
    reset_intel_context()
    args = argparse.Namespace(push=False, time=None, mode="full", code="")
    await cmd_briefing(args)
    return RedirectResponse(url="/briefing", status_code=303)


@app.post("/api/run-scan")
async def run_scan(request: Request):
    from main import cmd_scan
    import argparse

    # 读取前端选择的模式
    form = await request.form()
    mode = form.get("mode", "auto")

    _write_progress("初始化", 0, "准备启动扫描...")
    SCAN_RUNNING["status"] = True

    args = argparse.Namespace(push=False, time=None, mode=mode, code="")
    try:
        portfolio = await cmd_scan(args)
    finally:
        _write_progress("完成", 100, "")
        SCAN_RUNNING["status"] = False

    return RedirectResponse(url="/", status_code=303)


@app.get("/api/scan-progress")
async def scan_progress():
    """前端轮询进度"""
    if not SCAN_RUNNING["status"]:
        pf = Path(__file__).parent.parent / "data_store" / "latest_portfolio.json"
        if pf.exists():
            return JSONResponse({"running": False, "done": True, "step": "完成", "pct": 100, "detail": ""})
        return JSONResponse({"running": False, "done": False, "step": "", "pct": 0, "detail": ""})

    try:
        if SCAN_PROGRESS_FILE.exists():
            data = json.loads(SCAN_PROGRESS_FILE.read_text())
            return JSONResponse({"running": True, **data})
    except Exception:
        pass
    return JSONResponse({"running": True, "step": "扫描中...", "pct": 0, "detail": ""})


@app.get("/api/status")
async def api_status():
    regime = detect_market_regime()
    return {
        "market_regime": regime.get("regime"),
        "temperature": regime.get("temperature"),
        "intel_populated": get_intel_context().is_populated(),
    }


def run(port: int = 8080):
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=port)
