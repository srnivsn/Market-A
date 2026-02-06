"""screen_stocks.py

Technical screening tool for NIFTY 500 stocks.

Criteria:
 - Price > 50-day EMA: medium-term uptrend
 - RSI (14) between 50-70: positive momentum without overbought
 - ADX > 25: trend strength
 - Volume Spike: current volume >= 1.5x 20-day average

Usage:
  python screen_stocks.py --tickers TCS,INFY,RELIANCE --output results.csv
  python screen_stocks.py --sample --output results.csv
  python screen_stocks.py --tickers-file my_tickers.txt --period 6mo --output results.csv
"""

from __future__ import annotations
import argparse
import logging
from typing import List
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import numpy as np
import yfinance as yf
import pandas_ta as ta

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


def calculate_ema(series, period):
    """Calculate EMA using pandas ewm."""
    return series.ewm(span=period, adjust=False).mean()


def calculate_rsi(series, period=14):
    """Calculate RSI using pure pandas - ensure it returns a clean Series."""
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / (loss + 1e-10)  # Avoid division by zero
    rsi = 100 - (100 / (1 + rs))
    return pd.Series(rsi, index=series.index)  # Ensure it's a Series


def calculate_adx(high, low, close, period=14):
    """Calculate ADX using pure pandas."""
    plus_dm = high.diff()
    minus_dm = -low.diff()
    
    # Directional movements
    plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0)
    minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0)
    
    # True Range
    tr1 = high - low
    tr2 = abs(high - close.shift())
    tr3 = abs(low - close.shift())
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    
    # Smoothed values
    plus_di = 100 * (plus_dm.rolling(period).sum() / tr.rolling(period).sum())
    minus_di = 100 * (minus_dm.rolling(period).sum() / tr.rolling(period).sum())
    
    # DX
    dx = 100 * abs(plus_di - minus_di) / (plus_di + minus_di + 0.001)
    adx = dx.rolling(period).mean()
    
    return adx


def screen_stock(ticker: str, period: str = '3mo') -> dict | None:
    """
    Screen a single stock against technical criteria:
    - Price > 50-day EMA
    - RSI (14) between 50 and 70
    - ADX > 25
    - Volume Spike: current volume >= 1.5x 20-day avg volume
    
    Returns a dict with stock details and criteria met, or None if it fails to fetch data.
    """
    try:
        # Fetch data (append .NS for NSE listing if not already present)
        if not ticker.endswith('.NS'):
            ticker_symbol = f"{ticker}.NS"
        else:
            ticker_symbol = ticker
        
        logger.info("Fetching data for %s (period=%s)...", ticker_symbol, period)
        raw_data = yf.download(ticker_symbol, period=period, progress=False, threads=False)
        
        if raw_data is None or len(raw_data) == 0:
            logger.warning("No data fetched for %s", ticker_symbol)
            return None
        
        # Ensure we have proper Series, not DataFrames
        data = pd.DataFrame({
            'High': raw_data['High'].squeeze() if isinstance(raw_data['High'], pd.DataFrame) else raw_data['High'],
            'Low': raw_data['Low'].squeeze() if isinstance(raw_data['Low'], pd.DataFrame) else raw_data['Low'],
            'Close': raw_data['Close'].squeeze() if isinstance(raw_data['Close'], pd.DataFrame) else raw_data['Close'],
            'Volume': raw_data['Volume'].squeeze() if isinstance(raw_data['Volume'], pd.DataFrame) else raw_data['Volume'],
        })
        
        if len(data) < 50:
            logger.warning("Insufficient data for %s (got %d rows, need 50+)", ticker, len(data))
            return None
        
        logger.info("Got %d rows for %s", len(data), ticker)
        
        # Calculate indicators using pure pandas
        try:
            # EMA 50
            ema50 = calculate_ema(data['Close'], 50)
            
            # RSI 14
            rsi14 = calculate_rsi(data['Close'], 14)
            
            # ADX
            adx = calculate_adx(data['High'], data['Low'], data['Close'], 14)
            
            # Volume average (20-day)
            vol_avg_20 = data['Volume'].rolling(window=20).mean()
            
            # Get current values (last row) - use numpy scalar extraction
            current_price = float(data['Close'].values[-1])
            current_ema50 = float(ema50.values[-1])
            rsi_last = rsi14.values[-1]
            current_rsi = float(rsi_last) if np.isfinite(rsi_last) else 50.0
            adx_last = adx.values[-1]
            current_adx = float(adx_last) if np.isfinite(adx_last) else 0.0
            current_volume = int(data['Volume'].values[-1])
            vol_avg_last = vol_avg_20.values[-1]
            current_vol_avg = float(vol_avg_last) if np.isfinite(vol_avg_last) else 0.0
        except Exception as calc_err:
            logger.warning("Indicator calculation/extraction failed for %s: %s", ticker, str(calc_err)[:100])
            return None
        
        # Check criteria
        price_above_ema = current_price > current_ema50
        rsi_in_range = (50.0 <= current_rsi <= 70.0)
        adx_strong = (current_adx > 25.0)
        volume_spike = (current_volume >= (current_vol_avg * 1.5)) if current_vol_avg > 0 else False
        
        # All criteria met?
        all_met = price_above_ema and rsi_in_range and adx_strong and volume_spike
        
        result = {
            'Ticker': ticker,
            'Price': round(current_price, 2),
            'EMA50': round(current_ema50, 2),
            'Price>EMA50': price_above_ema,
            'RSI14': round(current_rsi, 2),
            'RSI_50_70': rsi_in_range,
            'ADX': round(current_adx, 2),
            'ADX>25': adx_strong,
            'Volume': int(current_volume),
            'VolAvg20': int(current_vol_avg),
            'VolumeSpike': volume_spike,
            'AllCriteria': all_met,
        }
        
        return result
        
    except Exception as e:
        logger.warning("Error screening %s: %s", ticker, str(e))
        return None


