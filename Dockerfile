# 경량 Python 3.12 이미지 사용
FROM python:3.12-slim

# 작업 디렉토리
WORKDIR /app

# 프로젝트 파일 복사
COPY . /app

# 의존성 설치 (requirements.txt가 없다면 직접 지정)
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir "mcp[server]" requests beautifulsoup4 cachetools

# 환경 변수 기본값 설정
ENV TRANSPORT=http
ENV CACHE_TTL_SECONDS=600
ENV RATE_LIMIT_INTERVAL=1.0

# 포트 노출 (Smithery에서 http.port=8000)
EXPOSE 8000

# 애플리케이션 실행
CMD ["python", "src/main.py"]