from fastapi import APIRouter
from app.services.trade_manager import trade_manager
from app.services.backtester import Backtester
from app.services.strategy import Strategy
import asyncio

router = APIRouter()
backtester = Backtester()
strategy = Strategy()

@router.get("/prices")
def get_prices():
    if not trade_manager.frontend_cache:
        return {"status": "success", "data": []}
    return {"status": "success", "data": trade_manager.frontend_cache}

@router.get("/analysis/{ticker}")
async def analyze_coin(ticker: str):
    """
    [Debug Mode Enabled]
    상세보기 요청 시 -> 콘솔에 점수 계산 과정 출력
    """
    try:
        cached_data = backtester.get_analysis(ticker)
        
        if not cached_data:
            await backtester._analyze_one(ticker)
            cached_data = backtester.get_analysis(ticker)
            if not cached_data:
                 return {"status": "error", "message": "데이터 분석 중..."}

        response_data = cached_data.copy()

        # 실시간 데이터 및 캐시된 캔들 가져오기
        if ticker in trade_manager.cached_day_dfs and ticker in trade_manager.cached_min_dfs:
            
            # 1. 캔들 복사 및 실시간 가격 주입
            df_day = trade_manager.cached_day_dfs[ticker].copy()
            df_min = trade_manager.cached_min_dfs[ticker].copy()
            
            if ticker in trade_manager.shared_data:
                current_price = trade_manager.shared_data[ticker]['current_price']
                df_day.iloc[-1, df_day.columns.get_loc('close')] = current_price
                df_min.iloc[-1, df_min.columns.get_loc('close')] = current_price
            
            # 2. [Debug] 전략 재실행하며 로그 출력
            # 사용자가 모달을 켰다는 건 궁금하다는 뜻이므로 여기서 로그를 찍어줌
            print(f">>> 🔍 [User Request] {ticker} 상세 분석 요청")
            
            # 여기서 debug=True를 넣으면 콘솔에 쫙 뜹니다!
            realtime_result = strategy.get_ensemble_signal(df_day, df_min, debug=True)
            
            if realtime_result:
                response_data['current_price'] = realtime_result['current_price']
                response_data['score'] = realtime_result['score']
                response_data['strategies'] = realtime_result['strategies']
                response_data['rsi'] = realtime_result['rsi']
                response_data['mfi'] = realtime_result['mfi']
                response_data['should_buy'] = realtime_result['should_buy']
                response_data["score_breakdown"] = realtime_result.get("score_breakdown", [])

        # 백업: TradeManager 감시 대상은 아니지만 실시간 가격은 있는 경우
        elif trade_manager.shared_data and ticker in trade_manager.shared_data:
            response_data['current_price'] = trade_manager.shared_data[ticker]['current_price']

        return {
            "status": "success",
            "data": response_data
        }

    except Exception as e:
        print(f"API Error: {e}")
        return {"status": "error", "message": str(e)}

@router.get("/status/{ticker}")
def get_coin_status(ticker: str):
    return {"status": "success", "data": {}}