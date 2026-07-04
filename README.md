# Memecoin Sniper Lab

A **paper-first research instrument** for testing whether any positive
expectancy exists in sniping new Solana memecoin launches — before risking a
single dollar. It is engineered like a trading system and honest like a lab
notebook. It is **not** a money printer, and if you run it expecting one, the
data it produces will patiently explain why.

```
detection (WS) ──> filters ──> score ──> [paper|shadow|live] entry ──> exit engine
      │                │                        │                        │
      └────────────────┴──────── SQLite ────────┴────────────────────────┘
                                    │
                     read-only localhost dashboard + evaluation protocol
```

---

## Read this first: realistic expectations

- **Most snipes lose.** The overwhelming majority of new launches are scams:
  rugs, bundled dumps, honeypots. The filter pipeline rejects most of what it
  sees, and the survivors still mostly go to zero. Expected EV of the whole
  category is **negative** after costs.
- **You will probably not win block-0 on shared RPC.** Winning the first slot
  is a latency race against co-located bots on Geyser/gRPC feeds and private
  validators. On a free/cheap shared endpoint you are seconds behind. The
  `block_0` trigger mode exists as a **control arm** so your own latency logs
  (`event_seen`, `tx_landed`, slot delta) prove this to you with data.
- **Paper P&L is an optimistic upper bound.** Even though the paper model
  subtracts priority fees, the ~1% route fee, pool fees, entry/exit slippage
  against real pool depth, adverse-selection drift, and a fill-probability
  discount, it still cannot model being the exit liquidity for a same-slot
  bundler. Real results will be worse. The shadow phase measures how much
  worse.
- **Win rate is not expectancy.** A 70% win rate with +30% average wins and a
  tail of −100% rugs is a losing strategy. Optimize the **mean net P&L per
  trade** and stare at the left tail of the distribution (the dashboard shows
  median and p5 for exactly this reason).
- **Fees bleed even when nothing fills.** Failed snipes still pay base +
  priority fees. There is a daily fee-spend cap independent of P&L.

If, knowing all that, you still want to measure it — that's what this is for.

---

## The edge is a config choice, not hardcoded

`trigger.mode` selects the hypothesis under test (same code, different bet):

| mode | entry trigger | expectation |
|---|---|---|
| `block_0` | first slot after pool creation | structural loser on shared infra; **control arm** |
| `migration` | the instant a pump.fun token graduates to PumpSwap | more realistic window; token survived the curve |
| `filter_edge` | deliberate few-second delay; entry gated purely on filter/score quality | the hypothesis this architecture is built to test |

## Filter pipeline (cheap → expensive, every rejection logged with its reason)

1. **authorities** — mint & freeze authority revoked (a live freeze authority
   can freeze your account forever).
2. **token2022** — Token-2022 extension inspection, the modern honeypot
   vector: transfer hooks, transfer fees, permanent delegate, default-frozen
   state, non-transferable, unknown extensions. All hard fails.
3. **liquidity** — minimum initial SOL depth.
4. **lp_status** — LP **burned** vs **locked** as *distinct* checks: burned =
   supply verifiably destroyed / authority gone; locked = held by a specific
   known locker program (weaker — trust moves to the locker). Anything else
   is rug-capable and scores 0.
5. **holders** — top-holder / deployer concentration caps.
6. **bundle** — share of supply grabbed by bundled wallets in the launch block.
7. **deployer** — wallet age, funding source, prior token history via the
   Helius enhanced-tx API; async + SQLite-cached; never gates `block_0`.
8. **honeypot** — a **simulated sell** on a real route (`simulateTransaction`
   with an existing holder as the seller), because the only way to know a
   token can be sold is to try selling it. Bonding-curve tokens are handled
   via their own route pre-migration.

Outputs combine into a 0–1 score. The acceptance threshold is **pre-registered**
(`config/preregistration.json`, hash-recorded in the DB on first run) and must
never be tuned on the data it is judged on.

## Exit engine (fully automatic)

- Tiered take-profits (default: sell 50% at 2×, 25% at 5×), computed on
  *realizable* value against actual pool depth, not chart price.
