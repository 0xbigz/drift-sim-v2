#%%
import sys 
import driftpy

from driftpy.accounts import get_market_account, get_user_account
from driftpy.math.amm import (
    calculate_swap_output, 
    calculate_amm_reserves_after_swap, 
    get_swap_direction
)
from driftpy.math.trade import calculate_trade_slippage, calculate_target_price_trade, calculate_trade_acquired_amounts
from driftpy.math.positions import calculate_base_asset_value, calculate_position_pnl
from driftpy.types import PositionDirection, AssetType, MarketPosition, SwapDirection
from driftpy.math.market import calculate_mark_price
from driftpy.constants.numeric_constants import AMM_TIMES_PEG_TO_QUOTE_PRECISION_RATIO, PEG_PRECISION

from solana.publickey import PublicKey

import json 
# import matplotlib.pyplot as plt 
import numpy as np 
import pandas as pd
from dataclasses import dataclass, field

from programs.clearing_house.state import Oracle, User
from programs.clearing_house.lib import ClearingHouse
from backtest.helpers import setup_new_user, adjust_oracle_pretrade
from driftpy.math.amm import calculate_price
from driftpy.constants.numeric_constants import AMM_RESERVE_PRECISION, QUOTE_PRECISION

@dataclass
class Event:     
    timestamp: int 
    
    def serialize_parameters(self):
        return json.loads(json.dumps(
            self, 
            default=lambda o: o.__dict__, 
            sort_keys=True, 
            indent=4
        ))
        
    def serialize_to_row(self):
        parameters = self.serialize_parameters()
        timestamp = parameters.pop("timestamp")
        event_name = parameters.pop("_event_name")
        row = {
            "event_name": event_name, 
            "timestamp": timestamp, 
            "parameters": json.dumps(parameters)
        }
        return row 
    
    @staticmethod
    def deserialize_from_row(class_type, event_row):
        event = json.loads(event_row.to_json())
        params = json.loads(event["parameters"])
        params["_event_name"] = event["event_name"]
        params["timestamp"] = event["timestamp"]
        event = class_type(**params)
        return event
    
    # this works for all Event subclasses
    @staticmethod
    def run_row(class_type, clearing_house: ClearingHouse, event_row) -> ClearingHouse:
        event = Event.deserialize_from_row(class_type, event_row)
        return event.run(clearing_house)
    
    @staticmethod
    def run_row_sdk(class_type, clearing_house: ClearingHouse, event_row) -> ClearingHouse:
        event = Event.deserialize_from_row(class_type, event_row)
        return event.run_sdk(clearing_house)
    
    def run(self, clearing_house: ClearingHouse) -> ClearingHouse:
        raise NotImplementedError

    # theres a lot of different inputs for this :/ 
    async def run_sdk(self, *args, **kwargs) -> ClearingHouse:
        raise NotImplementedError

@dataclass
class NullEvent(Event):     
    _event_name: str = "null"
    
    def run(self, clearing_house: ClearingHouse, verbose=False) -> ClearingHouse:
        return clearing_house

    def run_sdk(self):
        pass
    
@dataclass
class DepositCollateralEvent(Event): 
    user_index: int 
    deposit_amount: int
    username: str = "u"
    
    _event_name: str = "deposit_collateral"
    
    def run(self, clearing_house: ClearingHouse, verbose=False) -> ClearingHouse:
        if verbose:
            print(f'u{self.user_index} deposit...')
        clearing_house = clearing_house.deposit_user_collateral(
            self.user_index, 
            self.deposit_amount, 
            name=self.username
        )    
        return clearing_house

    async def run_sdk(self, provider, program, usdc_mint, user_kp) -> ClearingHouse:
        return await setup_new_user(
            provider, 
            program, 
            usdc_mint, 
            user_kp,
            self.deposit_amount,
        )
    
@dataclass 
class addLiquidityEvent(Event):
    market_index: int = 0 
    user_index: int = 0 
    token_amount: int = 0 

    _event_name: str = "add_liquidity"

    def run(self, clearing_house: ClearingHouse, verbose=False) -> ClearingHouse:
        if verbose:
            print(f'u{self.user_index} {self._event_name}...')

        clearing_house = clearing_house.add_liquidity(
            market_index=self.market_index,
            user_index=self.user_index,
            token_amount=self.token_amount
        )
        return clearing_house

    async def run_sdk(self, clearing_house): 
        return await clearing_house.add_liquidity(
            self.token_amount, 
            self.market_index
        )

