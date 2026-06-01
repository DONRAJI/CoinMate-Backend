"""
[Phase 1A] 키워드 기반 뉴스 센티멘트 스코어링
- 영문 + 한국어 키워드 매칭
- 치명적 부정(hack/scam/delist 등)은 별도 플래그로 매수 차단 트리거
- 반환: (sentiment_score -1.0~+1.0, is_critical bool, matched_keywords list)
"""

# 치명적 부정 (자동 매수 차단/즉시 알림 트리거)
CRITICAL_NEG_KEYWORDS = {
    # 영문
    "hack", "hacked", "hacking", "exploit", "exploited", "vulnerability",
    "breach", "stolen", "theft", "drained",
    "rugpull", "rug pull", "rug-pull", "scam", "fraud", "ponzi",
    "lawsuit", "sued", "indicted", "charged",
    "delist", "delisted", "delisting", "removed from",
    "regulatory action", "sec sues", "cease and desist",
    "banned", "ban on", "shut down", "shutdown",
    "insolvency", "bankrupt", "bankruptcy",
    # 한국어
    "해킹", "해킹당", "도난", "탈취", "유출",
    "러그풀", "사기", "폰지",
    "상장폐지", "거래정지", "거래 중단",
    "규제 위반", "규제 조치", "고소", "고발", "기소",
    "파산", "지급정지",
}

# 일반 부정
NEG_KEYWORDS = {
    "crash", "plunge", "tumble", "sell-off", "selloff", "dump", "bearish",
    "decline", "drop", "fall", "tank",
    "investigation", "probe", "concern", "warning", "risk", "fears",
    "downgrade", "outflow",
    # 한국어
    "폭락", "급락", "하락", "매도", "매도세", "약세",
    "조사", "수사", "경고", "위험", "우려",
}

# 일반 긍정
POS_KEYWORDS = {
    "partnership", "partner with", "listing", "launch", "launches", "launching",
    "upgrade", "integration", "integrated",
    "adoption", "adopted", "mainnet", "rally", "surge", "surges",
    "breakout", "bullish", "approval", "approves", "milestone",
    "institutional", "endorse", "inflow", "all-time high", "ath",
    "buyback", "buy back",
    # 한국어
    "파트너십", "제휴", "상장", "출시", "런칭", "업그레이드",
    "도입", "메인넷", "급등", "강세", "돌파", "승인",
    "신고가", "최고가", "유입",
}

# 강한 긍정 (가중치 추가)
STRONG_POS_KEYWORDS = {
    "etf approval", "etf approved", "spot etf",
    "mainnet launch", "binance listing", "coinbase listing", "upbit listing",
    "메인넷 출시", "메인넷 런칭", "etf 승인", "현물 etf", "업비트 상장",
    "코인베이스 상장", "바이낸스 상장",
}


def score_text(text: str) -> tuple[float, bool, list[str]]:
    """
    제목+본문 등의 텍스트에서 키워드 매칭으로 sentiment 계산.

    Args:
        text: 분석할 문자열 (제목+설명 권장)

    Returns:
        sentiment: -1.0 ~ +1.0 (clamp)
        is_critical: 치명적 부정 키워드 매칭 여부
        matched: 매칭된 키워드 목록
    """
    if not text:
        return 0.0, False, []

    t = text.lower()
    matched: set[str] = set()
    score = 0.0
    is_critical = False

    for kw in CRITICAL_NEG_KEYWORDS:
        if kw in t:
            matched.add(kw)
            score -= 1.0
            is_critical = True

    for kw in STRONG_POS_KEYWORDS:
        if kw in t:
            matched.add(kw)
            score += 0.7

    for kw in POS_KEYWORDS:
        if kw in t:
            matched.add(kw)
            score += 0.3

    for kw in NEG_KEYWORDS:
        if kw in t:
            matched.add(kw)
            score -= 0.3

    # Clamp to [-1, +1]
    score = max(-1.0, min(1.0, score))
    return round(score, 2), is_critical, sorted(matched)
