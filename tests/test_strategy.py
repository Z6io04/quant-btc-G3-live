"""Frozen G3 economic boundaries using only synthetic balances and prices."""
import copy
from decimal import Decimal as D
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from g3core.strategy import (DAY, HOUR, MINUTE, RULES, entry_signal,
    evaluate_position, funding_frame, is_decision_time, plan_entry,
    total_cost_rate, valuation, entry_decision_minute, entry_signal_due)

NOW = 30 * DAY + HOUR  # 09:00 Beijing.


def history(rate='0.0001'):
    return {t: rate for t in range(0, NOW + DAY, 8 * HOUR)}


def market():
    return {'now': NOW, 'spot': {'bid': '100000', 'ask': '100000', 'ts': NOW},
            'swap': {'bid': '100000', 'ask': '100000', 'ts': NOW}, 'mark': '100000',
            'spec': {'spot_lot': '0.00000001', 'spot_min': '0.00001', 'spot_tick': '0.1',
                     'swap_lot': '0.1', 'swap_min': '0.1', 'swap_tick': '0.1',
                     'contract_usd': '100', 'mmr': '0.004', 'tier_max_contracts': '10000'}}


def account(cash='20', btc='0', contracts='0', available_btc=None):
    return {'balances': {'USDT': {'cash': cash, 'eq': cash, 'available': cash, 'liab': '0'},
                         'BTC': {'cash': btc, 'eq': btc, 'available': btc if available_btc is None else available_btc, 'liab': '0'}},
            'position': {'contracts': contracts, 'avgPx': '100000', 'upl': '0', 'margin': '0'},
            'fees': {'spot': '0.001', 'swap': '0.0005'}}


def group(**values):
    p = {'opened_at': NOW - 10 * DAY, 'entry_equity': '100', 'contracts': '1',
         'entry_btc': '0.001', 'entry_spot_cost': '100', 'entry_swap_px': '100000',
         'funding_btc': '0', 'treasury_sold_btc': '0', 'bad_days': 0, 'last_daily_check': None}
    p.update(values)
    return p


