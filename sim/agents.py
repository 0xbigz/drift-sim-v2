from multiprocessing import Event
from driftpy.math.amm import calculate_amm_reserves_after_swap, get_swap_direction
from driftpy.math.amm import calculate_swap_output, calculate_terminal_price, calculate_mark_price_amm
from driftpy.math.trade import calculate_trade_slippage, calculate_target_price_trade, calculate_trade_acquired_amounts
from driftpy.math.positions import calculate_base_asset_value, calculate_position_pnl
from driftpy.types import PositionDirection, AssetType, MarketPosition
from driftpy.math.market import calculate_mark_price
from driftpy.constants.numeric_constants import AMM_TIMES_PEG_TO_QUOTE_PRECISION_RATIO, MARK_PRICE_PRECISION, PEG_PRECISION

from solana.publickey import PublicKey
import copy

import pandas as pd
import numpy as np

from programs.clearing_house.state import Oracle, User
from programs.clearing_house.lib import ClearingHouse
from sim.events import OpenPositionEvent, NullEvent

''' Agents ABC '''

class Agent:
    def init(self):
        ''' define params of agent '''
        pass

    def run(self, state_i: ClearingHouse) -> Event:
        ''' returns an event '''
        pass


class Arb(Agent):
    ''' arbitrage a single market to oracle'''
    def __init__(self, intensity: float, market_index: int, user_index: int, lookahead:int = 0):
        # assert(intensity > 0 and intensity <= 1)
        self.user_index = user_index
        self.intensity = intensity
        self.market_index = market_index
        self.lookahead = lookahead # default to looking at oracle at 0
        
    def run(self, state_i: ClearingHouse) -> Event:
        market_index = self.market_index
        user_index = self.user_index
        intensity = self.intensity

        now = state_i.time                                                             
        market = state_i.markets[market_index]
        oracle: Oracle = market.amm.oracle
        oracle_price = oracle.get_price(now)

        cur_mark = calculate_mark_price(market, oracle_price)
        target_mark = oracle.get_price(now + self.lookahead)
        target_mark = (target_mark - cur_mark) * intensity + cur_mark # only arb 1% of gap?
        # print(now, market.amm.peg_multiplier, calculate_mark_price_amm(market.amm), cur_mark, target_mark)


        # print(cur_mark, target_mark)

        #account for exchange fee in arb price
        exchange_fee = float(state_i.fee_structure.numerator)/state_i.fee_structure.denominator
        # print(exchange_fee)
        if target_mark < cur_mark*(1+exchange_fee) and target_mark > cur_mark*(1-exchange_fee):
            target_mark = cur_mark
        elif target_mark > cur_mark:
            target_mark = target_mark * (1-exchange_fee)
            # print('long to', target_mark, 'vs', cur_mark)
        elif target_mark < cur_mark:
            target_mark = target_mark * (1+exchange_fee)
            # print('short to', target_mark, 'vs', cur_mark)
        else:
            target_mark = cur_mark
        

        unit = AssetType.QUOTE

        direction, trade_size, entry_price, target_price = \
            calculate_target_price_trade(
                market, 
                int(target_mark * MARK_PRICE_PRECISION), 
                unit, 
                use_spread=True,
                oracle_price=oracle_price
            )
        
        trade_size = int(abs(trade_size)) # whole numbers only 
        if trade_size:
            print('NOW: ', now)
        quote_asset_reserve = (
            trade_size 
            * AMM_TIMES_PEG_TO_QUOTE_PRECISION_RATIO 
            / market.amm.peg_multiplier
        )
        
        # TODO: add this check to the clearing house 
        # convert to reserve amount 
        # trade size too small = no trade 
        if quote_asset_reserve < market.amm.minimum_quote_asset_trade_size: 
            trade_size = 0 
        
        if direction == PositionDirection.LONG:
            direction = 'long'
        else:
            direction = 'short'

        # arb is from 0 - x (intensity)
        # if trade_size != 0 and entry_price != 0:
        #     trade_size = max(self.intensity*100, 
        #                         min(trade_size*entry_price/(1e13), self.intensity*10000)
        #                         )/(entry_price) * 1e13
        
        if trade_size == 0:
            event = NullEvent(timestamp=now)
        else: 
            event = OpenPositionEvent(self.user_index, direction, int(trade_size), now, market_index)
            print(direction, trade_size/1e6, 'LUNA-PERP @', entry_price, '(',target_price/1e10,')')


        # print(now, market.amm.peg_multiplier, calculate_mark_price_amm(market.amm), cur_mark, target_mark)

        return event

class Noise(Agent):
    def __init__(self, intensity: float, market_index: int, user_index: int, lookahead:int = 0):
        # assert(intensity > 0 and intensity <= 1)
        self.intensity = intensity
        self.user_index = user_index
        self.market_index = market_index
        self.lookahead = lookahead # default to looking at oracle at 0 

    def run(self, state_i: ClearingHouse) -> Event:
        market_index = self.market_index
        user_index = self.user_index
        intensity = self.intensity  
        direction = 'long'
        trade_size = int(1e6)
        now = state_i.time                                                             

        event = OpenPositionEvent(self.user_index, direction, trade_size, now, market_index)
        return event
