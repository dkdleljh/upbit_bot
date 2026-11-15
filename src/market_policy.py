"""Order eligibility, independent of strategy scores and position ownership."""
from datetime import datetime
import math
import time


class MarketPolicy:
    def __init__(self, cfg=None, clock=None):
        cfg = (cfg or {}).get('market_safety', {})
        self.clock = clock or time.time
        self.catalog_ttl = float(cfg.get('catalog_max_age_seconds', 120))
        self.data_ttl = float(cfg.get('quote_max_age_seconds', 90))
        self.blocked_assets = set(cfg.get('blocked_buy_assets', ['SOPH', 'ZIL']))
        dates = cfg.get('trading_ends_at', {
            'SOPH': '2026-11-16T15:00:00+09:00',
            'ZIL': '2026-11-16T15:00:00+09:00',
        })
        self.ends = {}
        for asset, value in dates.items():
            dt = datetime.fromisoformat(str(value))
            if dt.tzinfo is None:
                raise ValueError('Trading cutoff must include a timezone')
            self.ends[asset] = dt.timestamp()
        self.markets = {}
        self.catalog_at = None
        self.catalog_ok = False
        self.seen = {}

    def update_catalog(self, rows):
        self.markets = {r['market']: r for r in rows}
        self.catalog_at = self.clock()
        self.catalog_ok = bool(rows)
        self.seen = {k: v for k, v in self.seen.items() if k[0] in self.markets}

    def record_data(self, market, kind, row):
        # Exchange timestamps, not just receipt times, prevent old packets reviving a quote.
        try:
            stamp = float(row['timestamp']) / 1000
        except (KeyError, ValueError, TypeError):
            return
        now = self.clock()
        if math.isfinite(stamp) and 0 <= now - stamp <= self.data_ttl:
            self.seen[(market, kind)] = stamp

    def data_fresh(self, market):
        now = self.clock()
        return all((market, kind) in self.seen and
                   0 <= now - self.seen[(market, kind)] <= self.data_ttl
                   for kind in ('ticker', 'orderbook'))

    def reason(self, market, side, require_data=False):
        now = self.clock()
        asset = market.split('-', 1)[-1]
        if asset in self.ends and now >= self.ends[asset]:
            return 'trading_ended'
        if side in ('BUY', 'bid') and asset in self.blocked_assets:
            return 'blocked_buy_asset'
        if (not self.catalog_ok or self.catalog_at is None or
                not 0 <= now - self.catalog_at <= self.catalog_ttl):
            return 'market_catalog_unavailable'
        row = self.markets.get(market)
        if row is None:
            return 'market_unavailable'
        if side in ('BUY', 'bid'):
            event = row.get('market_event') or {}
            warning = event.get('warning')
            if not isinstance(warning, bool):
                legacy = row.get('market_warning')
                warning = {'NONE': False, 'CAUTION': True}.get(legacy)
            if warning is not False:
                return 'market_warning_or_unknown'
        if require_data and not self.data_fresh(market):
            return 'market_data_stale'
        return None