class SignalTests(unittest.TestCase):
    def test_frozen_roundtrip_cost_and_entry(self):
        self.assertEqual(total_cost_rate(), D('0.0042'))
        signal = entry_signal(history(), NOW)
        self.assertTrue(signal['allowed'])
        self.assertEqual(signal['frame']['daily_rate'], '0.0003')
        self.assertEqual(signal['frame']['observations'], 42)

    def test_normalized_api_history_and_raw_history_match(self):
        events = history()
        normalized = [{'ts': t, 'rate': r} for t, r in events.items()]
        raw = [{'fundingTime': str(t), 'realizedRate': r} for t, r in events.items()]
        self.assertEqual(funding_frame(events, NOW, 14), funding_frame(normalized, NOW, 14))
        self.assertEqual(funding_frame(events, NOW, 14), funding_frame(raw, NOW, 14))

    def test_history_requires_preceding_coverage_and_no_internal_gaps(self):
        events = {t: r for t, r in history().items() if t > NOW - 14 * DAY}
        self.assertIsNone(funding_frame(events, NOW, 14))
        events = history()
        del events[NOW // (8 * HOUR) * 8 * HOUR - 3 * DAY]
        self.assertIsNone(funding_frame(events, NOW, 14))

    def test_future_rates_and_unpublished_latest_do_not_leak(self):
        events = history()
        expected = funding_frame(events, NOW, 14)
        events[NOW] = '-100'
        self.assertEqual(funding_frame(events, NOW, 14), expected)
        events[NOW - MINUTE] = '-1'
        self.assertEqual(entry_signal(events, NOW)['reason'], 'latest_funding_not_positive')

    def test_stale_history_and_prediction_rejected(self):
        events = {t: r for t, r in history().items() if t < NOW - 9 * HOUR}
        self.assertIsNone(funding_frame(events, NOW, 14))
        with self.assertRaises(ValueError):
            funding_frame([{'fundingTime': NOW - HOUR, 'fundingRate': '.1'}], NOW, 14)

    def test_cost_gate_uses_actual_fee_estimate(self):
        events = history('0.00002')
        self.assertTrue(entry_signal(events, NOW)['allowed'])
        self.assertFalse(entry_signal(events, NOW, '0.01', '0.01')['allowed'])

    def test_daily_time_dedup_and_no_stale_replay(self):
        self.assertTrue(is_decision_time(NOW + 10_000))
        self.assertFalse(is_decision_time(NOW + 10_000, NOW))
        self.assertFalse(is_decision_time(NOW + MINUTE))
        self.assertFalse(is_decision_time(NOW - MINUTE))


class EntryScheduleTests(unittest.TestCase):
    def test_all_twenty_four_hours_and_latest_complete_boundary(self):
        for hour in range(24):
            now=30*DAY+hour*HOUR
            self.assertEqual(entry_decision_minute(now+59000),now)
            self.assertEqual(entry_signal_due(now),now+MINUTE)

    def test_repeated_minute_and_clock_rollback_do_not_replay(self):
        self.assertIsNone(entry_decision_minute(NOW+59999,NOW))
        self.assertIsNone(entry_decision_minute(NOW-MINUTE,NOW))
        self.assertEqual(entry_decision_minute(NOW+MINUTE,NOW),NOW+MINUTE)

    def test_days_offline_skip_every_historical_slot(self):
        self.assertEqual(entry_decision_minute(NOW+3*DAY+17*MINUTE,NOW),NOW+3*DAY+17*MINUTE)

    def test_initialization_boundary_and_mid_minute(self):
        self.assertEqual(entry_decision_minute(NOW,initialized_at=NOW),NOW)
        self.assertIsNone(entry_decision_minute(NOW+59000,initialized_at=NOW+1))
        self.assertEqual(entry_decision_minute(NOW+MINUTE,initialized_at=NOW+1),NOW+MINUTE)

    def test_malformed_clocks_are_rejected(self):
        for value in (True,-1,1.1,None,'0'):
            with self.subTest(value=value), self.assertRaises(ValueError):entry_decision_minute(value)
        with self.assertRaises(ValueError):entry_decision_minute(NOW,True)
        with self.assertRaises(ValueError):entry_signal_due(NOW+1)


class SizingTests(unittest.TestCase):
    def test_twenty_dollar_nearest_with_contract_fraction(self):
        result = plan_entry({'capital_usdt': '20'}, market(), account())
        self.assertTrue(result['allowed'])
        self.assertEqual(result['contracts'], '0.2')
        self.assertEqual(result['notional_usd'], '20')
        self.assertEqual(result['allocation_method'], 'nearest')
        self.assertLessEqual(abs(D(result['estimated_delta_usdt'])), D('0.4'))
        self.assertLessEqual(D(result['estimated_spot_cost']), D(20))

    def test_exact_fallback_leaves_idle_cash(self):
        result = plan_entry({'capital_usdt': '25'}, market(), account('25'))
        self.assertTrue(result['allowed'])
        self.assertEqual(result['allocation_method'], 'exact_fallback')
        self.assertEqual(result['notional_usd'], '20')
        self.assertLess(D(result['estimated_spot_cost']), D(21))
        self.assertLessEqual(abs(D(result['estimated_delta_usdt'])), D('0.5'))

    def test_actual_ioc_limit_and_quote_fee_fit_full_budget(self):
        # Executor clips the buy limit toward the book; the strategy estimates
        # away from it, so this envelope remains conservative at a fixed quote.
        from g3core.execution import Executor, SPOT
        for cash in ('20', '60', '100'):
            cfg = {'capital_usdt': cash, 'max_slippage_bps': 3}
            m, a = market(), account(cash)
            plan = plan_entry(cfg, m, a)
            self.assertTrue(plan['allowed'])
            limit = Executor(None, None, cfg)._limit(m, SPOT, 'buy')
            worst = D(plan['gross_btc']) * limit * (1 + D(a['fees']['spot']))
            self.assertLessEqual(worst, D(cash))

    def test_real_contract_minimum_is_respected(self):
        m = market()
        m['spec'].update(swap_min='1', swap_lot='1')
        for cash in ('20', '60'):
            self.assertEqual(plan_entry({'capital_usdt': cash}, m, account(cash))['reason'], 'minimum_lot')

    def test_leverage_does_not_scale_hedge(self):
        expected = plan_entry({'capital_usdt': '20', 'leverage': '1'}, market(), account())
        actual = plan_entry({'capital_usdt': '20', 'leverage': '15'}, market(), account())
        self.assertEqual(expected, actual)

    def test_high_budget_still_respects_exchange_spot_order_maximum(self):
        m=market();m['spec']['spot_max']='0.00001'
        plan=plan_entry({'capital_usdt':100},m,account(cash='100'))
        self.assertFalse(plan['allowed'])
        self.assertEqual(plan['reason'],'spot_order_limit')

    def test_budget_available_balance_and_existing_position(self):
        a = account('100')
        a['balances']['USDT']['available'] = '25'
        plan = plan_entry({'capital_usdt': '30'}, market(), a)
        self.assertEqual(plan['budget_usdt'], '25')
        a['position']['contracts'] = '-0.2'
        self.assertEqual(plan_entry({'capital_usdt': '30'}, market(), a)['reason'], 'position_exists')

    def test_out_of_tier_and_invalid_decimal_rejected(self):
        m = market()
        m['spec']['tier_max_contracts'] = '0.1'
        self.assertEqual(plan_entry({'capital_usdt': '20'}, m, account())['reason'], 'maintenance_tier_limit')
        with self.assertRaises(ValueError):
            plan_entry({'capital_usdt': 'NaN'}, market(), account())
        with self.assertRaises(ValueError):
            plan_entry({'capital_usdt': True}, market(), account())


class PositionTests(unittest.TestCase):
    def test_valuation_is_net_of_two_leg_exit_cost(self):
        value = D(valuation(market(), account('0', '0.001', '-1')))
        self.assertLess(value, D(100))
        self.assertGreater(value, D(99))

    def test_treasury_only_earned_coins_and_never_crosses_neutral(self):
        a = account('0', '0.00103', '-1')
        p = group(funding_btc='0.00005')
        result = evaluate_position({'capital_usdt': '100'}, market(), a, p, history(), NOW)
        self.assertIsNone(result['exit_reason'])
        self.assertEqual(result['treasury_btc'], '0.00003')
        a['balances']['BTC']['available'] = '0.00002'
        result = evaluate_position({'capital_usdt': '100'}, market(), a, p, history(), NOW)
        self.assertEqual(result['treasury_btc'], '0.00002')

    def test_treasury_dust_does_not_bypass_residual_guard(self):
        m = market()
        m['spec']['spot_min'] = '0.0001'
        a = account('0', '0.00105', '-1')
        result = evaluate_position({'capital_usdt': '100'}, m, a, group(funding_btc='0.00005'), history(), NOW)
        self.assertEqual(result['exit_reason'], 'residual_exposure_guard')
        self.assertEqual(result['treasury_btc'], '0')

    def test_inverse_neutral_uses_entry_price_after_market_doubles(self):
        m = market()
        m['spot'].update(bid='200000', ask='200000')
        m['swap'].update(bid='200000', ask='200000')
        m['mark'] = '200000'
        a = account('0', '0.001015', '-1')
        a['balances']['BTC']['eq'] = '0.000515'
        a['position']['upl'] = '-0.0005'
        result = evaluate_position({'capital_usdt': '100'}, m, a, group(funding_btc='0.000015'), history(), NOW)
        self.assertIsNone(result['exit_reason'])
        self.assertEqual(result['treasury_btc'], '0.000015')
        self.assertEqual(result['metrics']['residual_delta_usdt'], '3')
        self.assertEqual(result['metrics']['collateral_ratio'], '1.03')

    def test_extra_account_cash_is_removed_before_budget_valuation(self):
        a = account('980', '0.0002', '-0.2')
        # Runtime removes idle baseline USDT outside the explicit 20U budget.
        normalized = copy.deepcopy(a)
        normalized['balances']['USDT']['cash'] = '0'
        expected = D(valuation(market(), normalized))
        self.assertLess(expected, D(20))
        self.assertEqual(D(valuation(market(), a)) - expected, D(980))

    def test_negative_carry_requires_seven_day_hold_and_three_daily_checks(self):
        p = group(bad_days=2)
        a = account('0', '0.001', '-1')
        result = evaluate_position({'capital_usdt': '100'}, market(), a, p, history('-0.001'), NOW)
        self.assertEqual(result['exit_reason'], 'funding_deterioration')
        self.assertEqual(result['bad_days'], 3)
        p['opened_at'] = NOW - 6 * DAY
        result = evaluate_position({'capital_usdt': '100'}, market(), a, p, history('-0.001'), NOW)
        self.assertIsNone(result['exit_reason'])
        self.assertEqual(result['bad_days'], 2)

    def test_same_daily_check_does_not_increment_twice(self):
        p = group(bad_days=2, last_daily_check=NOW)
        result = evaluate_position({'capital_usdt': '100'}, market(), account('0', '0.001', '-1'), p, history('-0.001'), NOW + 10_000)
        self.assertEqual(result['bad_days'], 2)
        self.assertIsNone(result['exit_reason'])

    def test_anytime_entry_does_not_accelerate_daily_funding_exit(self):
        p=group(bad_days=2)
        a=account('0','0.001','-1')
        for now in (NOW-MINUTE,NOW+MINUTE,NOW+3*HOUR,NOW+DAY-MINUTE):
            result=evaluate_position({'capital_usdt':'100'},market(),a,p,history('-0.001'),now)
            self.assertEqual(result['bad_days'],2)
            self.assertIsNone(result['exit_reason'])
            self.assertIsNone(result['last_daily_check'])

    def test_margin_and_loss_guards_use_actual_balances(self):
        a = account('0', '0.001', '-1')
        a['balances']['BTC']['eq'] = '0.0002'
        result = evaluate_position({'capital_usdt': '100'}, market(), a, group(), history(), NOW)
        self.assertEqual(result['exit_reason'], 'margin_guard')
        result = evaluate_position({'capital_usdt': '100', 'max_loss_usdt': '0.1'}, market(), account('0', '0.001', '-1'), group(), history(), NOW)
        self.assertEqual(result['exit_reason'], 'maximum_loss_guard')
        self.assertTrue(result['halt'])

    def test_peak_drawdown_halts_even_without_open_position(self):
        result = evaluate_position({'capital_usdt': '100'}, market(), account('94'), None, history(), NOW, peak_equity_usdt='100')
        self.assertTrue(result['halt'])
        self.assertEqual(result['metrics']['drawdown'], '0.06')

    def test_inputs_are_not_mutated(self):
        args = ({'capital_usdt': '100'}, market(), account('0', '0.00103', '-1'), group(funding_btc='0.00003'), history(), NOW)
        before = copy.deepcopy(args)
        evaluate_position(*args)
        self.assertEqual(before, args)


if __name__ == '__main__':
    unittest.main()
