"""
[Phase 1A] 뉴스 수집 + 센티멘트 + 저장 백그라운드 루프

소스 (전부 무료, 키 불필요):
- CoinDesk RSS
- CoinTelegraph RSS
- Decrypt RSS
- The Block RSS
- CryptoPanic API (CRYPTOPANIC_API_KEY env 설정 시 추가)

특징:
- RSS는 ticker 메타데이터가 없어서 제목+본문에서 정규식으로 추출 (업비트 KRW 마켓 심볼 화이트리스트)
- 주기: 15분
- 중복 제거: external_id (source:url) UNIQUE
- 정리: 30일 이상 자동 삭제
"""
import asyncio
import os
import re
import time
import sqlite3
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime

import httpx

from app.core.database import DB_PATH
from app.services.news_sentiment import score_text

KST = timezone(timedelta(hours=9))

COLLECT_INTERVAL = 900       # 15분
CLEANUP_INTERVAL = 86400     # 24시간마다 cleanup 호출
NEWS_RETENTION_DAYS = 30

CRYPTOPANIC_API_KEY = (os.getenv("CRYPTOPANIC_API_KEY", "") or "").strip()

RSS_SOURCES = [
    ("coindesk", "https://www.coindesk.com/arc/outboundfeeds/rss/?outputType=xml"),
    ("cointelegraph", "https://cointelegraph.com/rss"),
    ("decrypt", "https://decrypt.co/feed"),
    ("theblock", "https://www.theblock.co/rss.xml"),
]

# 업비트 KRW 마켓 심볼 정규식 (lazy init)
_UPBIT_TICKER_REGEX = None


def _get_upbit_ticker_regex() -> re.Pattern:
    """업비트 KRW 마켓 심볼 정규식 (3-8자만 — 1-2자 심볼은 일반 영단어와 충돌)"""
    global _UPBIT_TICKER_REGEX
    if _UPBIT_TICKER_REGEX is not None:
        return _UPBIT_TICKER_REGEX
    try:
        import pyupbit
        tickers = pyupbit.get_tickers(fiat="KRW") or []
    except Exception:
        tickers = []
    symbols = sorted(
        {t.replace("KRW-", "") for t in tickers if t.startswith("KRW-")},
        key=len, reverse=True
    )
    symbols = [s for s in symbols if 3 <= len(s) <= 8]
    if not symbols:
        symbols = ["BTC", "ETH", "XRP", "SOL", "ADA", "DOT", "LINK", "TRX", "BCH", "LTC"]
    pattern = r"\b(" + "|".join(re.escape(s) for s in symbols) + r")\b"
    _UPBIT_TICKER_REGEX = re.compile(pattern)
    return _UPBIT_TICKER_REGEX


def _extract_tickers(text: str) -> list[str]:
    if not text:
        return []
    regex = _get_upbit_ticker_regex()
    matches = regex.findall(text.upper())
    return list(dict.fromkeys(matches))[:6]


def _parse_pubdate(s: str) -> str | None:
    if not s:
        return None
    try:
        return parsedate_to_datetime(s).astimezone(timezone.utc).isoformat()
    except Exception:
        return None


def _strip_html(s: str) -> str:
    if not s:
        return ""
    return re.sub(r"<[^>]+>", " ", s).strip()


