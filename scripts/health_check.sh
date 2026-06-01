#!/bin/bash
# [P0+] CoinMate 헬스체크 → Discord 알림
# - /health가 2회 연속 비정상이면 1회 알림, 정상 복구 시 복구 알림
# - 디스크 사용량 85% 초과 시 1회 알림
# - systemd timer로 5분마다 실행
ENV_FILE=/home/ec2-user/CoinMate-Backend/.env
STATE_FILE=/home/ec2-user/.health_state
DISK_STATE_FILE=/home/ec2-user/.disk_alert_state
DISK_THRESHOLD=85

WEBHOOK=$(grep '^DISCORD_WEBHOOK_URL=' "$ENV_FILE" 2>/dev/null | cut -d'=' -f2- | tr -d '\r"')
[ -z "$WEBHOOK" ] && exit 0

# 최초 실행 시 상태 파일 초기화
[ -f "$STATE_FILE" ] || echo "0 0" > "$STATE_FILE"
[ -f "$DISK_STATE_FILE" ] || echo "0" > "$DISK_STATE_FILE"

# ===== 디스크 사용량 체크 =====
disk_pct=$(df / | tail -1 | awk '{print $5}' | tr -d '%')
disk_alerted=$(cat "$DISK_STATE_FILE" 2>/dev/null || echo 0)
if [ "$disk_pct" -ge "$DISK_THRESHOLD" ]; then
    if [ "$disk_alerted" = "0" ]; then
        usage=$(df -h / | tail -1 | awk '{print $3" / "$2" ("$5")"}')
        curl -s -m 10 -H "Content-Type: application/json" \
            -d "{\"content\":\"💾 [CoinMate] 디스크 사용량 임계 ${disk_pct}% 초과. 현재 ${usage}. 정리 필요\"}" "$WEBHOOK" >/dev/null
        echo "1" > "$DISK_STATE_FILE"
    fi
else
    # 임계 아래로 떨어지면 알림 상태 리셋
    [ "$disk_alerted" = "1" ] && echo "0" > "$DISK_STATE_FILE"
fi

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
