/*
 * Burnerific card: silo, stock and the pellet forecast at a glance.
 *
 *   type: custom:burnerific-card
 *   entity: sensor.nbe_v7std_52629_pellets_run_out   # any Burnerific entity of the boiler
 *
 * The card finds the boiler's other entities itself (same device, by translation key).
 */

const KEYS = {
  state: "sensor.state",
  silo: "sensor.silo",
  siloLevel: "sensor.silo_level",
  storage: "sensor.storage",
  stock: "sensor.stock",
  runout: "sensor.runout_date",
  orderBy: "sensor.order_by",
  order: "sensor.order_recommendation",
  restock: "sensor.restock_date",
  siloEmpty: "sensor.silo_empty_date",
  burned: "sensor.burned_today",
  expected: "sensor.forecast_today",
  orderNow: "binary_sensor.order_pellets",
  refill: "binary_sensor.refill_silo",
  ash: "binary_sensor.ash_due_now",
  clean: "binary_sensor.clean_due_now",
  sweep: "binary_sensor.sweep_due_now",
  service: "binary_sensor.service_due_now",
  alarm: "binary_sensor.alarm",
};

const TEXT = {
  en: { silo: "Silo", storage: "Storage", total: "Total", runout: "Pellets run out", likely: "likely", orderBy: "Order by",
        order: "Order", restock: "Restock", today: "Today", burned: "burned", expected: "expected", stock: "Pellets left",
        ash: "Empty ash", clean: "Clean boiler", sweep: "Chimney sweep", service: "Service", refill: "Fill silo",
        orderNow: "Order pellets", alarm: "Alarm", setup: "Pick any Burnerific entity in the card's 'entity' option." },
  da: { silo: "Silo", storage: "Lager", total: "I alt", runout: "Pillerne slipper op", likely: "sandsynligvis",
        orderBy: "Bestil senest", order: "Bestil", restock: "Genopfyldning", today: "I dag", burned: "brændt",
        expected: "forventet", stock: "Piller tilbage", ash: "Tøm aske", clean: "Rens kedel", sweep: "Skorstensfejer",
        service: "Service", refill: "Fyld silo", orderNow: "Bestil piller", alarm: "Alarm",
        setup: "Vælg en vilkårlig Burnerific-enhed i kortets 'entity'-indstilling." },
};

class BurnerificCard extends HTMLElement {
  setConfig(config) {
    if (!config || !config.entity) throw new Error("Set 'entity' to any Burnerific entity of your boiler");
    this._config = config;
  }

  static getStubConfig(hass) {
    const any = Object.values(hass.entities || {}).find((e) => e.platform === "burnerific");
    return { entity: any ? any.entity_id : "" };
  }

