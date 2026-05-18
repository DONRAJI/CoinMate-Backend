import httpx
from app.core.config import DISCORD_WEBHOOK_URL
from app.core.logger import get_logger

log = get_logger("notifier")

async def send_discord(message: str):
    if not DISCORD_WEBHOOK_URL:
        return
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(DISCORD_WEBHOOK_URL, json={"content": message})
    except Exception as e:
        log.warning(f"Discord 전송 실패: {e}")

async def notify_buy(ticker: str, price: float, budget: float, strategy: str):
    await send_discord(
        f"**[매수]** {ticker}\n"
        f"가격: {price:,.0f}원 | 금액: {budget:,.0f}원\n"
        f"전략: {strategy}"
    )

async def notify_sell(ticker: str, price: float, profit: float, reason: str):
    emoji = "+" if profit >= 0 else ""
    await send_discord(
        f"**[매도]** {ticker}\n"
        f"가격: {price:,.0f}원 | 수익: {emoji}{profit:.2f}%\n"
        f"사유: {reason}"
    )

async def notify_error(source: str, error: str):
    await send_discord(f"**[ERROR]** {source}\n```{error[:500]}```")
