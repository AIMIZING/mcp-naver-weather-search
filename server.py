# server.py
import sys
import time
import json
import logging
import threading
from typing import Dict, Any, Optional

import requests
from bs4 import BeautifulSoup
from cachetools import TTLCache

from mcp.server.fastmcp import FastMCP
import os

# ─────────────────────────────────────────────────────────────
# 기본 설정
# ─────────────────────────────────────────────────────────────

CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "600"))
RATE_LIMIT_INTERVAL = float(os.getenv("RATE_LIMIT_INTERVAL", "1.0"))

mcp = FastMCP("Naver Weather MCP (Scraping)")

LOG_LEVEL = logging.INFO
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger("naver-weather")

# 네이버 검색 URL 템플릿
SEARCH_URL = "https://search.naver.com/search.naver?query={query}"

# HTTP 설정
DEFAULT_TIMEOUT = 6.0
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# 캐시/레이트리밋
CACHE_TTL_SECONDS = 600  # 10분
cache: TTLCache[str, Dict[str, Any]] = TTLCache(maxsize=256, ttl=CACHE_TTL_SECONDS)

RATE_LIMIT_INTERVAL = 1.0  # 초당 1회
_last_request_ts = 0.0
_rl_lock = threading.Lock()

# 재시도
MAX_RETRIES = 3
BACKOFF_BASE = 0.8  # 지수 백오프 시작(초)

# 선택자(버전 관리 가능)
SELECTORS = {
    "temp_primary": [".temperature_text > strong"],  # 예: '23°'
    "status_primary": [".weather_main"],            # 예: '맑음'
    "sensible_temp": [".temperature_info .sensible em"],  # '체감온도 20°'
    # 보조 선택자 (DOM 변경 시 추가)
    "temp_fallback": ["span.temperature_text strong", ".temperature_text"],
    "status_fallback": [".status .weather", ".status", ".weather"],
    "humidity_guess_blocks": [
        ".summary_list", ".weather_info", ".temperature_info"
    ],
}

# ─────────────────────────────────────────────────────────────
# 유틸: 레이트리밋 / 요청 / 파싱
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
    """요청 재시도 + 지수 백오프로 HTML 가져오기"""
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "ko-KR,ko;q=0.9"}
    err: Optional[Exception] = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            _rate_limit()
            resp = requests.get(url, headers=headers, timeout=DEFAULT_TIMEOUT)
            # 429/5xx는 재시도
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
    """'습도' 텍스트를 포함한 숫자 추정(레이아웃 변화 대비 완화)"""
    import re
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
    # 예: "23°" or "23도" → "23°C" 형태로
    t = txt.replace("도", "").replace(" ", "").replace("°", "")
    if t.startswith("+"): t = t[1:]
    return f"{t}°C" if t else txt

def _parse_weather(html: str, region: str) -> Dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")

    temp = _first_text(soup, SELECTORS["temp_primary"]) or _first_text(soup, SELECTORS["temp_fallback"])
    status = _first_text(soup, SELECTORS["status_primary"]) or _first_text(soup, SELECTORS["status_fallback"])
    sensible = _first_text(soup, SELECTORS["sensible_temp"])
    humidity = _guess_humidity(soup)

    if temp:
        temp = _normalize_temp(temp)

    parsed = {
        "region": region,
        "status": status,
        "temperature": temp,
        "sensible_temperature": sensible,
        "humidity": humidity,
        "source": SEARCH_URL.format(query=f"{region}+날씨"),
        "timestamp": int(time.time()),
    }
    return parsed

def _format_text(data: Dict[str, Any]) -> str:
    lines = [f"[네이버 날씨] {data.get('region','-')}"]
    if data.get("status"): lines.append(f"- 상태: {data['status']}")
    if data.get("temperature"): lines.append(f"- 기온: {data['temperature']}")
    if data.get("sensible_temperature"): lines.append(f"- 체감온도: {data['sensible_temperature']}")
    if data.get("humidity"): lines.append(f"- 습도: {data['humidity']}")
    if data.get("source"): lines.append(f"- 참고: {data['source']}")
    # 선택자 실패 대비
    if len(lines) <= 2:
        lines.append("- 안내: 일부 정보 수집에 실패했습니다. 잠시 후 다시 시도해 주세요.")
        if data.get("source"): lines.append(f"- 참고: {data['source']}")
    return "\n".join(lines)

# ─────────────────────────────────────────────────────────────
# MCP Tool
# ─────────────────────────────────────────────────────────────
@mcp.tool(
    name="get_weather_by_region",
    description="지역명을 받아 네이버 검색 결과(날씨 모듈)에서 현재 상태/기온 등을 조회합니다. format='text'|'json'"
)
def get_weather_by_region(region: str, format: str = "text") -> str:
    """
    Args:
        region: 조회할 지역명 (예: '서울', '부산 해운대', 'Jeju')
        format: 'text' 또는 'json' (기본: text)
    """
    region_key = region.strip()
    if not region_key:
        return "지역명이 비어 있습니다. 예: region='서울'"

    # 캐시
    if region_key in cache:
        data = cache[region_key]
        log.info(f"[cache] hit for region='{region_key}'")
    else:
        try:
            url = SEARCH_URL.format(query=f"{region_key}+날씨")
            html = _fetch_html(url)
            data = _parse_weather(html, region_key)
            cache[region_key] = data
        except Exception as e:
            log.exception("weather fetch/parse failed")
            # 축약 오류 메시지(내부 정보 노출 방지)
            return f"[오류] 날씨 정보를 가져오는 중 문제가 발생했습니다. 잠시 후 다시 시도해 주세요. (reason: {str(e)[:120]})"

    if format.lower() == "json":
        return json.dumps(data, ensure_ascii=False, indent=2)
    return _format_text(data)

# (선택) 리소스: 제공 필드 안내
@mcp.resource(
    uri="naver://weather/fields",
    name="supported_fields",
    description="이 MCP가 반환 가능한 필드 목록을 제공합니다."
)
def supported_fields() -> Dict[str, Any]:
    return {
        "fields": ["region", "status", "temperature", "sensible_temperature", "humidity", "source", "timestamp"],
        "cache_ttl_seconds": CACHE_TTL_SECONDS,
        "rate_limit_seconds": RATE_LIMIT_INTERVAL,
    }

# ─────────────────────────────────────────────────────────────
# 실행
# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("🔧 Naver Weather MCP (scraping) starting…", file=sys.stderr)
    mcp.run(transport="stdio")