  getCardSize() {
    return 6;
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  _t(key) {
    const lang = (this._hass.locale && this._hass.locale.language) || this._hass.language || "en";
    return (TEXT[lang.slice(0, 2)] || TEXT.en)[key] || TEXT.en[key];
  }

  _entities() {
    const all = this._hass.entities || {};
    const anchor = all[this._config.entity];
    if (!anchor) return null;
    const map = {};
    for (const e of Object.values(all)) {
      if (e.device_id !== anchor.device_id || e.platform !== "burnerific" || !e.translation_key) continue;
      map[`${e.entity_id.split(".")[0]}.${e.translation_key}`] = e.entity_id;
    }
    const out = {};
    for (const [name, key] of Object.entries(KEYS)) out[name] = map[key] ? this._hass.states[map[key]] : undefined;
    out.device = anchor.device_id ? this._hass.devices[anchor.device_id] : undefined;
    return out;
  }

  _num(s) {
    const v = s ? parseFloat(s.state) : NaN;
    return Number.isFinite(v) ? v : null;
  }

  _fmtDate(s) {
    if (!s || !s.state || ["unknown", "unavailable"].includes(s.state)) return "–";
    return this._fmtIso(s.state);
  }

  _fmtIso(iso) {
    if (!iso) return "–";
    const d = new Date(`${iso}T12:00:00`);
    return d.toLocaleDateString(this._hass.locale ? this._hass.locale.language : undefined, { day: "numeric", month: "short" });
  }

  _kg(v) {
    return v === null ? "–" : `${Math.round(v).toLocaleString(this._hass.locale ? this._hass.locale.language : undefined)} kg`;
  }

  _gauge(pct) {
    const r = 34, c = 2 * Math.PI * r;
    const p = pct === null ? 0 : Math.max(0, Math.min(100, pct));
    const color = p < 15 ? "var(--error-color)" : p < 35 ? "var(--warning-color)" : "var(--success-color, var(--primary-color))";
    return `<svg viewBox="0 0 84 84" width="84" height="84" role="img" aria-label="${Math.round(p)}%">
      <circle cx="42" cy="42" r="${r}" fill="none" stroke="var(--divider-color)" stroke-width="9"/>
      <circle cx="42" cy="42" r="${r}" fill="none" stroke="${color}" stroke-width="9" stroke-linecap="round"
        stroke-dasharray="${(c * p) / 100} ${c}" transform="rotate(-90 42 42)"/>
      <text x="42" y="47" text-anchor="middle" font-size="17" font-weight="500" fill="var(--primary-text-color)">${pct === null ? "–" : Math.round(p) + "%"}</text>
    </svg>`;
  }

  _fan(runout, restock) {
    const bands = runout && runout.attributes && runout.attributes.bands;
    if (!bands || bands.length < 2) return "";
    const W = 320, H = 120, L = 34, B = 18, T = 6;
    const t0 = Date.parse(bands[0].date), t1 = Date.parse(bands[bands.length - 1].date);
    const ymax = Math.max(...bands.map((b) => b.p90), 1);
    const x = (iso) => L + ((Date.parse(iso) - t0) / Math.max(1, t1 - t0)) * (W - L - 4);
    const y = (v) => T + (1 - v / ymax) * (H - T - B);
    const area = bands.map((b) => `${x(b.date)},${y(b.p90)}`).join(" ") + " " +
      bands.slice().reverse().map((b) => `${x(b.date)},${y(b.p10)}`).join(" ");
    const line = bands.map((b) => `${x(b.date)},${y(b.p50)}`).join(" ");
    let mark = "";
    if (restock && restock.state && Date.parse(restock.state) <= t1 && Date.parse(restock.state) >= t0) {
      const rx = x(restock.state);
      const right = rx > W * 0.7;  // keep the label inside the chart
      mark = `<line x1="${rx}" x2="${rx}" y1="${T}" y2="${H - B}" stroke="var(--secondary-text-color)" stroke-dasharray="3 3"/>
        <text x="${right ? rx - 3 : rx + 3}" y="${T + 9}" font-size="10" text-anchor="${right ? "end" : "start"}"
          fill="var(--secondary-text-color)">${this._t("restock")}</text>`;
    }
    const ticks = [0, 0.5, 1].map((f) => {
      const v = ymax * f;
      return `<text x="${L - 4}" y="${y(v) + 3}" font-size="10" text-anchor="end" fill="var(--secondary-text-color)">${Math.round(v)}</text>`;
    }).join("");
    const mid = bands[Math.floor(bands.length / 2)];
    return `<div class="fan"><div class="label">${this._t("stock")} (kg)</div>
      <svg viewBox="0 0 ${W} ${H}" width="100%" role="img" aria-label="${this._t("stock")}">
        <line x1="${L}" x2="${W - 4}" y1="${H - B}" y2="${H - B}" stroke="var(--divider-color)"/>
        ${ticks}
        <polygon points="${area}" fill="var(--primary-color)" opacity="0.18"/>
        <polyline points="${line}" fill="none" stroke="var(--primary-color)" stroke-width="2" stroke-linejoin="round"/>
        ${mark}
        <text x="${L}" y="${H - 4}" font-size="10" fill="var(--secondary-text-color)">${this._fmtIso(bands[0].date)}</text>
        <text x="${x(mid.date)}" y="${H - 4}" font-size="10" text-anchor="middle" fill="var(--secondary-text-color)">${this._fmtIso(mid.date)}</text>
        <text x="${W - 4}" y="${H - 4}" font-size="10" text-anchor="end" fill="var(--secondary-text-color)">${this._fmtIso(bands[bands.length - 1].date)}</text>
      </svg></div>`;
  }

  _render() {
    if (!this._hass || !this._config) return;
    if (!this.shadowRoot) this.attachShadow({ mode: "open" });
    const e = this._entities();
    if (!e) {
      this.shadowRoot.innerHTML = `<ha-card><div class="pad">${this._t("setup")}</div></ha-card>`;
      return;
    }
    const name = (e.device && (e.device.name_by_user || e.device.name)) || "Burnerific";
    const state = e.state ? this._hass.formatEntityState(e.state) : "";
    const chips = ["alarm", "orderNow", "refill", "ash", "clean", "sweep", "service"]
      .filter((k) => e[k] && e[k].state === "on")
      .map((k) => `<span class="chip ${k === "alarm" ? "bad" : ""}">${this._t(k)}</span>`)
      .join("");
    const r = e.runout && e.runout.attributes ? e.runout.attributes : {};
    const orderKg = this._num(e.order);
    this.shadowRoot.innerHTML = `
      <style>
        ha-card { padding: 16px; }
        .head { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; }
        .name { font-size: 18px; font-weight: 500; }
        .state { color: var(--secondary-text-color); font-size: 14px; }
        .row { display: flex; align-items: center; gap: 16px; margin-top: 12px; }
        .stats { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; flex: 1; }
        .label { color: var(--secondary-text-color); font-size: 12px; }
        .value { font-size: 16px; font-weight: 500; }
        .runout { margin-top: 16px; padding-top: 12px; border-top: 1px solid var(--divider-color); display: grid;
                  grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }
        .big { font-size: 22px; font-weight: 500; }
        .fan { margin-top: 12px; }
        .chips { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 12px; }
        .chip { background: var(--warning-color); color: var(--text-primary-color, #fff); border-radius: 12px;
                padding: 2px 10px; font-size: 12px; }
        .chip.bad { background: var(--error-color); }
        .pad { padding: 8px; }
      </style>
      <ha-card>
        <div class="head"><span class="name">${name}</span><span class="state">${state}</span></div>
        <div class="row">
          ${this._gauge(this._num(e.siloLevel))}
          <div class="stats">
            <div><div class="label">${this._t("silo")}</div><div class="value">${this._kg(this._num(e.silo))}</div></div>
            <div><div class="label">${this._t("storage")}</div><div class="value">${this._kg(this._num(e.storage))}</div></div>
            <div><div class="label">${this._t("total")}</div><div class="value">${this._kg(this._num(e.stock))}</div></div>
            <div><div class="label">${this._t("today")}</div><div class="value">${this._kg(this._num(e.burned))}</div></div>
            <div><div class="label">${this._t("expected")}</div><div class="value">${this._kg(this._num(e.expected))}</div></div>
            <div><div class="label">${this._t("refill")}</div><div class="value">${this._fmtDate(e.siloEmpty)}</div></div>
          </div>
        </div>
        <div class="runout">
          <div><div class="label">${this._t("runout")}</div><div class="big">${this._fmtDate(e.runout)}</div>
            <div class="label">${this._fmtIso(r.runout_p10)} – ${this._fmtIso(r.runout_p90)}</div></div>
          <div><div class="label">${this._t("orderBy")}</div><div class="big">${this._fmtDate(e.orderBy)}</div>
            <div class="label">${this._t("order")} ${this._kg(orderKg)} · ${this._t("restock")} ${this._fmtDate(e.restock)}</div></div>
        </div>
        ${this._fan(e.runout, e.restock)}
        ${chips ? `<div class="chips">${chips}</div>` : ""}
      </ha-card>`;
  }
}

if (!customElements.get("burnerific-card")) {
  customElements.define("burnerific-card", BurnerificCard);
  window.customCards = window.customCards || [];
  window.customCards.push({
    type: "burnerific-card",
    name: "Burnerific",
    description: "Silo, stock and the pellet forecast for an NBE boiler.",
    preview: true,
  });
}
