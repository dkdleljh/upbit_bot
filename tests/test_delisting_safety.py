import asyncio
import json
import time
from datetime import datetime
from unittest.mock import AsyncMock

import pytest

from src.execution import ExecutionEngine
from src.market_policy import MarketPolicy
from src.state_machine import TradingStateMachine
from src.storage import Storage
from src.upbit_rest import UpbitRestClient, UpbitAPIError
from src.upbit_ws import MarketCache
from src.utils import load_config

CUTOFF = datetime.fromisoformat('2026-11-16T15:00:00+09:00').timestamp()
PAIRS = ['KRW-SOPH', 'BTC-SOPH', 'USDT-SOPH', 'KRW-ZIL', 'BTC-ZIL']


def run(coro):
    return asyncio.run(coro)


def catalog(markets):
    return [{'market': m, 'market_event': {'warning': False}} for m in markets]


def ready(rest, market='KRW-ABC', clock=None):
    rest.policy.clock = clock or time.time
    rest.policy.update_catalog(catalog([market]))
    for kind in ('ticker', 'orderbook'):
        rest.policy.record_data(market, kind, {'timestamp': rest.policy.clock() * 1000})


def fill(uuid, qty, price=100, state='done', remaining=0):
    return {'uuid': uuid, 'state': state, 'executed_volume': str(qty),
            'remaining_volume': str(remaining), 'paid_fee': str(qty * price * .0005),
            'trades': [{'price': str(price), 'volume': str(qty), 'funds': str(qty * price)}] if qty else []}


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setenv('UPBIT_LIVE_CONFIRM', 'YES')
    monkeypatch.setenv('UPBIT_KILL_SWITCH', '0')
    monkeypatch.setattr(asyncio, 'sleep', AsyncMock())
    cfg = load_config('config/config.yaml')
    storage = Storage(':memory:')
    rest = UpbitRestClient('fake-access', 'fake-secret')
    engine = ExecutionEngine(cfg, storage, 'live', rest)
    ready(rest)
    return cfg, storage, rest, engine


@pytest.mark.parametrize('market', PAIRS)
@pytest.mark.parametrize('delta', [-.001, 0, .001])
def test_cutoff_and_buy_block_for_all_pairs(market, delta):
    p = MarketPolicy(clock=lambda: CUTOFF + delta)
    p.update_catalog(catalog([market]))
    assert p.reason(market, 'BUY') in {'blocked_buy_asset', 'trading_ended'}
    assert p.reason(market, 'SELL') == (None if delta < 0 else 'trading_ended')


def test_warning_missing_catalog_and_stale_quote_fail_closed():
    clock = [1000.]
    p = MarketPolicy(clock=lambda: clock[0])
    assert p.reason('KRW-ABC', 'BUY') == 'market_catalog_unavailable'
    p.update_catalog([{'market': 'KRW-ABC', 'market_event': {'warning': True}}])
    assert p.reason('KRW-ABC', 'BUY') == 'market_warning_or_unknown'
    assert p.reason('KRW-ABC', 'SELL') is None
    p.update_catalog([{'market': 'KRW-ABC'}])
    assert p.reason('KRW-ABC', 'BUY') == 'market_warning_or_unknown'
    p.update_catalog(catalog(['KRW-ABC']))
    for k in ('ticker', 'orderbook'):
        p.record_data('KRW-ABC', k, {'timestamp': 1000000})
    assert p.reason('KRW-ABC', 'BUY', True) is None
    clock[0] += 91
    assert p.reason('KRW-ABC', 'BUY', True) == 'market_data_stale'
    clock[0] += 30
    assert p.reason('KRW-ABC', 'BUY') == 'market_catalog_unavailable'


@pytest.mark.parametrize('method,args', [('get_markets', ()), ('get_tickers', (['KRW-ABC'],)),
                                        ('get_orderbook', (['KRW-ABC'],)),
                                        ('get_candles_minutes', ('KRW-ABC',)), ('get_accounts', ())])
def test_error_dict_is_never_a_data_list(setup, monkeypatch, method, args):
    _, _, rest, _ = setup
    monkeypatch.setattr(rest, '_request', AsyncMock(return_value={'error': {'name': 'market_not_found'}}))
    with pytest.raises(ValueError):
        run(getattr(rest, method)(*args))


