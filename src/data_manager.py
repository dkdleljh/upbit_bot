"""
Historical data collection and storage system for Upbit markets.
Provides functionality to download and store candle data for backtesting.
"""
import asyncio
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Optional

import aiohttp
import pandas as pd

from .storage import Storage
from .utils import now_ms

LOGGER = logging.getLogger(__name__)


class HistoricalDataCollector:
    """Collects and stores historical candle data from Upbit API."""
    
    def __init__(self, storage: Storage):
        self.storage = storage
        self.base_url = "https://api.upbit.com/v1/candles/minutes/1"
        
    async def collect_market_data(self, market: str, start_date: datetime, 
                              end_date: datetime, max_concurrent: int = 5) -> bool:
        """
        Collect historical candle data for a specific market.
        
        Args:
            market: Market identifier (e.g., "KRW-BTC")
            start_date: Start date for data collection
            end_date: End date for data collection
            max_concurrent: Maximum concurrent requests
            
        Returns:
            True if successful, False otherwise
        """
        LOGGER.info(f"Collecting data for {market} from {start_date} to {end_date}")
        
        try:
            # Initialize candles table if not exists
            await self._ensure_candles_table()
            
            # Calculate date chunks (Upbit API has limits)
            date_chunks = self._calculate_date_chunks(start_date, end_date)
            
            # Collect data in chunks with concurrency control
            all_candles = []
            semaphore = asyncio.Semaphore(max_concurrent)
            
            tasks = []
            for chunk_start, chunk_end in date_chunks:
                task = self._collect_chunk(market, chunk_start, chunk_end, semaphore)
                tasks.append(task)
            
            chunk_results = await asyncio.gather(*tasks, return_exceptions=True)
            
            # Process results
            for result in chunk_results:
                if isinstance(result, Exception):
                    LOGGER.error(f"Error collecting chunk: {result}")
                elif result:
                    all_candles.extend(result)
            
            # Store candles in database
            if all_candles:
                await self._store_candles(market, all_candles)
                LOGGER.info(f"Stored {len(all_candles)} candles for {market}")
                return True
            else:
                LOGGER.warning(f"No candles collected for {market}")
                return False
                
        except Exception as e:
            LOGGER.error(f"Failed to collect data for {market}: {e}")
            return False
    
    async def collect_universe_data(self, markets: List[str], start_date: datetime, 
                                end_date: datetime) -> Dict[str, bool]:
        """
        Collect historical data for multiple markets.
        
        Args:
            markets: List of market identifiers
            start_date: Start date for data collection
            end_date: End date for data collection
            
        Returns:
            Dictionary mapping market to success status
        """
        LOGGER.info(f"Collecting data for {len(markets)} markets")
        
        results = {}
        
        # Collect with rate limiting to avoid hitting API limits
        for i, market in enumerate(markets):
            LOGGER.info(f"Processing market {i+1}/{len(markets)}: {market}")
            
            success = await self.collect_market_data(market, start_date, end_date, max_concurrent=3)
            results[market] = success
            
            # Rate limiting: wait between markets
            if i < len(markets) - 1:
                await asyncio.sleep(1)
        
        # Summary
        successful = sum(1 for success in results.values() if success)
        LOGGER.info(f"Data collection complete: {successful}/{len(markets)} markets successful")
        
        return results
    
    def _calculate_date_chunks(self, start_date: datetime, end_date: datetime) -> List[tuple]:
        """
        Split date range into chunks compatible with Upbit API limits.
        Upbit API returns max 200 candles per request (200 minutes = ~3.3 hours).
        """
        chunks = []
        current_start = start_date
        
        while current_start < end_date:
            # Use 3-hour chunks to be safe
            chunk_end = min(current_start + timedelta(hours=3), end_date)
            chunks.append((current_start, chunk_end))
            current_start = chunk_end
        
        return chunks
    
    async def _collect_chunk(self, market: str, start_date: datetime, 
                           end_date: datetime, semaphore: asyncio.Semaphore) -> List[Dict]:
        """Collect a chunk of candle data with semaphore control."""
        async with semaphore:
            try:
                start_ts = float(start_date.timestamp())
                end_ts = float(end_date.timestamp())

                # Upbit `to` 파라미터는 시각 문자열 기준.
                to_str = end_date.strftime("%Y-%m-%d %H:%M:%S")
                url = f"{self.base_url}?market={market}&to={to_str}&count=200"
                
                # Make request
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as response:
                        if response.status == 200:
                            candles = await response.json()
                            
                            # Filter by start date and format
                            formatted_candles = []
                            for candle in candles:
                                candle_ts = float(candle.get("timestamp", 0.0)) / 1000.0
                                if candle_ts <= 0:
                                    continue
                                if start_ts <= candle_ts <= end_ts:
                                    formatted_candles.append({
                                        'market': market,
                                        'timestamp': candle_ts,
                                        'open': float(candle['opening_price']),
                                        'high': float(candle['high_price']),
                                        'low': float(candle['low_price']),
                                        'close': float(candle['trade_price']),
                                        'volume': float(candle['candle_acc_trade_volume']),
                                        'value': float(candle['candle_acc_trade_price'])
                                    })
                            
                            # Sort by timestamp
                            formatted_candles.sort(key=lambda x: x['timestamp'])
                            
                            LOGGER.debug(f"Collected {len(formatted_candles)} candles for {market} chunk")
                            return formatted_candles
                        
                        else:
                            LOGGER.warning(f"API request failed for {market}: {response.status}")
                            return []
                            
            except Exception as e:
                LOGGER.error(f"Error collecting chunk for {market}: {e}")
                return []
    
    async def _ensure_candles_table(self):
        """Ensure candles table exists in storage."""
        create_table_sql = """
            CREATE TABLE IF NOT EXISTS candles (
                market TEXT NOT NULL,
                timestamp REAL NOT NULL,
                open REAL NOT NULL,
                high REAL NOT NULL,
                low REAL NOT NULL,
                close REAL NOT NULL,
                volume REAL NOT NULL,
                value REAL NOT NULL,
                PRIMARY KEY (market, timestamp)
            )
        """
        
        create_index_sql = """
            CREATE INDEX IF NOT EXISTS idx_candles_market_time 
            ON candles(market, timestamp)
        """
        
        self.storage.execute(create_table_sql)
        self.storage.execute(create_index_sql)
        
    async def _store_candles(self, market: str, candles: List[Dict]):
        """Store candles in the database."""
        if not candles:
            return
        
        # Use INSERT OR REPLACE to handle duplicates
        insert_sql = """
            INSERT OR REPLACE INTO candles 
            (market, timestamp, open, high, low, close, volume, value)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """
        
        # Prepare data for batch insert
        data = []
        for candle in candles:
            data.append((
                candle['market'],
                candle['timestamp'],
                candle['open'],
                candle['high'],
                candle['low'],
                candle['close'],
                candle['volume'],
                candle['value']
            ))
        
        # Batch insert
        self.storage.execute_many(insert_sql, data)
        LOGGER.debug(f"Stored {len(data)} candles for {market}")
    
    def get_data_coverage(self, market: str, start_date: datetime, 
                        end_date: datetime) -> Dict:
        """
        Get data coverage information for a market.
        
        Returns:
            Dictionary with coverage statistics
        """
        query = """
            SELECT 
                COUNT(*) as total_candles,
                MIN(timestamp) as start_timestamp,
                MAX(timestamp) as end_timestamp,
                COUNT(DISTINCT DATE(timestamp, 'unixepoch')) as unique_days
            FROM candles 
            WHERE market = ? AND timestamp BETWEEN ? AND ?
        """
        
        result = self.storage.query_one(
            query, 
            (market, start_date.timestamp(), end_date.timestamp())
        )
        
        if result and result['total_candles'] > 0:
            expected_minutes = int((end_date - start_date).total_seconds() / 60)
            coverage_pct = min(100, (result['total_candles'] / expected_minutes) * 100)
            
            return {
                'market': market,
                'total_candles': result['total_candles'],
                'expected_candles': expected_minutes,
                'coverage_pct': coverage_pct,
                'start_time': datetime.fromtimestamp(result['start_timestamp']),
                'end_time': datetime.fromtimestamp(result['end_timestamp']),
                'unique_days': result['unique_days']
            }
        else:
            return {
                'market': market,
                'total_candles': 0,
                'expected_candles': int((end_date - start_date).total_seconds() / 60),
                'coverage_pct': 0,
                'start_time': None,
                'end_time': None,
                'unique_days': 0
            }
    
    def get_missing_data_ranges(self, market: str, start_date: datetime, 
                             end_date: datetime, gap_minutes: int = 10) -> List[tuple]:
        """
        Identify missing data ranges for a market.
        
        Args:
            market: Market identifier
            start_date: Start date to check
            end_date: End date to check
            gap_minutes: Minimum gap size to consider as missing data
            
        Returns:
            List of (start, end) tuples for missing ranges
        """
        query = """
            SELECT timestamp FROM candles 
            WHERE market = ? AND timestamp BETWEEN ? AND ?
            ORDER BY timestamp
        """
        
        candles = self.storage.query(query, (market, start_date.timestamp(), end_date.timestamp()))
        
        if not candles:
            return [(start_date, end_date)]
        
        missing_ranges = []
        timestamps = [c['timestamp'] for c in candles]
        
        # Check gaps
        for i in range(len(timestamps) - 1):
            current_time = datetime.fromtimestamp(timestamps[i])
            next_time = datetime.fromtimestamp(timestamps[i + 1])
            
            # If gap is larger than specified minutes
            if (next_time - current_time).total_seconds() > gap_minutes * 60:
                gap_start = current_time + timedelta(minutes=1)
                gap_end = next_time - timedelta(minutes=1)
                missing_ranges.append((gap_start, gap_end))
        
        # Check start and end
        first_time = datetime.fromtimestamp(timestamps[0])
        if first_time > start_date:
            missing_ranges.insert(0, (start_date, first_time - timedelta(minutes=1)))
        
        last_time = datetime.fromtimestamp(timestamps[-1])
        if last_time < end_date:
            missing_ranges.append((last_time + timedelta(minutes=1), end_date))
        
        return missing_ranges


