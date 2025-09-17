# src/main.py
import os
import re
import time
import json
import logging
import threading
from typing import Dict, Any, Optional

import requests
from bs4 import BeautifulSoup
from cachetools import TTLCache
from mcp.server.fastmcp import FastMCP

# ─────────────────────────────────────────────────────────────
# MCP 서버 인스턴스 생성
# ─────────────────────────────────────────────────────────────
mcp = FastMCP("Naver Weather MCP (STDIO)")

# ─────────────────────────────────────────────────────────────
# 기본 설정/로깅
# ─────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger("naver-weather")

CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "600"))        # 10분
RATE_LIMIT_INTERVAL = float(os.getenv("RATE_LIMIT_INTERVAL", "1.0"))  # 초당 1회
DEFAULT_TIMEOUT = 6.0

SEARCH_URL = "https://search.naver.com/search.naver?query={query}"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# 캐시/레이트리밋
cache: TTLCache[str, Dict[str, Any]] = TTLCache(maxsize=256, ttl=CACHE_TTL_SECONDS)
_last_request_ts = 0.0
_rl_lock = threading.Lock()

# 재시도
MAX_RETRIES = 3
BACKOFF_BASE = 0.8  # 지수 백오프 시작(초)

SELECTORS = {
    "temp_primary": [".temperature_text > strong"],
    "status_primary": [".weather_main"],
    "sensible_temp": [".temperature_info .sensible em"],
    "temp_fallback": ["span.temperature_text strong", ".temperature_text"],
    "status_fallback": [".status .weather", ".status", ".weather"],
    "humidity_guess_blocks": [".summary_list", ".weather_info", ".temperature_info"],
}

# ─────────────────────────────────────────────────────────────
# 유틸 함수
# ─────────────────────────────────────────────────────────────
def _rate_limit():
    global _last_request_ts
    with _rl_lock:
        now = time.monotonic()
        wait = (_last_request_ts + RATE_LIMIT_INTERVAL) - now
        if wait > 0:
            time.sleep(wait)
        _last_request_ts = time.monotonic()

def _fetch_html(url: str) -> str:
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "ko-KR,ko;q=0.9"}
    err: Optional[Exception] = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            _rate_limit()
            resp = requests.get(url, headers=headers, timeout=DEFAULT_TIMEOUT)
            if resp.status_code >= 500 or resp.status_code == 429:
                raise requests.HTTPError(f"HTTP {resp.status_code}: {resp.text[:200]}")
            resp.raise_for_status()
            return resp.text
        except Exception as e:
            err = e
            sleep_for = BACKOFF_BASE * (2 ** (attempt - 1))
            log.warning(f"[fetch] attempt {attempt}/{MAX_RETRIES} failed: {e}. backoff {sleep_for:.1f}s")
            time.sleep(sleep_for)
    raise RuntimeError(f"Failed to fetch after retries: {err}")

def _first_text(soup: BeautifulSoup, selectors: list[str]) -> Optional[str]:
    for sel in selectors:
        el = soup.select_one(sel)
        if el:
            txt = el.get_text(strip=True)
            if txt:
                return txt
    return None

def _guess_humidity(soup: BeautifulSoup) -> Optional[str]:
    for block_sel in SELECTORS["humidity_guess_blocks"]:
        block = soup.select_one(block_sel)
        if not block:
            continue
        text = block.get_text(" ", strip=True)
        m = re.search(r"습도\s*([0-9]{1,3})\s*%?", text)
        if m:
            return m.group(1) + "%"
    return None

def _normalize_temp(txt: str) -> str:
    clean = re.sub(r"^[^\d\-\+]*", "", txt)
    t = clean.replace("도", "").replace(" ", "").replace("°", "")
    if t.startswith("+"):
        t = t[1:]
    return f"{t}°C" if t else txt

def _parse_weather(html: str, region: str) -> Dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    temp = _first_text(soup, SELECTORS["temp_primary"]) or _first_text(soup, SELECTORS["temp_fallback"])
    status = _first_text(soup, SELECTORS["status_primary"]) or _first_text(soup, SELECTORS["status_fallback"])
    sensible = _first_text(soup, SELECTORS["sensible_temp"])
    humidity = _guess_humidity(soup)
    if temp:
        temp = _normalize_temp(temp)
    return {
        "region": region,
        "status": status,
        "temperature": temp,
        "sensible_temperature": sensible,
        "humidity": humidity,
        "source": SEARCH_URL.format(query=f"{region}+날씨"),
        "timestamp": int(time.time()),
    }

def _format_text(data: Dict[str, Any]) -> str:
    lines = [f"[네이버 날씨] {data.get('region','-')}"]
    if data.get("status"): lines.append(f"- 상태: {data['status']}")
    if data.get("temperature"): lines.append(f"- 기온: {data['temperature']}")
    if data.get("sensible_temperature"): lines.append(f"- 체감온도: {data['sensible_temperature']}")
    if data.get("humidity"): lines.append(f"- 습도: {data['humidity']}")
    if data.get("source"): lines.append(f"- 참고: {data['source']}")
    if len(lines) <= 2:
        lines.append("- 안내: 일부 정보 수집에 실패했습니다. 잠시 후 다시 시도해 주세요.")
        if data.get("source"): lines.append(f"- 참고: {data['source']}")
    return "\n".join(lines)

# ─────────────────────────────────────────────────────────────
# MCP 툴 및 리소스 등록
# ─────────────────────────────────────────────────────────────
@mcp.tool(name="get_weather_by_region", description="지역명을 받아 네이버 검색 결과(날씨 모듈)에서 현재 상태/기온 등을 조회합니다. format='text'|'json'")
def get_weather_by_region(region: str, format: str = "text") -> str:
    region_key = (region or "").strip()
    if not region_key:
        return "지역명이 비어 있습니다. 예: region='서울'"

    if region_key in cache:
        data = cache[region_key]
        log.info(f"[cache] hit for region='{region_key}'")
    else:
        try:
            html = _fetch_html(SEARCH_URL.format(query=f"{region_key}+날씨"))
            data = _parse_weather(html, region_key)
            cache[region_key] = data
        except Exception as e:
            log.exception("weather fetch/parse failed")
            return f"[오류] 날씨 정보를 가져오는 중 문제가 발생했습니다. 잠시 후 다시 시도해 주세요. (reason: {str(e)[:120]})"

    if (format or "").lower() == "json":
        return json.dumps(data, ensure_ascii=False, indent=2)
    return _format_text(data)

@mcp.resource(uri="naver://weather/fields", name="supported_fields", description="이 MCP가 반환 가능한 필드 목록을 제공합니다.")
def supported_fields() -> Dict[str, Any]:
    return {
        "fields": ["region", "status", "temperature", "sensible_temperature", "humidity", "source", "timestamp"],
        "cache_ttl_seconds": CACHE_TTL_SECONDS,
        "rate_limit_seconds": RATE_LIMIT_INTERVAL,
    }

# ─────────────────────────────────────────────────────────────
# STDIO 서버 실행
# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    mcp.run()
