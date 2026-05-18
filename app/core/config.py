import os
from dotenv import load_dotenv

load_dotenv()

UPBIT_ACCESS = os.getenv("UPBIT_ACCESS") or os.getenv("UPBIT_ACCESS_KEY", "")
UPBIT_SECRET = os.getenv("UPBIT_SECRET") or os.getenv("UPBIT_SECRET_KEY", "")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
