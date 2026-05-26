"""Strategy 엔진 테스트"""
import pytest
import pandas as pd
import numpy as np
from app.services.strategy import Strategy


def make_ohlcv(n=60, base_price=1000, trend='flat'):
    """테스트용 OHLCV 데이터 생성"""
    dates = pd.date_range('2025-01-01', periods=n, freq='h')
    prices = np.full(n, base_price, dtype=float)

    if trend == 'up':
        prices = base_price + np.linspace(0, base_price * 0.3, n)
    elif trend == 'down':
        prices = base_price - np.linspace(0, base_price * 0.2, n)

    noise = np.random.normal(0, base_price * 0.005, n)
    close = prices + noise
    open_ = close - np.random.uniform(-5, 5, n)
    high = np.maximum(close, open_) + np.random.uniform(0, 10, n)
    low = np.minimum(close, open_) - np.random.uniform(0, 10, n)
    volume = np.random.uniform(100, 1000, n)

    return pd.DataFrame({
        'open': open_, 'high': high, 'low': low, 'close': close, 'volume': volume
    }, index=dates)


class TestStrategy:
    def setup_method(self):
        self.strategy = Strategy()

    def test_weights_sum(self):
        """가중치 합이 13.5점"""
        total = sum(self.strategy.WEIGHTS.values())
        assert total == pytest.approx(13.5, abs=0.1)

    def test_buy_threshold(self):
        """매수 기준점 확인"""
        assert self.strategy.BUY_THRESHOLD == 6.0

    def test_returns_none_for_insufficient_data(self):
        """데이터 부족 시 None 반환"""
        short_df = make_ohlcv(n=10)
        result = self.strategy.get_ensemble_signal(short_df)
        assert result is None

    def test_returns_dict_for_valid_data(self):
        """충분한 데이터 시 dict 반환"""
        df = make_ohlcv(n=60)
        result = self.strategy.get_ensemble_signal(df, df)
        assert result is not None
        assert isinstance(result, dict)

    def test_result_has_required_keys(self):
        """결과에 필수 키가 모두 존재"""
        df = make_ohlcv(n=60)
        result = self.strategy.get_ensemble_signal(df, df)
        assert result is not None
        required = ['score', 'rsi', 'strategies', 'target_price', 'stop_loss_price']
        for key in required:
            assert key in result, f"Missing key: {key}"

    def test_score_in_valid_range(self):
        """점수가 유효 범위 내"""
        df = make_ohlcv(n=60)
        result = self.strategy.get_ensemble_signal(df, df)
        assert result is not None
        assert -15 <= result['score'] <= 15  # 음수 가능 (oscillator -1)

    def test_rsi_in_valid_range(self):
        """RSI가 0~100 범위"""
        df = make_ohlcv(n=60)
        result = self.strategy.get_ensemble_signal(df, df)
        assert result is not None
        assert 0 <= result['rsi'] <= 100
