"""
API 키 인증 (P0 보안)
- 쓰기 엔드포인트(매수/매도/설정변경/봇제어)에만 적용
- .env의 API_KEY와 일치하는 X-API-Key 헤더 요구
- API_KEY가 비어있으면(미설정) 인증을 건너뜀 (로컬 개발 호환)
"""
from fastapi import Header, HTTPException
from app.core.config import API_KEY


async def verify_api_key(x_api_key: str | None = Header(default=None)):
    # 키가 설정되지 않은 환경(로컬 등)에서는 통과
    if not API_KEY:
        return
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")
