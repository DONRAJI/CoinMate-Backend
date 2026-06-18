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


async def notify_shadow_buy(ticker: str, price: float, budget: float, strategy: str):
    """[섀도우] 가상 매수 알림 — 실거래와 구분(보라색 🧪)."""
    await _send_embed(
        title=f"🧪 [가상] 매수 · {ticker}",
        description="섀도우 모드 가상거래 (실제 주문 아님)",
        color=COLORS["purple"],
        fields=[
            {"name": "가상 체결가", "value": f"{price:,.0f}원"},
            {"name": "가상 투입", "value": f"{budget:,.0f}원"},
            {"name": "전략", "value": strategy, "inline": False},
        ],
        footer="SHADOW MODE · 위험 없는 가상거래 검증",
    )


async def notify_shadow_sell(ticker: str, price: float, profit: float, reason: str):
    """[섀도우] 가상 매도 알림."""
    sign = "+" if profit >= 0 else ""
    await _send_embed(
        title=f"🧪 [가상] 매도 · {ticker} ({sign}{profit:.2f}%)",
        description="섀도우 모드 가상거래 (실제 주문 아님)",
        color=COLORS["purple"],
        fields=[
            {"name": "가상 체결가", "value": f"{price:,.0f}원"},
            {"name": "수익률", "value": f"{sign}{profit:.2f}%"},
            {"name": "사유", "value": reason, "inline": False},
        ],
        footer="SHADOW MODE · 위험 없는 가상거래 검증",
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

    # [세션31] 섀도우(가상) 거래 검증 중 — 일일 요약에 가상거래도 표시
    shadow = stats.get("shadow")
    if shadow:
        s_pnl = shadow.get("pnl", 0)
        s_sign = "+" if s_pnl >= 0 else ""
        fields.append({"name": "🧪 가상 거래", "value": f"{shadow.get('trades', 0)}건", "inline": False})
        fields.append({"name": "🧪 가상 승률", "value": f"{shadow.get('win_rate', 0):.0f}% ({shadow.get('wins', 0)}/{shadow.get('trades', 0)})"})
        fields.append({"name": "🧪 가상 손익", "value": f"{s_sign}{s_pnl:,.0f}원"})
        fields.append({"name": "🧪 가상 자산", "value": f"{shadow.get('total_assets', 0):,.0f}원 ({shadow.get('return_pct', 0):+.2f}%)"})
        fields.append({"name": "🧪 가상 보유", "value": f"{shadow.get('open', 0)}건"})

    await _send_embed(
        title=f"{pnl_emoji} 일일 요약 · {datetime.now(KST).strftime('%m/%d')}",
        color=pnl_color,
        fields=fields,
        footer="매일 자정 직전 자동 전송",
    )


async def notify_ml_eval_daily(entry: dict):
    """[분봉 모델] 일일 예측 평가 결과 (익절확률 calibration)."""
    pred = entry.get('predicted_avg_winrate', 0) * 100
    actual = entry.get('actual_winrate', 0) * 100
    calib = entry.get('calibration_error_pp', 0)
    above_wr = entry.get('above_threshold_winrate')
    above_n = entry.get('above_threshold_n', 0)
    top10 = entry.get('top10_winrate', 0) * 100

    # calibration 색상
    if abs(calib) <= 8:
        color = COLORS["green"]; cal_txt = f"{calib:+.1f}%p ✅ 양호"
    elif calib > 0:
        color = COLORS["yellow"]; cal_txt = f"{calib:+.1f}%p ⚠️ 과대평가"
    else:
        color = COLORS["yellow"]; cal_txt = f"{calib:+.1f}%p ⚠️ 과소평가"

    fields = [
        {"name": "평가 종목", "value": f"{entry.get('n_evaluated', 0)}개"},
        {"name": "예측 평균 익절확률", "value": f"{pred:.1f}%"},
        {"name": "실제 익절률", "value": f"{actual:.1f}%"},
        {"name": "Calibration 오차", "value": cal_txt, "inline": False},
        {"name": f"매수기준(42%)↑ {above_n}개", "value": f"실제익절 {(above_wr or 0)*100:.0f}%" if above_wr is not None else "해당없음"},
        {"name": "Top10 실제익절", "value": f"{top10:.0f}%"},
    ]

    bo = entry.get('buy_opportunities')
    if bo:
        fields.append({
            "name": "매수후보 수 (게이트 통과)",
            "value": f"현행bull {bo.get('current_bull', 0)} / neutral {bo.get('current_neutral', 0)} / 완화 {bo.get('breakeven_relaxed', 0)}",
            "inline": False,
        })
    await _send_embed(
        title=f"📊 분봉 모델 일일 평가 · {entry.get('date', '')}",
        description='"예측한 익절확률대로 실제 익절/손절했는지" 검증',
        color=color,
        fields=fields,
        footer="익절+3.5%가 손절-2%보다 먼저 닿으면 익절(win)",
    )


async def notify_ml_eval_weekly(review: dict):
    """[분봉 모델] 주간 모델 점검 — 누적 진단 + 수정 방향 제안."""
    pred = review.get('avg_predicted_winrate', 0) * 100
    actual = review.get('avg_actual_winrate', 0) * 100
    calib = review.get('avg_calibration_error_pp', 0)
    above = review.get('avg_above_threshold_winrate')
    recs = review.get('recommendations', [])

    # 권장에 🔴 있으면 빨강, 🟡만 있으면 노랑, 아니면 파랑
    joined = " ".join(recs)
    if "🔴" in joined:
        color = COLORS["red"]
    elif "⚠️" in joined or "🟡" in joined:
        color = COLORS["yellow"]
    else:
        color = COLORS["blue"]

    fields = [
        {"name": "집계 기간", "value": f"최근 {review.get('days_covered', 0)}일"},
        {"name": "평균 예측", "value": f"{pred:.1f}%"},
        {"name": "평균 실제", "value": f"{actual:.1f}%"},
        {"name": "평균 calibration", "value": f"{calib:+.1f}%p", "inline": False},
    ]
    if above is not None:
        fields.append({"name": "매수기준↑ 평균익절률", "value": f"{above*100:.0f}% (손익분기 36%)", "inline": False})
    if recs:
        fields.append({"name": "📋 진단 / 권장 방향", "value": "\n".join(recs), "inline": False})

    await _send_embed(
        title=f"📅 주간 모델 점검 · {review.get('date', '')}",
        description="누적 평가 기반 모델 수정 방향 진단 (주 1회)",
        color=color,
        fields=fields,
        footer="Colab 재학습 시 이 진단을 참고하세요",
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