def screen_stocks(tickers: List[str], period: str = '3mo', max_workers: int = 5) -> pd.DataFrame:
    """Screen multiple stocks in parallel and return results DataFrame."""
    results = []
    total = len(tickers)
    
    logger.info("Screening %d stocks with period=%s (max %d parallel workers)...", total, period, max_workers)
    
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(screen_stock, t, period): t for t in tickers}
        completed = 0
        for future in as_completed(futures):
            completed += 1
            ticker = futures[future]
            try:
                result = future.result()
                if result:
                    results.append(result)
                    if result['AllCriteria']:
                        logger.info("[%d/%d] ✓ %s: ALL CRITERIA MET | Price=%.2f RSI=%.2f ADX=%.2f", 
                                   completed, total, ticker, result['Price'], result['RSI14'], result['ADX'])
                    else:
                        logger.debug("[%d/%d] %s: partial match", completed, total, ticker)
            except Exception as e:
                logger.debug("[%d/%d] %s: error: %s", completed, total, ticker, str(e)[:100])
    
    df = pd.DataFrame(results)
    if len(df) > 0:
        passed = df[df['AllCriteria']]
        logger.info("Screening complete: %d stocks screened, %d passed all criteria", len(df), len(passed))
    else:
        logger.info("Screening complete: no data fetched")
        passed = df
    
    return df, passed


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Screen NIFTY 500 stocks by technical criteria")
    parser.add_argument("--tickers", help="Comma-separated list of tickers (e.g., TCS,INFY,RELIANCE)")
    parser.add_argument("--tickers-file", help="File with one ticker per line")
    parser.add_argument("--sample", action='store_true', help="Use sample NIFTY 500 tickers")
    parser.add_argument("--period", default='3mo', help="Historical period (e.g., 3mo, 6mo, 1y, 2y)")
    parser.add_argument("--output", required=True, help="Output CSV file for all results")
    parser.add_argument("--output-passed", help="Output CSV file for stocks that passed ALL criteria (optional)")
    parser.add_argument("--workers", type=int, default=5, help="Max parallel download workers")
    
    args = parser.parse_args(argv)
    
    # Collect tickers
    tickers = []
    if args.sample:
        try:
            from nifty500_tickers import SAMPLE_NIFTY500_TICKERS
            tickers = SAMPLE_NIFTY500_TICKERS
            logger.info("Using sample NIFTY 500 tickers (%d stocks)", len(tickers))
        except ImportError:
            logger.error("Cannot import SAMPLE_NIFTY500_TICKERS from nifty500_tickers.py")
            return 1
    elif args.tickers:
        tickers = [t.strip() for t in args.tickers.split(',')]
        logger.info("Using %d provided tickers", len(tickers))
    elif args.tickers_file:
        try:
            with open(args.tickers_file, 'r') as f:
                tickers = [line.strip() for line in f if line.strip()]
            logger.info("Loaded %d tickers from %s", len(tickers), args.tickers_file)
        except Exception as e:
            logger.error("Failed to read tickers file: %s", e)
            return 1
    else:
        parser.print_help()
        logger.error("Please provide --tickers, --tickers-file, or --sample")
        return 1
    
    if not tickers:
        logger.error("No tickers provided")
        return 1
    
    # Screen stocks
    all_results, passed = screen_stocks(tickers, period=args.period, max_workers=args.workers)
    
    # Save results
    if len(all_results) > 0:
        all_results.to_csv(args.output, index=False)
        logger.info("Saved %d results to %s", len(all_results), args.output)
        
        if args.output_passed and len(passed) > 0:
            passed.to_csv(args.output_passed, index=False)
            logger.info("Saved %d passed stocks to %s", len(passed), args.output_passed)
    else:
        logger.warning("No results to save")
    
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
