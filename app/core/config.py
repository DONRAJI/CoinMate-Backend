import os
from dotenv import load_dotenv

load_dotenv()

UPBIT_ACCESS = os.getenv("UPBIT_ACCESS") or os.getenv("UPBIT_ACCESS_KEY", "")
UPBIT_SECRET = os.getenv("UPBIT_SECRET") or os.getenv("UPBIT_SECRET_KEY", "")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")

# 🔥 [P0] 쓰기 엔드포인트(매수/매도/설정/봇제어) 보호용 API 키
# .env의 API_KEY와 일치하는 X-API-Key 헤더가 있어야 호출 허용
API_KEY = os.getenv("API_KEY", "")

# 🔥 [P0] CORS 허용 도메인 (쉼표 구분). 미설정 시 "*" (개발/전환용)
# 예: ALLOWED_ORIGINS=https://coinmate.vercel.app,https://www.mysite.com
_origins_raw = os.getenv("ALLOWED_ORIGINS", "").strip()
ALLOWED_ORIGINS = [o.strip() for o in _origins_raw.split(",") if o.strip()] if _origins_raw else ["*"]
