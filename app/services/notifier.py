"""
Discord 알림 모음 — Embed(색상/필드) 포맷.
색상 가이드:
- 🟢 green   (성공/매수/복구): 0x2ECC71
- 🔵 blue    (정보/요약):       0x3498DB
- 🟡 yellow  (경고/주의):        0xF1C40F
- 🔴 red     (위험/에러):        0xE74C3C
- 🟣 purple  (시스템/lifecycle): 0x9B59B6
- ⚫ gray    (중립):              0x95A5A6
"""
import httpx
from datetime import datetime, timezone, timedelta
from app.core.config import DISCORD_WEBHOOK_URL
from app.core.logger import get_logger

log = get_logger("notifier")
KST = timezone(timedelta(hours=9))

COLORS = {
    "green": 0x2ECC71,
    "blue": 0x3498DB,
    "yellow": 0xF1C40F,
    "red": 0xE74C3C,
    "purple": 0x9B59B6,
    "gray": 0x95A5A6,
}


async def _send_embed(title: str, description: str = "", color: int = COLORS["gray"],
                       fields: list[dict] | None = None, footer: str | None = None):
    """공용 embed 전송. 실패해도 봇은 계속 동작."""
    if not DISCORD_WEBHOOK_URL:
        return
    embed = {
        "title": title[:256],
        "color": color,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if description:
        embed["description"] = description[:2000]
    if fields:
        embed["fields"] = [
            {"name": f["name"][:256], "value": str(f["value"])[:1024],
             "inline": f.get("inline", True)}
            for f in fields[:25]
        ]
    if footer:
        embed["footer"] = {"text": footer[:2048]}
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(DISCORD_WEBHOOK_URL, json={"embeds": [embed]})
    except Exception as e:
        log.warning(f"Discord 전송 실패: {e}")


async def _send_text(message: str):
    """간단 텍스트 fallback."""
    if not DISCORD_WEBHOOK_URL:
        return
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(DISCORD_WEBHOOK_URL, json={"content": message[:2000]})
    except Exception as e:
        log.warning(f"Discord 전송 실패: {e}")


# ─────────────────── 매매 알림 (기존 호환) ───────────────────
async def notify_buy(ticker: str, price: float, budget: float, strategy: str):
    await _send_embed(
        title=f"🟢 매수 체결 · {ticker}",
        color=COLORS["green"],
        fields=[
            {"name": "체결가", "value": f"{price:,.0f}원"},
            {"name": "투입금액", "value": f"{budget:,.0f}원"},
            {"name": "전략", "value": strategy, "inline": False},
        ],
    )


async def notify_sell(ticker: str, price: float, profit: float, reason: str):
    color = COLORS["green"] if profit >= 0 else COLORS["red"]
    emoji = "🟢" if profit >= 0 else "🔴"
    sign = "+" if profit >= 0 else ""
    await _send_embed(
        title=f"{emoji} 매도 체결 · {ticker}",
        color=color,
        fields=[
            {"name": "체결가", "value": f"{price:,.0f}원"},
            {"name": "수익률", "value": f"{sign}{profit:.2f}%"},
            {"name": "사유", "value": reason, "inline": False},
        ],
    )


async def notify_error(source: str, error: str):
    await _send_embed(
        title=f"🚨 오류 · {source}",
        description=f"```{error[:1500]}```",
        color=COLORS["red"],
    )


# ─────────────────── 신규 알림 ───────────────────
async def notify_lifecycle(event: str, detail: str = ""):
    """시작/종료/재시작 등 lifecycle 알림."""
    emoji_map = {"startup": "🟣", "shutdown": "⚫", "restart": "🟣"}
    color_map = {"startup": COLORS["purple"], "shutdown": COLORS["gray"], "restart": COLORS["purple"]}
    label = {"startup": "서버 시작", "shutdown": "서버 종료", "restart": "재시작"}.get(event, event)
    await _send_embed(
        title=f"{emoji_map.get(event, '⚫')} {label}",
        description=detail or None,
        color=color_map.get(event, COLORS["gray"]),
    )


async def notify_critical_news(ticker: str, title: str, url: str, source: str,
                                position_info: str | None = None):
    """[Phase 1C] 보유 코인 critical 뉴스 즉시 알림."""
    fields = [
        {"name": "출처", "value": source},
        {"name": "코인", "value": ticker},
        {"name": "기사", "value": f"[{title[:200]}]({url})", "inline": False},
    ]
    if position_info:
        fields.append({"name": "현재 포지션", "value": position_info, "inline": False})
    await _send_embed(
        title=f"🚨 보유 코인 치명적 뉴스 · {ticker}",
        description="⚠️ 즉시 확인 권장 — 수동 매도 검토 필요",
        color=COLORS["red"],
        fields=fields,
    )


async def notify_daily_summary(stats: dict):
    """일일 요약 (자정 직전)."""
    pnl = stats.get("today_pnl", 0)
    pnl_color = COLORS["green"] if pnl >= 0 else COLORS["red"]
    pnl_emoji = "📈" if pnl >= 0 else "📉"
    sign = "+" if pnl >= 0 else ""
    fields = [
        {"name": "오늘 거래", "value": f"{stats.get('today_trades', 0)}건"},
        {"name": "오늘 승률", "value": f"{stats.get('today_win_rate', 0):.0f}%"},
        {"name": "오늘 손익", "value": f"{sign}{pnl:,.0f}원"},
        {"name": "총 자산", "value": f"{stats.get('total_assets', 0):,.0f}원"},
        {"name": "보유 코인", "value": f"{stats.get('open_count', 0)}건"},
        {"name": "주문가능 KRW", "value": f"{stats.get('krw_balance', 0):,.0f}원"},
    ]
    if stats.get("regime"):
        fields.append({"name": "BTC 레짐", "value": stats["regime"], "inline": False})
    await _send_embed(
        title=f"{pnl_emoji} 일일 요약 · {datetime.now(KST).strftime('%m/%d')}",
        color=pnl_color,
        fields=fields,
        footer="매일 자정 직전 자동 전송",
    )


async def notify_ml_trained(score: float, cal_diff_pct: float | None, n_train: int,
                              prev_score: float | None = None):
    """매일 ML 학습 완료 후 결과."""
    fields = [
        {"name": "정확도", "value": f"{score:.1f}%"},
        {"name": "학습 표본", "value": f"{n_train:,}건"},
    ]
    if prev_score is not None:
        diff = score - prev_score
        sign = "+" if diff >= 0 else ""
        fields.append({"name": "이전 대비", "value": f"{sign}{diff:.2f}%p"})
    if cal_diff_pct is not None:
        fields.append({"name": "calibration error", "value": f"{cal_diff_pct:+.1f}%p"})
    color = COLORS["blue"]
    if prev_score and score < prev_score - 10:
        color = COLORS["yellow"]  # 급락 경고
    await _send_embed(
        title="🤖 ML 학습 완료",
        color=color,
        fields=fields,
        footer="매일 자정 자동 실행",
    )


async def notify_regime_change(prev: str, new: str, btc_detail: dict | None = None):
    """BTC 레짐 전환 알림."""
    emoji = {"bull": "🐂", "neutral": "😐", "bear": "🐻"}
    label = {"bull": "상승장", "neutral": "중립", "bear": "하락장"}
    color_map = {"bull": COLORS["green"], "neutral": COLORS["gray"], "bear": COLORS["red"]}
    fields = [
        {"name": "이전", "value": f"{emoji.get(prev,'?')} {label.get(prev, prev)}"},
        {"name": "현재", "value": f"{emoji.get(new,'?')} {label.get(new, new)}"},
    ]
    if btc_detail:
        fields.append({"name": "BTC 가격", "value": f"{btc_detail.get('btc_price', 0):,.0f}원", "inline": False})
        fields.append({"name": "MA24 이격", "value": f"{btc_detail.get('ma24_dev_pct', 0):+.2f}%"})
        fields.append({"name": "6h 모멘텀", "value": f"{btc_detail.get('mom6_pct', 0):+.2f}%"})
    note = ""
    if new == "bear":
        note = "⚠️ 신규 매수 전면 차단"
    elif new == "bull":
        note = "✅ 정상 매수 진행"
    elif new == "neutral":
        note = "⚖️ 엄격 진입 (score≥6.5, ML≥60%)"
    await _send_embed(
        title=f"🌐 BTC 레짐 전환 · {label.get(prev,prev)} → {label.get(new,new)}",
        description=note or None,
        color=color_map.get(new, COLORS["gray"]),
        fields=fields,
    )


async def notify_upbit_auth_fail(detail: str):
    """업비트 API 인증 실패 (IP 화이트리스트 등)."""
    await _send_embed(
        title="🔐 업비트 API 인증 실패",
        description=(
            "잔고 조회 등 API 호출이 차단되었습니다.\n"
            "**원인 후보**: IP 화이트리스트 변경, API 키 만료/삭제\n"
            "**즉시 조치**: 업비트 → 마이페이지 → Open API 관리에서 IP 확인"
        ),
        color=COLORS["red"],
        fields=[{"name": "응답", "value": f"```{detail[:500]}```", "inline": False}],
    )