- Hard stop-loss (default −60%) and a max-hold force-exit (default 10 min).
- **You cannot stop-loss out of a rug.** Real-time LP monitoring
  (accountSubscribe on the pool's SOL vault + poll fallback) triggers an
  emergency exit on liquidity pulls; a sell that cannot land is recorded as
  `could_not_sell` — a distinct outcome category, never a normal loss.
- Priority-fee **escalation ladder** on sells with retries (in a dump,
  everyone exits at once; a fixed fee never lands), fresh blockhash per
  attempt (Solana blockhashes die in ~60–90 s).

## Risk layer

Hard per-trade cap (default **$2**), max concurrent positions, daily loss kill
switch, **daily fee-spend cap**, SOL floor reserve (never spend the gas you
need to exit), RPC-latency degradation halt, halt on unhandled exceptions,
duplicate-launch double-buy guard (in-memory + DB unique index), and crash
recovery that reconciles open positions **from the chain** (the chain is the
source of truth, not SQLite) and resumes the exit engine.

External kill switch, reachable from outside the process:

```bash
touch KILL            # sentinel file — halts entries, winds down positions
kill -USR1 <pid>      # or signal
```

---

## Setup

### 1. Install

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pytest                      # 146 tests should pass before you trust anything
```

### 2. RPC (free tier is fine — that's part of the experiment)

Create a free account at [helius.dev](https://www.helius.dev/) (or any
provider with WebSocket support). Copy `.env.example` to `.env` and fill in:

```
SNIPER_RPC_HTTP_URL=https://mainnet.helius-rpc.com/?api-key=...
SNIPER_RPC_WS_URL=wss://mainnet.helius-rpc.com/?api-key=...
SNIPER_HELIUS_API_KEY=...        # enables the deployer-history filter
```

`.env` is gitignored. Keep it that way.

### 3. Burner wallet (only needed for shadow/live; paper needs no key)

Create a **fresh throwaway wallet** (e.g. `solana-keygen new` or any wallet
app), fund it with only what you are fully prepared to lose ($50–100), and
export its **base58 private key** — *never a seed phrase*. Then encrypt it:

```bash
python -m sniper.keystore create        # writes keystore.json (0600, scrypt+Fernet)
```

The plaintext `SNIPER_PRIVATE_KEY` env var also works but is deliberately
noisy about being the worse option. The key is never logged and never leaves
the process.

### 4. Pre-register your threshold (before collecting evaluation data)

```bash
cp config/preregistration.example.json config/preregistration.json
# edit score_threshold + trigger_mode, set registered_at, save — then don't touch it
```

The file's hash is recorded in the DB on first run; changing it mid-sample
voids the evaluation (the gate will tell you so).

### 5. NTP

Latency numbers are only meaningful if the host clock is right. The bot
checks the clock offset via SNTP at startup and flags all latency samples as
untrusted if it's off; run `chrony`/`systemd-timesyncd` on the host.

---

## Running

```bash
# Slice 1-2: pure observation — watch launches, filters, latencies. No trades.
python -m sniper.main --mode observe

# Slice 3-4 (DEFAULT): paper trading with the honest cost model + exits + risk
python -m sniper.main --mode paper

# choose the hypothesis:
python -m sniper.main --mode paper --trigger filter_edge
python -m sniper.main --mode paper --trigger migration
python -m sniper.main --mode paper --trigger block_0     # the control arm

# dashboard (read-only, localhost only):
open http://127.0.0.1:8787/

# the go-live gate:
python -m sniper.evaluate
```

**Shadow (canary) mode** — tiny real trades (0.01–0.05 SOL) whose only purpose
is calibration: measuring actual fill rate and realized slippage against what
paper predicted for the same moment. This is the real test of the paper model.

```bash
python -m sniper.main --mode shadow
```

**Live mode** is double-gated. It refuses to arm unless BOTH are true:

```yaml
# config: execution.live_enabled: true
```
```bash
# environment:
export SNIPER_LIVE_ACK=I_UNDERSTAND_THIS_WILL_PROBABLY_LOSE_MONEY
python -m sniper.main --mode live
```

Do not do this until `python -m sniper.evaluate` prints **GO** — see
[docs/evaluation_protocol.md](docs/evaluation_protocol.md). If it ever does,
size positions with heavily discounted fractional Kelly (built into the risk
layer), never full Kelly, never more than the per-trade cap.

### Suggested build/run order

1. **Observe** for a few days: verify detection, rejection reasons, and your
   real latency distribution (the dashboard's latency panel is the point).
2. **Paper** with your pre-registered threshold for 2–4+ weeks / several
   hundred evaluated trades.
3. Run `python -m sniper.evaluate`. Expect NO-GO. That is a successful
   experiment: you measured the edge (or its absence) for ~$0.
4. Only on GO: **shadow** to calibrate, re-evaluate, then (maybe) gated live.

---

## Monitoring

`data/sniper.db` (SQLite, WAL) holds every launch seen, every filter result
with its rejection reason, every trade with a full fee breakdown, latency
samples across the whole budget (`event_seen → filters_done → tx_built →
tx_sent → tx_landed` + slot delta), positions, calibration rows, and risk
events. The dashboard renders launches, rejections, the **full P&L
distribution** (median + left tail, not just win rate), latency percentiles
(p50/p95/p99), and outcomes with `could_not_sell` broken out separately.

The trade log is intentionally complete enough (timestamps, signatures,
amounts, fees per leg) to double as a tax record — swaps are taxable events in
most jurisdictions; confirm the specifics with a local professional.

## Security notes (non-negotiable)

- Private key: env/.env or encrypted keystore only; gitignored; never logged;
  never a seed phrase; burner wallet only.
- Dependencies: `solders`/`solana-py` and the official Jupiter API only. No
  closed-source "sniper" libraries — that ecosystem is itself a honeypot.
- Dashboard binds to `127.0.0.1` only and the config loader refuses anything
  else; all endpoints are GET-only over a read-only DB connection.

## Upgrade path

Detection currently uses public WebSocket subscriptions with reconnect +
silent-stall watchdogs. The documented upgrade for serious latency is a
gRPC/Geyser feed (e.g. Yellowstone) — the detector emits `LaunchEvent`s onto a
queue precisely so a Geyser-backed source can drop in without touching
anything downstream. Nothing here depends on it.

## Disclaimers

Educational/research software. Not financial, legal, or tax advice. Memecoin
sniping is an extreme-risk activity with likely negative expected value; use
only funds you can afford to lose entirely, in a jurisdiction where this is
legal for you.