def test_catalog_requests_details_and_invalidates_failure(setup, monkeypatch):
    _, _, rest, _ = setup
    get = AsyncMock(return_value=catalog(['KRW-ABC']))
    monkeypatch.setattr(rest, '_get', get)
    assert run(rest.get_markets()) == ['KRW-ABC']
    assert get.call_args.args[1] == {'is_details': 'true'}
    get.side_effect = TimeoutError()
    with pytest.raises(TimeoutError):
        run(rest.get_markets())
    assert not rest.policy.catalog_ok


def test_post_response_loss_is_single_attempt_and_durable(setup, monkeypatch):
    _, storage, rest, _ = setup
    transport = AsyncMock(side_effect=TimeoutError())
    monkeypatch.setattr(rest, '_request_aiohttp', transport)
    run(rest.place_market_buy('KRW-ABC', 10000))
    assert transport.await_count == 1
    body = transport.call_args.args[3]
    assert body['identifier'].startswith('ub-')
    row = storage.pending_orders('KRW-ABC')[0]
    assert row['status'] == 'unknown'
    assert json.loads(row['request_json'])['identifier'] == body['identifier']
    assert run(rest.place_market_buy('KRW-ABC', 10000))['error']['name'] == 'unresolved_order'
    assert transport.await_count == 1
    ready(rest, 'BTC-ABC')
    assert rest.order_block_reason('BTC-ABC', 'BUY') == 'unresolved_order'


@pytest.mark.parametrize('status', [400, 401, 403, 404, 418, 429, 500])
def test_order_errors_not_retried(setup, monkeypatch, status):
    _, storage, rest, _ = setup
    transport = AsyncMock(side_effect=UpbitAPIError(status, {'error': {'name': 'test_rejection'}}))
    monkeypatch.setattr(rest, '_request_aiohttp', transport)
    result = run(rest.place_market_buy('KRW-ABC', 10000))
    assert result['http_status'] == status
    assert transport.await_count == 1
    assert bool(storage.pending_orders()) == (status in {418, 429, 500})


def test_get_429_retries_and_honors_retry_after(setup, monkeypatch):
    _, _, rest, _ = setup
    transport = AsyncMock(side_effect=[UpbitAPIError(429, {}, 40), []])
    monkeypatch.setattr(rest, '_request_aiohttp', transport)
    assert run(rest._get('/v1/ticker')) == []
    assert transport.await_count == 2
    assert rest._backoff_until_monotonic - time.monotonic() > 39


@pytest.mark.parametrize('status', [404, 418])
def test_permanent_get_errors_stop(setup, monkeypatch, status):
    _, _, rest, _ = setup
    transport = AsyncMock(side_effect=UpbitAPIError(status))
    monkeypatch.setattr(rest, '_request_aiohttp', transport)
    with pytest.raises(UpbitAPIError):
        run(rest._get('/v1/market/all'))
    assert transport.await_count == 1


def test_cutoff_rechecked_after_rate_wait(setup, monkeypatch):
    _, storage, rest, _ = setup
    clock = [CUTOFF - 1]
    ready(rest, 'KRW-SOPH', lambda: clock[0])
    async def pass_cutoff():
        clock[0] = CUTOFF
    monkeypatch.setattr(rest, '_respect_backoff', pass_cutoff)
    transport = AsyncMock()
    monkeypatch.setattr(rest, '_request_aiohttp', transport)
    result = run(rest.place_market_sell('KRW-SOPH', 100))
    assert result['error']['name'] == 'trading_ended'
    transport.assert_not_called()
    assert not storage.pending_orders()


def test_cancel_rejection_never_sends_market_order(setup, monkeypatch):
    _, storage, rest, engine = setup
    calls = []
    async def transport(method, url, params, body, auth):
        calls.append((method, url, params, body))
        if url.endswith('/accounts'):
            return [{'currency': 'ABC', 'balance': '100', 'locked': '0'}]
        if method == 'POST':
            return {'uuid': 'limit-1', 'state': 'wait'}
        if method == 'DELETE':
            raise UpbitAPIError(400, {'error': {'name': 'cancel_rejected'}})
        return fill('limit-1', 0, state='wait', remaining=100)
    monkeypatch.setattr(rest, '_request_aiohttp', transport)
    result = run(engine.execute_market('KRW-ABC', 'SELL', 10000, 100, 100, 0, 'time_exit'))
    assert not result.ok
    assert len([c for c in calls if c[0] == 'POST']) == 1
    assert len([c for c in calls if c[0] == 'DELETE']) == 1
    assert storage.pending_orders('KRW-ABC')


