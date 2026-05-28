#!/bin/bash
# [P0] CoinMate 헬스체크 → Discord 알림
# - /health가 2회 연속 비정상이면 1회 알림, 정상 복구 시 복구 알림
# - cron 5분마다 실행 권장
ENV_FILE=/home/ec2-user/CoinMate-Backend/.env
STATE_FILE=/home/ec2-user/.health_state

WEBHOOK=$(grep '^DISCORD_WEBHOOK_URL=' "$ENV_FILE" 2>/dev/null | cut -d'=' -f2- | tr -d '\r"')
[ -z "$WEBHOOK" ] && exit 0

# 최초 실행 시 상태 파일 초기화
[ -f "$STATE_FILE" ] || echo "0 0" > "$STATE_FILE"

resp=$(curl -s -m 10 http://localhost:8000/health)
status=$(echo "$resp" | python3.11 -c "import sys,json; print(json.load(sys.stdin).get('status','down'))" 2>/dev/null)
[ -z "$status" ] && status="down"

read failcount alerted < "$STATE_FILE" 2>/dev/null
failcount=${failcount:-0}
alerted=${alerted:-0}

if [ "$status" = "healthy" ]; then
    if [ "$alerted" = "1" ]; then
        curl -s -m 10 -H "Content-Type: application/json" \
            -d '{"content":"✅ [CoinMate] 서버 정상 복구"}' "$WEBHOOK" >/dev/null
    fi
    echo "0 0" > "$STATE_FILE"
else
    failcount=$((failcount + 1))
    if [ "$failcount" -ge 2 ] && [ "$alerted" = "0" ]; then
        curl -s -m 10 -H "Content-Type: application/json" \
            -d "{\"content\":\"🚨 [CoinMate] 서버 이상 감지 (상태: $status, ${failcount}회 연속). EC2/서비스 확인 필요\"}" "$WEBHOOK" >/dev/null
        alerted=1
    fi
    echo "$failcount $alerted" > "$STATE_FILE"
fi
