# Live evidence, 2026-09-07 15:52–15:55 UTC (Track B)

Records pulled from the real `agent.binance.com` MCP server after the flip, from Binance's own history
endpoints (`spot.myTrades`, `futures_usds.accountTradeList` via `tool_execute`, `convert.getConvertTradeHistory`,
`wallet.queryUserUniversalTransferHistory`). Account uid and client order ids are omitted; order ids, trade ids,
tranIds, prices, quantities and fees are verbatim.

| file | what |
|---|---|
| `spot_myTrades_BTCUSDT.json` | BUY 0.00012 BTC @ 78,852.00 (order 66329815842) and SELL 0.0001 BTC @ 78,843.99 (order 66329831688) |
| `futures_usds_accountTradeList_ETHUSDT.json` | BUY 0.01 ETH @ 2,471.14 and SELL 0.01 @ 2,471.04 reduceOnly, realized −0.001 USDT |
| `convert_getConvertTradeHistory.json` | 5 USDT → 0.00674179 BNB, order 2354161734891321300, SUCCESS |
| `wallet_transfers.json` | 15 USDT Spot→Futures (tranId 409061557911), 14.97428911 USDT back (tranId 409021326347) |
| `spot_getAccount_after.json` | Final balances |
| `../../docs/shadow.png` | Dashboard in SHADOW mode during the run: live vs paper equity, divergence table |
| `screenshots/` | Binance website screenshots of the sub-account order history (added by the account owner) |

The twin's view of the same calls (13 mirrored events, divergence per write) is in `../../docs/shadow.png`;
the two twin gaps it exposed (sell by `quoteOrderQty`, convert quote id mapping) were fixed in commit e2d58b4.
