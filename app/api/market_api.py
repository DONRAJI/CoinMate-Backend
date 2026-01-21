from fastapi import APIRouter
from app.services.trade_manager import trade_manager
# Backtester, Strategy 임포트는 필요 없습니다 (TradeManager꺼 쓸 거니까)

router = APIRouter()

@router.get("/prices")
def get_prices():
    if not trade_manager.frontend_cache:
        return {"status": "success", "data": []}
    return {"status": "success", "data": trade_manager.frontend_cache}

@router.get("/analysis/{ticker}")
async def analyze_coin(ticker: str):
    """
    [수정됨] TradeManager가 데리고 있는 backtester와 strategy를 사용
    """
    try:
        # 🔥 [핵심 수정] trade_manager 안에 있는 backtester를 사용해야 데이터가 있습니다!
        cached_data = trade_manager.backtester.get_analysis(ticker)
        
        # 데이터 없으면 분석 요청
        if not cached_data:
            await trade_manager.backtester._analyze_one(ticker)
            cached_data = trade_manager.backtester.get_analysis(ticker)
            if not cached_data:
                 return {"status": "error", "message": "데이터 분석 중..."}

        response_data = cached_data.copy()

        # 실시간 데이터 주입
        if ticker in trade_manager.cached_day_dfs and ticker in trade_manager.cached_min_dfs:
            df_day = trade_manager.cached_day_dfs[ticker].copy()
            df_min = trade_manager.cached_min_dfs[ticker].copy()
            
            if trade_manager.shared_data and ticker in trade_manager.shared_data:
                current_price = trade_manager.shared_data[ticker]['current_price']
                df_day.iloc[-1, df_day.columns.get_loc('close')] = current_price
                df_min.iloc[-1, df_min.columns.get_loc('close')] = current_price
            
            # 🔥 [핵심 수정] trade_manager의 strategy 사용
            print(f">>> 🔍 [User Request] {ticker} 상세 분석 요청")
            realtime_result = trade_manager.strategy.get_ensemble_signal(df_day, df_min, debug=True)
            
            if realtime_result:
                response_data.update(realtime_result)

        # 백업 가격 정보
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