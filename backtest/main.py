#%%
%reload_ext autoreload
%autoreload 2

import sys
sys.path.insert(0, '../')
sys.path.insert(0, '../driftpy/src/')

import pandas as pd 
import numpy as np 

from driftpy.math.amm import *
from driftpy.math.trade import *
from driftpy.math.positions import *
from driftpy.math.market import *
from driftpy.math.user import *

from driftpy.types import *
from driftpy.constants.numeric_constants import *

from driftpy.setup.helpers import _usdc_mint, _user_usdc_account, mock_oracle, _setup_user, set_price_feed, adjust_oracle_pretrade
from driftpy.clearing_house import ClearingHouse
from driftpy.admin import Admin
from driftpy.types import OracleSource

from sim.events import * 
from driftpy.clearing_house import ClearingHouse as SDKClearingHouse
from driftpy.accounts import get_market_account
from driftpy.math.amm import calculate_mark_price_amm
from driftpy.accounts import get_user_account

from anchorpy import Provider, Program, create_workspace
from programs.clearing_house.state.market import SimulationAMM, SimulationMarket
from helpers import setup_bank, setup_market, setup_new_user, view_logs

#%%
folder_name = 'tmp4'
# folder_name = 'tmp'
events = pd.read_csv(f"./{folder_name}/events.csv")
clearing_houses = pd.read_csv(f"./{folder_name}/chs.csv")
events

#%%
# setup clearing house + bank + market 
# note first run `anchor localnet` in v2 dir

path = '../driftpy/protocol-v2'
path = '/Users/brennan/Documents/drift/protocol-v2'
workspace = create_workspace(path)
program: Program = workspace["clearing_house"]
oracle_program: Program = workspace["pyth"]
provider: Provider = program.provider

clearing_house, usdc_mint = await setup_bank(
    program
)

init_state = clearing_houses.iloc[0]
init_reserves = int(init_state.m0_base_asset_reserve) # 200 * 1e13
init_market = SimulationMarket(
    market_index=0,
    amm=SimulationAMM(
        oracle=None,
        base_asset_reserve=init_reserves, 
        quote_asset_reserve=init_reserves, 
        funding_period = 60 * 60, # 1 hour dont worry about funding updates for now 
        peg_multiplier=int(init_state.m0_peg_multiplier),
    )
)
oracle = await setup_market(
    clearing_house, 
    init_market, 
    workspace
)

#%%
from tqdm import tqdm

# fast init for users - airdrop takes a bit to finalize
user_indexs = np.unique([json.loads(e['parameters'])['user_index'] for _, e in events.iterrows() if 'user_index' in json.loads(e['parameters'])])
users = {}
for user_index in tqdm(user_indexs):
    user, tx_sig = await _setup_user(provider)
    users[user_index] = (user, tx_sig)
for i, (user, tx_sig) in tqdm(users.items()):
    await provider.connection.confirm_transaction(tx_sig, sleep_seconds=0.1)

user_chs = {}
user_token_amount = {}
user_baa_amount = {}

init_total_collateral = 0 

for i in tqdm(range(len(events))):
    event = events.iloc[i]
    
    if event.event_name == DepositCollateralEvent._event_name:
        event = Event.deserialize_from_row(DepositCollateralEvent, event)
        assert event.user_index not in user_chs, 'trying to re-init'
        assert event.user_index in users, "user not setup"
        print(f'=> {event.user_index} init user...')

        user_clearing_house, _ = await event.run_sdk(
            provider, 
            program, 
            usdc_mint, 
            users[event.user_index][0]
        )
        user_chs[event.user_index] = user_clearing_house
        init_total_collateral += event.deposit_amount

    elif event.event_name == OpenPositionEvent._event_name: 
        event = Event.deserialize_from_row(OpenPositionEvent, event)
        print(f'=> {event.user_index} opening position...')
        assert event.user_index in user_chs, 'user doesnt exist'

        assert event.user_index not in user_baa_amount

        ch: SDKClearingHouse = user_chs[event.user_index]
        await event.run_sdk(ch, oracle_program, adjust_oracle_pre_trade=True)
        
        user = await get_user_account(
            program, 
            ch.authority, 
        )
        user_baa_amount[event.user_index] = user.positions[0].base_asset_amount

    elif event.event_name == ClosePositionEvent._event_name: 
        event = Event.deserialize_from_row(ClosePositionEvent, event)
        print(f'=> {event.user_index} closing position...')
        assert event.user_index in user_chs, 'user doesnt exist'

        ch: SDKClearingHouse = user_chs[event.user_index]
        baa = user_baa_amount.pop(event.user_index)
        await event.run_sdk(ch, oracle_program, adjust_oracle_pre_trade=True)

    elif event.event_name == addLiquidityEvent._event_name: 
        event = Event.deserialize_from_row(addLiquidityEvent, event)
        print(f'=> {event.user_index} adding liquidity...')
        assert event.user_index in user_chs, 'user doesnt exist'

        ch: SDKClearingHouse = user_chs[event.user_index]
        user_token_amount[event.user_index] = event.token_amount
        await event.run_sdk(ch)

    elif event.event_name == removeLiquidityEvent._event_name:
        event = Event.deserialize_from_row(removeLiquidityEvent, event)
        print(f'=> {event.user_index} removing liquidity...')
        assert event.user_index in user_chs, 'user doesnt exist'

        if event.lp_token_amount == -1: # full burn 
            event.lp_token_amount = user_token_amount.pop(event.user_index)

        ch: SDKClearingHouse = user_chs[event.user_index]
        await event.run_sdk(ch)

        user = await get_user_account(
            program, 
            ch.authority, 
        )
        user_baa_amount[event.user_index] = user.positions[0].base_asset_amount

    elif event.event_name == SettleLPEvent._event_name: 
        event = Event.deserialize_from_row(SettleLPEvent, event)
        print(f'=> {event.user_index} settle lp...')
        ch: SDKClearingHouse = user_chs[event.user_index]
        await event.run_sdk(ch, ch.authority)
    else: 
        raise NotImplementedError

end_total_collateral = 0 
for (i, ch) in user_chs.items():
    user = await get_user_account(
        program, 
        ch.authority, 
    )

    balance = user.bank_balances[0].balance
    upnl = user.positions[0].unsettled_pnl
    total_user_collateral = balance + upnl

    end_total_collateral += total_user_collateral
    print(i, total_user_collateral)

market = await get_market_account(program, 0)
end_total_collateral += market.amm.total_fee_minus_distributions

print('market:', market.amm.total_fee_minus_distributions)
print(
    "=> difference in $, difference, end/init collateral",
    (end_total_collateral - init_total_collateral) / 1e6, 
    end_total_collateral - init_total_collateral, 
    (end_total_collateral, init_total_collateral)
)

#%%
#%%
#%%
#%%
#%%
#%%