@dataclass
class removeLiquidityEvent(Event):
    market_index: int = 0 
    user_index: int = 0 
    lp_token_amount: int = -1

    _event_name: str = "remove_liquidity"
    
    def run(self, clearing_house: ClearingHouse, verbose=False) -> ClearingHouse:
        if verbose:
            print(f'u{self.user_index} {self._event_name}...')
        
        clearing_house = clearing_house.remove_liquidity(
            self.market_index, 
            self.user_index, 
            self.lp_token_amount
        )    
        return clearing_house
    
    async def run_sdk(self, clearing_house) -> ClearingHouse:
        await clearing_house.remove_liquidity(
            self.lp_token_amount, 
            self.market_index
        )
    
@dataclass
class OpenPositionEvent(Event): 
    user_index: int 
    direction: str 
    quote_amount: int 
    market_index: int
    
    _event_name: str = "open_position"
    
    def run(self, clearing_house: ClearingHouse, verbose=False) -> ClearingHouse:
        if verbose:
            print(f'u{self.user_index} {self._event_name} {self.direction} {self.quote_amount}...')
        direction = {
            "long": PositionDirection.LONG,
            "short": PositionDirection.SHORT,
        }[self.direction]
        
        clearing_house = clearing_house.open_position(
            direction, 
            self.user_index, 
            self.quote_amount, 
            self.market_index
        )
        
        return clearing_house

    async def run_sdk(self, clearing_house, oracle_program=None, adjust_oracle_pre_trade=False) -> ClearingHouse:
        # tmp -- sim is quote open position v2 is base only
        market = await get_market_account(clearing_house.program, self.market_index)

        mark_price = calculate_price(
            market.amm.base_asset_reserve,
            market.amm.quote_asset_reserve,
            market.amm.peg_multiplier,
        )
        baa = int(self.quote_amount * AMM_RESERVE_PRECISION / QUOTE_PRECISION / mark_price)
        if baa == 0: 
            print('warning: baa too small -> rounding up')
            baa = market.amm.base_asset_amount_step_size
        
        direction = {
            "long": PositionDirection.LONG(),
            "short": PositionDirection.SHORT(),
        }[self.direction]

        if adjust_oracle_pre_trade: 
            assert oracle_program is not None
            await adjust_oracle_pretrade(
                baa, 
                direction, 
                market, 
                oracle_program
            )
        
        return await clearing_house.open_position(
            direction,
            baa,
            self.market_index
        )
                
@dataclass
class ClosePositionEvent(Event): 
    user_index: int 
    market_index: int
    _event_name: str = "close_position"
    
    def run(self, clearing_house: ClearingHouse, verbose=False) -> ClearingHouse:
        if verbose:
            print(f'u{self.user_index} {self._event_name}...')
        clearing_house = clearing_house.close_position(
            self.user_index, 
            self.market_index
        )
        
        return clearing_house
    
    async def run_sdk(self, clearing_house, oracle_program=None, adjust_oracle_pre_trade=False) -> ClearingHouse:
        # tmp -- sim is quote open position v2 is base only
        market = await get_market_account(clearing_house.program, self.market_index)
        user = await get_user_account(clearing_house.program, clearing_house.authority)
        position = None 
        for _position in user.positions: 
            if _position.market_index == self.market_index: 
                position = _position
                break 
        assert position is not None, "user not in market"

        direction = PositionDirection.LONG() if position.base_asset_amount < 0 else PositionDirection.SHORT()

        if adjust_oracle_pre_trade: 
            assert oracle_program is not None
            await adjust_oracle_pretrade(
                position.base_asset_amount, 
                direction, 
                market, 
                oracle_program
            )

        return await clearing_house.close_position(self.market_index)

 
         
@dataclass
class SettleLPEvent(Event): 
    user_index: int 
    market_index: int
    _event_name: str = "settle_lp"
    
    def run(self, clearing_house: ClearingHouse, verbose=False) -> ClearingHouse:
        if verbose:
            print(f'u{self.user_index} {self._event_name}...')
            
        clearing_house = clearing_house.settle_lp(
            self.market_index,
            self.user_index, 
        )
        
        return clearing_house

    async def run_sdk(self, clearing_house, settlee_pk):
        return await clearing_house.settle_lp(
            settlee_pk, 
            self.market_index
        )
         

# %%
