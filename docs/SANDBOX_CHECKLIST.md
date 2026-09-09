# NexusPMT sandbox checklist (before real funds)

Do **not** set `KALSHI_TRADING_MODE=live` against production until every box is checked.

## Branch gate

- [ ] All work merged to `staging` with green unit + smoke CI
- [ ] E2E against Kalshi **demo** + WorldMap on `somykida_net` passed

## Paper / demo

- [ ] Paper mode on demo for multiple sessions; PnL horizons (RT/5m/15m/30m/1h) update
- [ ] Trade ledger matches intended autonomous paper fills
- [ ] Sports / entertainment markets never appear in edge table or ledger as fills
- [ ] Kill switch halts new orders immediately
- [ ] Pause / resume behave as documented
- [ ] Flatten all requires `FLATTEN` confirm and clears paper positions
- [ ] Circuit breaker trips on simulated daily loss

## Demo live (still demo API)

- [ ] Tiny max notional; one intentional fill
- [ ] Cancel / reject / approval queue verified
- [ ] Staging soak ≥ 24h paper without crash loops

## Promote to prod

- [ ] `staging` → `prod` PR approved
- [ ] Prod image still defaults to **paper**
- [ ] Live flip only via runtime env after human approval + risk caps set