class DataManager:
    """High-level interface for managing historical data."""
    
    def __init__(self, storage: Storage):
        self.storage = storage
        self.collector = HistoricalDataCollector(storage)
    
    async def ensure_data_availability(self, markets: List[str], start_date: datetime, 
                                   end_date: datetime, fill_gaps: bool = True) -> Dict[str, bool]:
        """
        Ensure historical data is available for specified markets.
        
        Args:
            markets: List of market identifiers
            start_date: Required start date
            end_date: Required end date
            fill_gaps: Whether to fill missing data gaps
            
        Returns:
            Dictionary mapping market to availability status
        """
        LOGGER.info(f"Checking data availability for {len(markets)} markets")
        
        results = {}
        
        for market in markets:
            coverage = self.collector.get_data_coverage(market, start_date, end_date)
            
            if coverage['coverage_pct'] >= 95:  # Good coverage
                results[market] = True
                LOGGER.info(f"{market}: {coverage['coverage_pct']:.1f}% coverage")
                
            elif fill_gaps:
                LOGGER.info(f"{market}: {coverage['coverage_pct']:.1f}% coverage, filling gaps")
                
                # Find missing ranges
                missing_ranges = self.collector.get_missing_data_ranges(market, start_date, end_date)
                
                # Collect missing data
                success = True
                for missing_start, missing_end in missing_ranges:
                    chunk_success = await self.collector.collect_market_data(
                        market, missing_start, missing_end
                    )
                    success = success and chunk_success
                
                results[market] = success
                
            else:
                results[market] = False
                LOGGER.warning(f"{market}: Insufficient coverage ({coverage['coverage_pct']:.1f}%)")
        
        return results
    
    def export_data_to_csv(self, markets: List[str], start_date: datetime, 
                         end_date: datetime, output_dir: str = "data/exports"):
        """
        Export historical data to CSV files for analysis.
        """
        import os
        
        os.makedirs(output_dir, exist_ok=True)
        
        for market in markets:
            query = """
                SELECT market, timestamp, open, high, low, close, volume, value
                FROM candles 
                WHERE market = ? AND timestamp BETWEEN ? AND ?
                ORDER BY timestamp
            """
            
            candles = self.storage.query(query, (market, start_date.timestamp(), end_date.timestamp()))
            
            if candles:
                # Convert to DataFrame
                df = pd.DataFrame(candles)
                df['datetime'] = pd.to_datetime(df['timestamp'], unit='s')
                
                # Save to CSV
                filename = f"{market.replace('-', '_')}_{start_date.date()}_to_{end_date.date()}.csv"
                filepath = os.path.join(output_dir, filename)
                
                df.to_csv(filepath, index=False)
                LOGGER.info(f"Exported {len(df)} candles to {filepath}")
            else:
                LOGGER.warning(f"No data to export for {market}")
