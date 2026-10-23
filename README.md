<img src="icon.png" alt="" width="96" align="right">

# burnerific

A, nah, **THE** Home Assistant integration for NBE pellet boilers.

It talks to the controller directly over the local NBE UDP protocol: no extra Python packages, and StokerCloud only as a fallback if you want it. Then it does what no other NBE integration does. It tracks your pellets, learns your house, and tells you when to fill the silo, when to order, when to clean, and what your heat costs.

## What you get

**The boiler**
- A **thermostat** (`climate`) for the boiler: setpoint, on/off, and whether it is heating or idle. With a hot water tank, a **water heater** too.
- State and substate in your language, alarm, flame, ignition, pumps, ignition counter
- Temperatures (boiler, return, flue gas, shaft, hot water, outdoor), power in % and kW, fan speeds. Oxygen sensors are only enabled on boilers with a lambda sensor.
- **Alarms become Repairs issues with a hint.** An ignition alarm with an empty silo says "probably out of pellets", not just "alarm 13".
- The controller's **event log** in Home Assistant's logbook ("changed from burning to stopped by timer")
- Consumption today and total, hot water today, and a fuel energy sensor (kWh) for the Energy dashboard
- With the PIN: start, stop, reset alarm, and `set_setting` for anything else. Writes use the controller's own RSA scheme, implemented in plain Python. A changed PIN triggers re-authentication.
- **StokerCloud fallback**: read the boiler through the cloud while the LAN is down (optional)

**The pellets**
- **Storage** (bags, pallets, the shed) and **silo** (the hopper by the boiler) in kg and bags
- **Deliveries** become lots with supplier and price. Pellets move first-in first-out, so Burnerific knows which lot is burning.
- **Fill silo** takes pellets out of storage and, with a PIN, updates the controller's silo count
- The silo follows the controller's count, Burnerific's own count, or **a level sensor** (percent, or a distance sensor above the pellets)
- **Import your fill log**: paste lines like `04-10-2025 6` (date, sacks) and the model learns from your past winters right away
- **Counter calibration**: compares what you filled with what the controller counted, and can correct its counter

**The forecast**
- **Pellets run out** and **Silo empty** dates, as P10 / P50 / P90, plus a fan chart of the remaining stock
- **Order by**: the last day to order given your delivery time. **Order to last until restock**: how much, with 90 % confidence.
- **Next restock date**: when you plan to buy again (for example when new-season prices start)
- Expected burn today, next 7 and next 30 days
- A **calendar** with all of it: silo empty, order by, run-out, restock and maintenance

**Maintenance**
- **Ash drawer** fill level, predicted from kg burned and the pellets' ash content
- **Heat exchanger cleaning** and **burner service** by kg burned, **chimney sweep** by date, each with a due date from the burn forecast
- Buttons to mark each one done

**Money and efficiency**
- **Cost** today, this month, this season, and expected for the next 30 days
- **Pellet price per kWh** (for the Energy dashboard), and heat price compared with a **heat pump**
- **Supplier quality**: does lot A burn more pellets per degree-day than lot B?
- **Weather-normalised yearly consumption** per season, so you can see whether new insulation paid off

**In your dashboard**
- A **Lovelace card** (`custom:burnerific-card`): silo gauge, stock, run-out date and range, order-by, fan chart and maintenance chips
- **Blueprints**, installed to `blueprints/automation/burnerific/`: fill-silo reminder, order pellets, and boiler alarm with hint
- English, Danish, Swedish, Norwegian, German and Polish. Swedish, Norwegian, German and Polish are first drafts; corrections welcome.

## The math

**1. A model of your house.** Daily pellet use is modelled with degree-days, wind and sun:

```
kg/day = base_load
       + k   · HDD
       + k_w · HDD · (wind − normal wind) / 5 m/s
       − k_s · sunshine · (fades in over the first 3 degree-days)
HDD    = max(0, T_limit − T_eff)
```

`base_load` is hot water and standby losses. `k` is kg per degree-day: how big and leaky the house is. `k_w` is how much more a cold windy day costs than a cold calm one, and `k_s` is what sunshine through the windows saves. `T_limit` is the heating limit, the outdoor temperature above which the house needs no heat. `T_eff` is the outdoor temperature smoothed over about a day, because the house has thermal mass. If the controller reports hot water separately, that part is modelled on its own.

The model learns from days (the controller's daily figures and Burnerific's own counting) and from periods: the controller's monthly totals and the gaps between logged silo fills, each paired with the measured weather of every day in it.

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
| `burnerific.add_delivery` | `kg` and/or `bags`, optional `price_per_kg` or `total_price`, `supplier`, `note` |
| `burnerific.fill_silo` | `kg` and/or `bags` (empty = whole bags up to silo capacity), `from_storage` |
| `burnerific.set_storage` | Correct storage after counting |
| `burnerific.set_silo` | Correct the silo content (written to the controller when it counts the silo) |
| `burnerific.set_setting` | `key` like `boiler.temp`, `value`. Needs the PIN |
| `burnerific.import_fills` | `log`: one fill per line (`04-10-2025 6`), `unit`: `bags` or `kg`. Returns what it learned |
| `burnerific.maintenance_done` | `task`: `ash`, `clean`, `sweep` or `service`, optional `date` |
| `burnerific.get_forecast` | Returns everything: quantiles, curve, model, maintenance, lots, seasons, costs, calibration |

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
