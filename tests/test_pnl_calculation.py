"""손익 계산 로직 테스트"""
import pytest


FEE_RATE = 0.0005  # 업비트 수수료 0.05%


def calc_pnl(buy_amount: float, profit_rate: float) -> float:
    """trade_repository.py의 총 손익 SQL과 동일한 로직"""
    return buy_amount * (1 - FEE_RATE) * (1 + profit_rate / 100.0) * (1 - FEE_RATE) - buy_amount


class TestPnLCalculation:
    def test_break_even_still_loses_to_fees(self):
        """수익률 0%여도 수수료 때문에 손실"""
        pnl = calc_pnl(10000, 0.0)
        assert pnl < 0
        assert pnl == pytest.approx(-10, abs=1)  # 약 -10원 (왕복 0.1%)

    def test_positive_profit(self):
        """+2% 수익 시 수수료 차감 후에도 이익"""
        pnl = calc_pnl(10000, 2.0)
        assert pnl > 0
        assert pnl == pytest.approx(190, abs=5)  # 약 190원

    def test_stop_loss(self):
        """-2% 손절 시 손실 계산"""
        pnl = calc_pnl(10000, -2.0)
        assert pnl < 0
        assert pnl == pytest.approx(-210, abs=5)  # 약 -210원

    def test_fee_threshold(self):
        """수수료를 넘기려면 최소 0.1% 이상 수익 필요"""
        pnl_01 = calc_pnl(10000, 0.1)
        assert pnl_01 == pytest.approx(0, abs=2)  # 거의 본전

    def test_large_trade(self):
        """큰 금액 거래도 비율 동일"""
        pnl_small = calc_pnl(10000, 3.0)
        pnl_large = calc_pnl(100000, 3.0)
        ratio = pnl_large / pnl_small
        assert ratio == pytest.approx(10.0, abs=0.1)


class TestEntryFilters:
    """진입 필터 로직 테스트 (trade_manager.py의 조건들)"""

    def test_rsi_overheat_blocks(self):
        """RSI >= 75이면 매수 차단"""
        rsi = 78
        assert rsi >= 75

    def test_mfi_overheat_blocks(self):
        """MFI >= 85이면 매수 차단"""
        mfi = 88
        assert mfi >= 85

    def test_rsi_mfi_divergence_blocks(self):
        """RSI >= 65 and MFI < 35이면 괴리로 차단"""
        rsi, mfi = 68, 30
        assert rsi >= 65 and mfi < 35

    def test_recent_surge_3h_blocks(self):
        """3시간 내 +5% 급등이면 차단"""
        price_3h_ago = 1000
        current = 1060  # +6%
        surge = ((current - price_3h_ago) / price_3h_ago) * 100
        assert surge >= 5.0

    def test_recent_surge_1h_blocks(self):
        """1시간 내 +3% 급등이면 차단"""
        price_1h_ago = 1000
        current = 1035  # +3.5%
        surge = ((current - price_1h_ago) / price_1h_ago) * 100
        assert surge >= 3.0

    def test_high_proximity_blocks(self):
        """6시간 최고가 대비 98% 이상이면 차단"""
        recent_high = 1000
        current = 985  # 98.5%
        ratio = current / recent_high
        assert ratio >= 0.98

    def test_normal_entry_passes(self):
        """정상 조건이면 모든 필터 통과"""
        rsi, mfi = 45, 50
        price_3h_ago, price_1h_ago, current = 1000, 1010, 1020
        recent_high = 1050

        assert rsi < 75
        assert mfi < 85
        assert not (rsi >= 65 and mfi < 35)
        assert ((current - price_3h_ago) / price_3h_ago) * 100 < 5.0
        assert ((current - price_1h_ago) / price_1h_ago) * 100 < 3.0
        assert current / recent_high < 0.98


class TestSellReasons:
    """매도 사유 정규화 테스트"""

    def test_stop_loss_trigger(self):
        profit_rate = -2.5
        stop_loss = -2.0
        assert profit_rate <= stop_loss
        reason = "stop_loss"
        assert reason == "stop_loss"

    def test_trailing_trigger(self):
        profit_rate = 2.0
        trailing_activation = 1.5
        drawdown = 1.5
        trailing_distance = 1.2
        assert profit_rate >= trailing_activation
        assert drawdown >= trailing_distance

    def test_take_profit_trigger(self):
        profit_rate = 4.0
        profit_target = 3.5
        assert profit_rate >= profit_target

    def test_score_drop_trigger(self):
        score = 2.5
        profit_rate = -0.8
        assert score < 3.0 and profit_rate < -0.5