def test_partial_cancel_aggregates_original_and_remaining_once(setup, monkeypatch):
    _, storage, rest, engine = setup
    calls = []
    cancelled = [False]
    async def transport(method, url, params, body, auth):
        calls.append((method, params, body))
        if url.endswith('/accounts'):
            return [{'currency': 'ABC', 'balance': '100', 'locked': '0'}]
        if method == 'POST':
            return {'uuid': 'limit-1' if body['ord_type'] == 'limit' else 'market-2', 'state': 'wait'}
        if method == 'DELETE':
            assert params == {'uuid': 'limit-1'} and body is None
            cancelled[0] = True
            return {'uuid': 'limit-1', 'state': 'cancel'}
        if params['uuid'] == 'limit-1':
            return fill('limit-1', 40, state='cancel' if cancelled[0] else 'wait', remaining=60)
        return fill('market-2', 60, 99)
    monkeypatch.setattr(rest, '_request_aiohttp', transport)
    result = run(engine.execute_market('KRW-ABC', 'SELL', 10000, 100, 100, 0, 'time_exit'))
    assert result.ok and result.qty == 100
    assert result.fill_price == pytest.approx(99.4)
    posts = [c[2] for c in calls if c[0] == 'POST']
    assert len(posts) == 2 and float(posts[1]['volume']) == 60
    assert posts[0]['identifier'] != posts[1]['identifier']
    assert not storage.pending_orders()
    assert sum(r['qty'] for r in storage.query('SELECT qty FROM trades')) == 100


def test_waiting_market_order_does_not_submit_remainder(setup, monkeypatch):
    _, storage, rest, engine = setup
    posts = []
    async def transport(method, url, params, body, auth):
        if url.endswith('/accounts'):
            return [{'currency': 'KRW', 'balance': '100000', 'locked': '0'}]
        if method == 'POST':
            posts.append(body)
            return {'uuid': 'buy-1', 'state': 'wait'}
        return fill('buy-1', 0, state='wait', remaining=100)
    monkeypatch.setattr(rest, '_request_aiohttp', transport)
    result = run(engine.execute_market('KRW-ABC', 'BUY', 10000, 100, 100, 0, 'entry'))
    assert not result.ok and result.reason == 'order_unresolved'
    assert len(posts) == 1 and storage.pending_orders()


def test_external_cancel_does_not_recreate_order(setup, monkeypatch):
    _, _, rest, engine = setup
    posts = []
    async def transport(method, url, params, body, auth):
        if url.endswith('/accounts'):
            return [{'currency': 'ABC', 'balance': '100', 'locked': '0'}]
        if method == 'POST':
            posts.append(body)
            return {'uuid': 'limit-1', 'state': 'wait'}
        assert method == 'GET'
        return fill('limit-1', 40, state='cancel', remaining=60)
    monkeypatch.setattr(rest, '_request_aiohttp', transport)
    result = run(engine.execute_market('KRW-ABC', 'SELL', 10000, 100, 100, 0, 'time_exit'))
    assert result.ok and result.qty == 40 and len(posts) == 1


def test_soph_dust_topup_cannot_buy(setup, monkeypatch):
    _, _, rest, engine = setup
    ready(rest, 'KRW-SOPH', lambda: CUTOFF - 1)
    async def transport(method, url, params, body, auth):
        assert method == 'GET' and url.endswith('/accounts')
        return [{'currency': 'SOPH', 'balance': '10', 'locked': '0'}]
    spy = AsyncMock(side_effect=transport)
    monkeypatch.setattr(rest, '_request_aiohttp', spy)
    result = run(engine.execute_market('KRW-SOPH', 'SELL', 1000, 10, 100, 0, 'time_exit'))
    assert not result.ok
    assert all(c.args[0] == 'GET' for c in spy.call_args_list)


@pytest.mark.parametrize('terminal_qty', [0, 5])
def test_restart_reconciles_by_identifier_without_resubmitting(setup, monkeypatch, tmp_path, terminal_qty):
    cfg, _, _, _ = setup
    path = str(tmp_path / 'orders.db')
    st = Storage(path)
    st.prepare_order({'identifier': 'intent-1', 'market': 'KRW-ABC', 'side': 'bid', 'price': '10000'})
    st.close()
    recovered = Storage(path)
    client = UpbitRestClient('fake', 'fake')
    client.configure_safety(cfg, recovered)
    ready(client)
    async def transport(method, url, params, body, auth):
        assert method == 'GET' and params == {'identifier': 'intent-1'}
        return fill('found-uuid', terminal_qty, state='cancel')
    monkeypatch.setattr(client, '_request_aiohttp', transport)
    run(client.reconcile_pending_orders())
    assert bool(recovered.pending_orders()) == (terminal_qty > 0)
    if terminal_qty:
        assert client.order_block_reason('KRW-ABC', 'BUY') == 'unresolved_order'
    assert recovered.query('SELECT * FROM trades') == []


