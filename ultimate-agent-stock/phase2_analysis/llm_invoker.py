"""LLM 调用封装 — 统一调 DeepSeek API"""
from config import get_config
import requests
import json
import time


def llm_complete(prompt: str, system_prompt: str = "", temperature: float = None) -> str:
    """
    调用 LLM（最多重试3次），失败返回空字符串（调用方自行降级）

    返回空串的原因可能是：API Key未配/网络超时/HTTP错误/响应格式异常
    """
    cfg = get_config().get("llm", {})
    api_key = cfg.get("api_key")
    if not api_key:
        return ""

    api_base = cfg.get("api_base", "https://api.deepseek.com/v1")
    model = cfg.get("model", "deepseek-chat")
    temp = temperature if temperature is not None else cfg.get("temperature", 0.3)
    max_tokens = cfg.get("max_tokens", 4096)

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    payload = {
        "model": model,
        "messages": messages,
        "temperature": temp,
        "max_tokens": max_tokens,
    }

    # 指数退避重试3次
    for attempt in range(3):
        try:
            r = requests.post(
                f"{api_base.rstrip('/')}/chat/completions",
                headers=headers, json=payload, timeout=60,
            )
            if r.status_code == 429:
                # 限流：等更久
                wait = 5 * (2 ** attempt)
                print(f"  ⚠️ [LLM] 限流(429), {wait}s后重试({attempt+1}/3)")
                time.sleep(wait)
                continue
            if r.status_code == 503:
                wait = 3 * (2 ** attempt)
                print(f"  ⚠️ [LLM] 服务不可用(503), {wait}s后重试({attempt+1}/3)")
                time.sleep(wait)
                continue
            if r.status_code != 200:
                print(f"  ⚠️ [LLM] HTTP {r.status_code}, 放弃")
                return ""

            data = r.json()
            if "choices" not in data or not data["choices"]:
                print(f"  ⚠️ [LLM] 响应无choices字段, 放弃")
                return ""
            return data["choices"][0]["message"]["content"]

        except requests.Timeout:
            wait = 3 * (2 ** attempt)
            print(f"  ⚠️ [LLM] 超时, {wait}s后重试({attempt+1}/3)")
            time.sleep(wait)
        except requests.ConnectionError:
            print(f"  ⚠️ [LLM] 连接失败, 放弃")
            return ""
        except Exception as e:
            if attempt < 2:
                time.sleep(2)
                continue
            print(f"  ⚠️ [LLM] 异常: {e}, 放弃")
            return ""

    return ""


def llm_json(prompt: str, system_prompt: str = "", temperature: float = 0.1) -> dict:
    """
    调用 LLM 并解析 JSON 返回

    返回:
        解析后的 dict，失败时返回 {"error": str}
    """
    sys_msg = (system_prompt + "\n请严格以 JSON 格式输出，不要包含其他内容。") if not system_prompt.startswith("请严格以 JSON") else system_prompt
    if "JSON" not in sys_msg:
        sys_msg += "\n请严格以 JSON 格式输出，不要包含其他文字。"

    result = llm_complete(prompt, sys_msg, temperature)
    try:
        return json.loads(result)
    except json.JSONDecodeError:
        try:
            start = result.index("{")
            end = result.rindex("}") + 1
            return json.loads(result[start:end])
        except (ValueError, json.JSONDecodeError):
            return {"error": "LLM 返回非 JSON", "raw": result[:200]}
