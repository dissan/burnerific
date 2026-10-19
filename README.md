<img src="icon.png" alt="" width="96" align="right">

# burnerific

A, nah, **THE** Home Assistant integration for NBE pellet boilers.

It talks to the controller directly over the local NBE UDP protocol: no cloud, no StokerCloud, no extra Python packages. Then it does what no other NBE integration does. It tracks your pellets and works out when you will run out, with a forecast that knows about the weather.

## What you get

**The boiler**
- State and substate in your language (English and Danish), alarm, flame, ignition, pumps
- Temperatures (boiler, return, flue gas, shaft, hot water, outdoor), power in % and kW, oxygen, fan speeds
- Consumption today and total, plus a fuel energy sensor (kWh) for the Energy dashboard
- With the PIN: boiler and hot water setpoints, start, stop, reset alarm, and a `set_setting` service for anything else. Writes use the controller's own RSA scheme, implemented in plain Python.

**The pellets**
- **Storage** (bags, pallets, the shed) and **silo** (the hopper by the boiler), each in kg, plus bags and total stock
- **Deliveries** go into storage, with a weighted average price and the value of your stock
- **Fill silo** takes pellets out of storage and, with a PIN, updates the controller's own silo count
- The silo follows the controller's count when it has one. Otherwise Burnerific tracks it from the burn counter.

**The forecast**
- **Pellets run out** and **Silo empty** dates, as P10 / P50 / P90
- **Order to last the season**: how much to order so that you reach the end of the heating season with 90 % confidence
- **Risk of running out this season**, expected burn today, next 7 and next 30 days (daily curve in the attributes)
- `Order pellets` and `Refill silo` binary sensors to hang automations on

## The math

**1. A model of your house.** Daily pellet use is modelled with degree-days:

```
kg/day = base_load + k · max(0, T_limit − T_eff)
```

`base_load` is hot water and standby losses. `k` is kg per degree-day: how big and leaky the house is. `T_limit` is the heating limit, the outdoor temperature above which the house needs no heat. `T_eff` is the outdoor temperature smoothed over about a day, because the house has thermal mass.

The fit is Bayesian (Normal-Inverse-Gamma conjugate), so:
- On day one it works from your rough yearly consumption.
- It sharpens as real days come in.
- Old days fade out (half-life 120 days), so it follows changes in how you live.

The heating limit isn't linear in the model. Burnerific fits a grid of limits and weights each one by its marginal likelihood. The forecast then samples across all of them (Bayesian model averaging), so uncertainty about the heating limit shows up in the run-out date instead of being hidden. One robust pass down-weights days that make no sense, such as the boiler being off for cleaning.

**2. A climate for your location.** Burnerific fits six years of daily temperatures for your home (Open-Meteo archive) as:
- a two-harmonic seasonal curve
- a seasonal spread (winters vary more than summers)
- an AR(1) persistence of weather anomalies

If Open-Meteo is off or unreachable, it falls back to a temperate default, mirrored for the southern hemisphere.

**3. Monte Carlo into the future.** Each of 400 simulated futures draws a house model from the posterior, then walks forward day by day:
- While there is a forecast, the temperature is your weather entity's forecast (then Open-Meteo's 16-day one) plus an error that grows with lead time.
- After that, the last anomaly fades into the seasonal climate.
- Each day burns pellets with realistic noise.

The quantiles of when the silo and the stock run dry are the dates you see. The seed is fixed per day, so the numbers don't jitter between refreshes.

**Learning from day one.** The controller keeps its own daily consumption for the last month and monthly totals for the last year. Burnerific backfills those, paired with measured temperatures from Open-Meteo, so the model starts with weeks of real data instead of nothing.

## Install

HACS → Custom repositories → `https://github.com/dissan/burnerific` (Integration), install, restart. Or copy `custom_components/burnerific` into your config folder.

Then go to **Settings → Devices & services → Add integration → Burnerific**:
1. Burnerific broadcasts on the LAN and lists the boilers that answer. If none answers, enter the IP address.
2. The PIN is optional. It is the code on the sticker on the controller, and it is only needed to change settings.
3. Tell it what is in storage, the silo capacity, bag size and a rough yearly consumption. Pick a weather entity if you have one.

The options let you choose an outdoor temperature sensor, who counts the silo, delivery lead time (for `Order pellets`), when the heating season ends (`MM-DD`, default `06-01`), and kWh per kg.

## Services

| Service | What it does |
|---|---|
| `burnerific.add_delivery` | `kg` and/or `bags`, optional `price_per_kg` or `total_price`, `note` |
| `burnerific.fill_silo` | `kg` and/or `bags` (empty = whole bags up to silo capacity), `from_storage` |
| `burnerific.set_storage` | Correct storage after counting |
| `burnerific.set_silo` | Correct the silo content (written to the controller when it counts the silo) |
| `burnerific.set_setting` | `key` like `boiler.temp`, `value`. Needs the PIN |
| `burnerific.get_forecast` | Returns everything: quantiles, daily curve, model parameters, climate |

```yaml
# The pallet arrived
action: burnerific.add_delivery
data:
  bags: 65
  total_price: 2780
```

## Privacy

Everything runs locally. With Open-Meteo on (the default), your home location rounded to about 1 km is sent to open-meteo.com to fetch climate history and a forecast. Turn it off in the options.

## Development

Open the repo in VS Code and choose **Reopen in Container**. The devcontainer has Python 3.14, Home Assistant and its test harness.

| Task (Terminal → Run Task) | Script |
|---|---|
| Run Home Assistant | `scripts/develop`: HA at http://localhost:8123 with Burnerific loaded from this checkout |
| Fake boiler | `python scripts/fake_boiler.py --speed 30`: a simulated NBE controller (PIN `0123456789`) |
| Home Assistant + fake boiler | both at once. Add the integration and it finds the fake boiler by broadcast |
| Tests | `scripts/test` |

The fake boiler speaks the real protocol, including encrypted writes. The house heats with the time of day, the boiler cycles, and the burn counter, silo and day/month arrays move. `--speed` burns pellets faster so you can watch the stock drop.

A real boiler on your LAN: Docker Desktop doesn't pass broadcasts through, so choose manual entry and type the boiler's IP. Replies come back through NAT. `ip:port` works too.

Code layout:
- `custom_components/burnerific/nbe/` is the protocol: frames, RSA, async UDP client and normalisation.
- `custom_components/burnerific/pellets/` is the stock ledger, history, climate, consumption model and forecast.

Neither imports Home Assistant, so `pytest` on those runs anywhere, with just `pytest` and `pytest-asyncio` installed. `tests/ha/` runs the integration in a real Home Assistant test instance against a fake controller on localhost. It is skipped where `pytest-homeassistant-custom-component` isn't installed.

The protocol was worked out with [NBElocalconnect](https://github.com/Spit68/NBElocalconnect) as a reference. The implementation here is our own.