def test_not_found_is_still_unknown(setup, monkeypatch):
    _, st, rest, _ = setup
    st.prepare_order({'identifier': 'unknown', 'market': 'KRW-ABC', 'side': 'bid'})
    transport = AsyncMock(side_effect=UpbitAPIError(404))
    monkeypatch.setattr(rest, '_request_aiohttp', transport)
    run(rest.reconcile_pending_orders())
    assert len(st.pending_orders()) == 1
    assert transport.await_count == 1


def test_quote_currency_market_buy_payload(setup, monkeypatch):
    _, _, rest, engine = setup
    ready(rest, 'BTC-ABC')
    posts = []
    async def transport(method, url, params, body, auth):
        if url.endswith('/accounts'):
            return [{'currency': 'BTC', 'balance': '1', 'locked': '0'}]
        if method == 'POST':
            posts.append(body)
            return {'uuid': 'btc-buy', 'state': 'wait'}
        return fill('btc-buy', 1, .0001)
    monkeypatch.setattr(rest, '_request_aiohttp', transport)
    result = run(engine.execute_market('BTC-ABC', 'BUY', 10000, 1, .0001, 0, 'entry'))
    assert result.ok
    assert posts[0]['ord_type'] == 'price'
    assert float(posts[0]['price']) == .0001 and 'volume' not in posts[0]


def test_catalog_refresh_prunes_quotes_but_not_positions(setup, monkeypatch):
    cfg, st, rest, _ = setup
    cache = MarketCache()
    sm = TradingStateMachine(cfg, rest, cache, st, None, None, 'live')
    ready(rest)
    sm.portfolio.add('KRW-ABC', 100, 100, 90, 50)
    sm.universe_top10 = ['KRW-ABC']
    run(cache.seed_tickers([{'market': 'KRW-ABC', 'timestamp': time.time() * 1000, 'trade_price': 100}]))
    monkeypatch.setattr(rest, '_get', AsyncMock(return_value=catalog(['KRW-BTC'])))
    run(sm._refresh_market_catalog(force=True))
    assert 'KRW-ABC' not in cache.tickers and sm.universe_top10 == []
    assert sm.portfolio.positions['KRW-ABC'].qty == 100


def test_stale_snapshot_excludes_old_quotes():
    cache = MarketCache()
    run(cache.seed_tickers([{'market': 'KRW-ABC', 'timestamp': (time.time() - 100) * 1000}]))
    assert run(cache.fresh_snapshot())[0] == {}


def test_error_counter_recovers_only_after_stability(setup, monkeypatch):
    _, _, rest, _ = setup
    monkeypatch.setattr('src.upbit_rest.time.time', lambda: 1000)
    ready(rest, clock=lambda: 1000)
    rest.error_count = 5
    rest.last_error_at = 999
    rest.recover_error_count(900)
    assert rest.error_count == 5
    rest.last_error_at = 99
    rest.recover_error_count(900)
    assert rest.error_count == 0


def test_legacy_maker_helper_cannot_bypass_kill_switch(setup, monkeypatch):
    _, _, rest, engine = setup
    engine.kill_switch = True
    transport = AsyncMock()
    monkeypatch.setattr(rest, '_request_aiohttp', transport)
    result = run(engine.execute_with_maker_first('KRW-ABC', 'BUY', 10000, 100, 100, 0, 'entry', {}))
    assert not result.ok and result.reason == 'kill_switch_on'
    transport.assert_not_called()


def test_live_without_keys_never_reports_paper_fill(setup):
    cfg, st, _, _ = setup
    engine = ExecutionEngine(cfg, st, 'live', UpbitRestClient())
    result = run(engine.execute_market('KRW-ABC', 'BUY', 10000, 100, 100, 0, 'entry'))
    assert not result.ok and result.reason == 'live_not_ready'
    assert st.query('SELECT * FROM trades') == []