class NewsCollector:
    def __init__(self):
        self._last_cleanup_ts = 0
        self._stats = {
            "last_run": None,
            "last_fetched": 0,
            "last_new": 0,
            "last_critical": 0,
            "total_runs": 0,
            "errors": 0,
            "sources": [],
        }
        # [Phase 1B] ticker별 sentiment summary 캐시 (1분)
        self._summary_cache: dict[str, dict] = {}
        self._summary_cache_ts = 0
        self.SUMMARY_CACHE_TTL = 60

    def get_all_ticker_summaries(self, hours: int = 24) -> dict:
        """모든 ticker의 N시간 sentiment 집계 (1회 SQL + 1분 캐시).
        반환: {symbol: {count, avg_sentiment, critical_count}}
        """
        if time.time() - self._summary_cache_ts < self.SUMMARY_CACHE_TTL:
            return self._summary_cache
        try:
            with sqlite3.connect(DB_PATH, timeout=5.0) as conn:
                rows = conn.execute(
                    """
                    SELECT tickers, sentiment, is_critical
                    FROM news
                    WHERE datetime(published_at) >= datetime('now', '-' || ? || ' hours')
                      AND tickers IS NOT NULL AND tickers != ''
                    """,
                    (hours,),
                ).fetchall()

            agg: dict[str, dict] = {}
            for tickers_str, sentiment, is_critical in rows:
                if not tickers_str:
                    continue
                s = float(sentiment) if sentiment is not None else 0.0
                crit = 1 if is_critical else 0
                for t in tickers_str.split(","):
                    t = t.strip().upper()
                    if not t:
                        continue
                    if t not in agg:
                        agg[t] = {"count": 0, "sum_sentiment": 0.0, "critical_count": 0}
                    agg[t]["count"] += 1
                    agg[t]["sum_sentiment"] += s
                    agg[t]["critical_count"] += crit

            result = {
                t: {
                    "count": d["count"],
                    "avg_sentiment": round(d["sum_sentiment"] / d["count"], 2) if d["count"] else 0.0,
                    "critical_count": d["critical_count"],
                }
                for t, d in agg.items()
            }
            self._summary_cache = result
            self._summary_cache_ts = time.time()
            return result
        except Exception as e:
            print(f"⚠️ [News summary] {e}")
            return self._summary_cache  # stale fallback

    def get_ticker_summary(self, ticker: str, hours: int = 24) -> dict:
        """단일 ticker (KRW-BTC 또는 BTC)의 sentiment summary"""
        symbol = (ticker or "").upper().replace("KRW-", "").strip()
        if not symbol:
            return {"count": 0, "avg_sentiment": None, "critical_count": 0}
        return self.get_all_ticker_summaries(hours).get(
            symbol, {"count": 0, "avg_sentiment": None, "critical_count": 0}
        )

    # ─────────────────── RSS fetchers ───────────────────
    async def fetch_rss(self, source_name: str, url: str) -> list[dict]:
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                r = await client.get(url, headers={"User-Agent": "Mozilla/5.0 CoinMate/1.0"})
                r.raise_for_status()
                # XML namespace 문제 회피: lxml 없이 ElementTree 사용
                root = ET.fromstring(r.text)
                items = root.findall(".//item")
                results = []
                for it in items:
                    title = (it.findtext("title") or "").strip()
                    link = (it.findtext("link") or "").strip()
                    if not title or not link:
                        continue
                    pubdate = _parse_pubdate(it.findtext("pubDate") or "")
                    desc = _strip_html(it.findtext("description") or "")[:500]
                    full = f"{title} {desc}"
                    tickers = _extract_tickers(full)
                    results.append({
                        "external_id": f"{source_name}:{link}",
                        "url": link,
                        "title": title[:500],
                        "description": desc,
                        "source": source_name,
                        "published_at": pubdate or datetime.now(timezone.utc).isoformat(),
                        "tickers": tickers,
                    })
                return results
        except Exception as e:
            print(f"⚠️ [News/{source_name}] {e}")
            return []

    async def fetch_cryptopanic(self) -> list[dict]:
        """CryptoPanic (CRYPTOPANIC_API_KEY env 설정 시만)"""
        if not CRYPTOPANIC_API_KEY:
            return []
        url = f"https://cryptopanic.com/api/v1/posts/?auth_token={CRYPTOPANIC_API_KEY}&public=true"
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.get(url, headers={"User-Agent": "CoinMate/1.0"})
                r.raise_for_status()
                data = r.json()
                results = []
                for p in data.get("results", []) or []:
                    currencies = p.get("currencies") or []
                    tickers = [(c.get("code") or "").upper() for c in currencies if c.get("code")]
                    votes = p.get("votes") or {}
                    neg = int(votes.get("negative", 0)) + int(votes.get("toxic", 0))
                    pos = int(votes.get("positive", 0)) + int(votes.get("lol", 0)) + int(votes.get("saved", 0))
                    vote_score = ((pos - neg) / (pos + neg) * 0.3) if (pos + neg) > 0 else 0.0
                    results.append({
                        "external_id": f"cryptopanic:{p.get('id')}",
                        "url": p.get("url", ""),
                        "title": p.get("title") or "",
                        "description": "",
                        "source": "cryptopanic",
                        "published_at": p.get("published_at"),
                        "tickers": list(dict.fromkeys(tickers))[:6],
                        "vote_score": vote_score,
                    })
                return results
        except Exception as e:
            print(f"⚠️ [News/cryptopanic] {e}")
            return []

    # ─────────────────── Persistence ───────────────────
    def score_and_save(self, articles: list[dict]) -> tuple[int, int, list[tuple[str, str]]]:
        if not articles:
            return 0, 0, []
        new_count = 0
        critical_count = 0
        critical_items: list[tuple[str, str]] = []

        with sqlite3.connect(DB_PATH, timeout=5.0) as conn:
            conn.execute("PRAGMA busy_timeout=5000")
            for a in articles:
                title = a.get("title", "")
                desc = a.get("description", "")
                sentiment, is_critical, matched = score_text(f"{title}. {desc}")
                if "vote_score" in a:
                    sentiment = round(max(-1.0, min(1.0, sentiment + a["vote_score"])), 2)
                tickers_str = ",".join(a.get("tickers", []))
                try:
                    cur = conn.execute(
                        """
                        INSERT OR IGNORE INTO news
                        (external_id, url, title, description, source, published_at,
                         tickers, sentiment, is_critical, raw_tags)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            a.get("external_id"),
                            a.get("url"),
                            title[:500],
                            desc[:500],
                            a.get("source"),
                            a.get("published_at"),
                            tickers_str,
                            sentiment,
                            1 if is_critical else 0,
                            ",".join(matched)[:300],
                        ),
                    )
                    if cur.rowcount > 0:
                        new_count += 1
                        if is_critical:
                            critical_count += 1
                            critical_items.append((tickers_str, title))
                except Exception as e:
                    print(f"⚠️ [News/insert] {e}")
            conn.commit()

        return new_count, critical_count, critical_items

    def cleanup_old(self):
        try:
            with sqlite3.connect(DB_PATH, timeout=5.0) as conn:
                cutoff = (datetime.now(KST) - timedelta(days=NEWS_RETENTION_DAYS)).isoformat()
                cur = conn.execute("DELETE FROM news WHERE created_at < ?", (cutoff,))
                conn.commit()
                if cur.rowcount > 0:
                    print(f">>> 🗞️ [News] {cur.rowcount}건 오래된 뉴스 정리 (>{NEWS_RETENTION_DAYS}일)")
        except Exception as e:
            print(f"⚠️ [News/cleanup] {e}")

    # ─────────────────── Loop ───────────────────
    async def run_once(self):
        try:
            sources_used = []
            all_articles = []

            # 모든 RSS 병렬 fetch
            rss_results = await asyncio.gather(
                *[self.fetch_rss(name, url) for name, url in RSS_SOURCES],
                return_exceptions=False,
            )
            for (name, _), arts in zip(RSS_SOURCES, rss_results):
                if arts:
                    sources_used.append(name)
                    all_articles.extend(arts)

            cp = await self.fetch_cryptopanic()
            if cp:
                sources_used.append("cryptopanic")
                all_articles.extend(cp)

            new_count, crit_count, _ = self.score_and_save(all_articles)

            self._stats["last_run"] = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
            self._stats["last_fetched"] = len(all_articles)
            self._stats["last_new"] = new_count
            self._stats["last_critical"] = crit_count
            self._stats["total_runs"] += 1
            self._stats["sources"] = sources_used

            print(
                f">>> 🗞️ [News] {self._stats['last_run']} - "
                f"수집 {len(all_articles)}건, 신규 {new_count}건, 치명적 {crit_count}건"
                f"{' (' + '+'.join(sources_used) + ')' if sources_used else ''}"
            )
        except Exception as e:
            print(f"⚠️ [News/run] {e}")
            self._stats["errors"] += 1

    async def loop(self):
        await asyncio.sleep(5)
        while True:
            await self.run_once()
            if time.time() - self._last_cleanup_ts > CLEANUP_INTERVAL:
                self.cleanup_old()
                self._last_cleanup_ts = time.time()
            await asyncio.sleep(COLLECT_INTERVAL)

    def get_stats(self) -> dict:
        return dict(self._stats)


news_collector = NewsCollector()