@pytest.mark.parametrize('filled_qty', [0, 40])
def test_ws_exit_retains_unfilled_position(setup, monkeypatch, filled_qty):
    from src.execution import ExecutionResult
    from src.risk import RiskManager
    cfg, st, rest, _ = setup
    sm = TradingStateMachine(cfg, rest, MarketCache(), st, RiskManager(cfg, 1000000), None, 'live')
    sm.portfolio.add('KRW-ABC', 100, 100, 95, 50)
    sm.exec_engine.execute_market = AsyncMock(return_value=ExecutionResult(
        filled_qty > 0, 'KRW-ABC', 'SELL', filled_qty, 90, 0, 0, 'test'))
    run(sm._process_exits_fast('KRW-ABC', 90))
    assert sm.portfolio.positions['KRW-ABC'].qty == 100 - filled_qty


def test_partial_exit_preserves_remainder_in_main_loop(setup, monkeypatch):
    from src.execution import ExecutionResult
    from src.risk import RiskManager
    cfg, st, rest, _ = setup
    sm = TradingStateMachine(cfg, rest, MarketCache(), st, RiskManager(cfg, 1000000), None, 'live')
    ready(rest)
    sm.portfolio.add('KRW-ABC', 100, 100, 95, 50)
    sm.portfolio.evaluate_exits = lambda *a, **k: [{'reason': 'time_exit', 'ratio': 1.0}]
    sm.exec_engine.execute_market = AsyncMock(return_value=ExecutionResult(
        True, 'KRW-ABC', 'SELL', 40, 100, 2, 0, 'time_exit'))
    run(sm._process_exits({'KRW-ABC': 100}))
    assert sm.portfolio.positions['KRW-ABC'].qty == 60
    assert sm.risk.realized_pnl == -2


def test_same_asset_journal_rejects_second_connection(tmp_path):
    path = str(tmp_path / 'shared.db')
    first, second = Storage(path), Storage(path)
    first.prepare_order({'identifier': 'a', 'market': 'KRW-ABC', 'side': 'bid'})
    with pytest.raises(RuntimeError, match='unresolved_order'):
        second.prepare_order({'identifier': 'b', 'market': 'BTC-ABC', 'side': 'ask'})
    assert len(second.pending_orders()) == 1


def test_reading_settled_order_does_not_reopen_journal(setup, monkeypatch):
    _, st, rest, _ = setup
    st.prepare_order({'identifier': 'settled', 'market': 'KRW-ABC', 'side': 'bid'})
    st.update_order('settled', fill('settled-uuid', 0, state='cancel'))
    st.acknowledge_order('settled-uuid')
    monkeypatch.setattr(rest, '_request_aiohttp', AsyncMock(return_value=fill('settled-uuid', 0, state='cancel')))
    run(rest.get_order('settled-uuid'))
    assert not st.pending_orders()


def test_confirmed_cancel_at_cutoff_does_not_submit_fallback(setup, monkeypatch):
    _, st, rest, engine = setup
    clock = [CUTOFF - 1]
    ready(rest, 'KRW-SOPH', lambda: clock[0])
    posts = []
    cancelled = [False]
    async def transport(method, url, params, body, auth):
        if url.endswith('/accounts'):
            return [{'currency': 'SOPH', 'balance': '100', 'locked': '0'}]
        if method == 'POST':
            posts.append(body)
            return {'uuid': 'last-limit', 'state': 'wait'}
        if method == 'DELETE':
            cancelled[0] = True
            clock[0] = CUTOFF
            return {'uuid': 'last-limit', 'state': 'cancel'}
        return fill('last-limit', 40, state='cancel' if cancelled[0] else 'wait', remaining=60)
    monkeypatch.setattr(rest, '_request_aiohttp', transport)
    result = run(engine.execute_market('KRW-SOPH', 'SELL', 10000, 100, 100, 0, 'time_exit'))
    assert result.ok and result.qty == 40
    assert len(posts) == 1 and not st.pending_orders()


def test_terminal_missing_fill_details_remains_quarantined(setup, monkeypatch):
    _, st, rest, engine = setup
    async def transport(method, url, params, body, auth):
        if url.endswith('/accounts'):
            return [{'currency': 'KRW', 'balance': '100000', 'locked': '0'}]
        if method == 'POST':
            return {'uuid': 'missing-trades', 'state': 'wait'}
        return {'uuid': 'missing-trades', 'state': 'done', 'executed_volume': '100', 'paid_fee': '5'}
    monkeypatch.setattr(rest, '_request_aiohttp', transport)
    result = run(engine.execute_market('KRW-ABC', 'BUY', 10000, 100, 100, 0, 'entry'))
    assert not result.ok and st.pending_orders()
    assert st.query('SELECT * FROM trades') == []
