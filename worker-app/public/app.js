import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

/* State */
const state = {
  config: null,
  supabase: null,
  session: null,
  me: null,
  health: null,
  teaser: null,
  dashboardWindow: "1d",
  dashboardDirection: "under",
  dashboardIncludeFiltered: false,
  prefillExtraFields: {},
  lastPrefilledUrl: "",
  mobileMenuOpen: false,
  authSubmitting: false,
  authRateLimitedUntil: 0
};

/* Helpers */
function formatCurrency(value) {
  return new Intl.NumberFormat("cs-CZ", { style: "currency", currency: "CZK", maximumFractionDigits: 0 }).format(value);
}

function formatPercent(value) {
  return new Intl.NumberFormat("cs-CZ", { style: "percent", maximumFractionDigits: 1, minimumFractionDigits: 1 }).format(value);
}

function formatDateTime(value) {
  if (!value) return "Neznámé";
  return new Intl.DateTimeFormat("cs-CZ", { dateStyle: "medium", timeStyle: "short" }).format(new Date(value));
}

function humanizeStaleReason(reason, degradedSources = []) {
  const sourceCopy = degradedSources.length ? ` (${degradedSources.join(", ")})` : "";
  const map = {
    missing_publish_run: "Dashboard ještě nemá první úspěšný publish.",
    publish_older_than_36h: "Data na webu jsou starší než 36 hodin.",
    source_guardrail_failed: `Poslední scrape doběhl degradovaně${sourceCopy}.`,
    db_unavailable: "Datová vrstva není momentálně dostupná."
  };
  return map[reason] || "Stav čerstvosti dat není potvrzený.";
}

function renderFreshnessBanner(freshness, label = "Data") {
  if (!freshness) return "";
  const updatedAt = freshness.generatedAt ? formatDateTime(freshness.generatedAt) : "neznámý čas";
  const degradedSources = Array.isArray(freshness.degradedSources) ? freshness.degradedSources : [];
  const reason = freshness.isStale || freshness.isDegraded
    ? humanizeStaleReason(freshness.staleReason, degradedSources)
    : `${label} aktualizovaná ${updatedAt}.`;
  const tone = freshness.isStale ? "error" : freshness.isDegraded ? "info" : "success";
  return `<div class="status-msg ${tone}" style="margin:0 0 16px">${reason}${freshness.generatedAt ? ` Aktualizováno ${updatedAt}.` : ""}</div>`;
}

function renderModelBanner() {
  if (!state.health?.modelStale) return "";
  const updatedAt = state.health?.lastSuccessfulTrainAt || state.health?.lastModelPromotionTime;
  return `<div class="status-msg info" style="margin:0 0 16px">Model nebyl v posledních 36 hodinách potvrzeně přetrénovaný. Poslední známý běh: ${formatDateTime(updatedAt)}.</div>`;
}

function humanizeFeature(name) {
  const m = {
    district_prague: "Lokalita", property_type: "Typ", disposition: "Dispozice",
    floor_area_m2: "Plocha", distance_to_center_km: "Vzdálenost od centra",
    condition: "Stav", ownership: "Vlastnictví", construction: "Konstrukce",
    energy_label: "Energie", has_elevator: "Výtah", has_parking: "Parkování"
  };
  return m[name] ?? name;
}

function humanizeWarning(flag) {
  const map = {
    geocode_fallback: "náhradní geolokace",
    too_few_comparables: "málo srovnatelných nabídek",
    limited_comparables: "omezené srovnání",
    many_missing_inputs: "hodně chybějících údajů",
    some_missing_inputs: "část údajů chybí",
    very_wide_prediction_interval: "široké tržní pásmo",
    wide_prediction_interval: "vyšší nejistota intervalu",
    extreme_local_ppm_gap: "extrém vůči lokálnímu ppm",
    missing_coordinates: "chybějící souřadnice",
    low_listing_quality_score: "nízká kvalita vstupu"
  };
  return map[flag] ?? flag;
}

function normalizeUrl(v) {
  const t = v.trim();
  return !t ? "" : /^https?:\/\//i.test(t) ? t : `https://${t}`;
}

function authCooldownRemainingMs() {
  return Math.max(0, state.authRateLimitedUntil - Date.now());
}

function authCooldownRemainingSeconds() {
  return Math.ceil(authCooldownRemainingMs() / 1000);
}

function updateAuthSubmitButton(button) {
  if (!button) return;

  const remainingSeconds = authCooldownRemainingSeconds();
  button.disabled = state.authSubmitting || remainingSeconds > 0;

  if (state.authSubmitting) {
    button.textContent = "Odesílám...";
    return;
  }

  if (remainingSeconds > 0) {
    button.textContent = `Zkusit znovu za ${remainingSeconds}s`;
    return;
  }

  button.textContent = "Poslat magic link";
}

function humanizeAuthError(error) {
  const message = error?.message?.toLowerCase?.() ?? "";

  if (message.includes("email rate limit exceeded") || message.includes("rate limit")) {
    return "Přihlašovací odkaz už byl odeslaný příliš často. Počkej asi minutu a zkus to znovu.";
  }

  if (message.includes("load failed") || message.includes("fetch failed") || message.includes("network")) {
    return "Nepodařilo se spojit s přihlašovací službou. Zkontroluj konfiguraci Supabase a zkus to znovu.";
  }

  if (error?.message) {
    return `Chyba: ${error.message}`;
  }

  return "Přihlášení se nepodařilo. Zkus to prosím znovu.";
}

async function apiFetch(path, init = {}) {
  const headers = new Headers(init.headers || {});
  headers.set("content-type", headers.get("content-type") || "application/json");
  if (state.session?.access_token) headers.set("authorization", `Bearer ${state.session.access_token}`);
  return fetch(path, { ...init, headers });
}

/* Router */
function navigate(path, replace = false) {
  if (replace) window.history.replaceState({}, "", path);
  else window.history.pushState({}, "", path);
  render();
}

function currentPath() {
  return window.location.pathname;
}

window.addEventListener("popstate", () => render());

/* Page: Landing */
function renderLanding() {
  const teaserFreshness = state.teaser?.freshness;
  const navAuthHtml = state.session
    ? `<a href="/app/prehled" data-link class="btn btn-primary btn-sm">Dashboard</a>`
    : `
          <a href="/login" data-link>Přihlásit se</a>
          <a href="/login" data-link class="btn btn-primary btn-sm">Začít zdarma</a>
        `;

  const teaserHtml = state.teaser?.summary?.length && !(teaserFreshness?.isStale || teaserFreshness?.isDegraded) ? state.teaser.summary.map(item => {
    const labels = { "1d": "Den", "7d": "Týden", "30d": "Měsíc" };
    return `<div class="landing-stat">
      <div class="landing-stat-value">${item.underCount + item.overCount}</div>
      <div class="landing-stat-label">Výkyvů za ${labels[item.window]?.toLowerCase()}</div>
    </div>`;
  }).join("") : `
    <div class="landing-stat"><div class="landing-stat-value">3</div><div class="landing-stat-label">Predikce zdarma</div></div>
    <div class="landing-stat"><div class="landing-stat-value">3</div><div class="landing-stat-label">Zdroje dat</div></div>
    <div class="landing-stat"><div class="landing-stat-value">ML</div><div class="landing-stat-label">Model predikce</div></div>
  `;

  return `
    <div class="landing">
      <nav class="landing-nav">
        <div class="landing-logo">Valuo</div>
        <div class="landing-nav-links">
          <a href="#features">Funkce</a>
          ${navAuthHtml}
        </div>
      </nav>

      <div class="landing-hero">
        <div class="landing-hero-inner">
          <h1 class="t-display">Kolik reálně stojí nemovitost v Praze?</h1>
          <p class="t-body">
            ML model trénovaný na tisících reálných nabídek odhadne cenu, ukáže tržní pásmo
            a průběžně hlídá extrémně levné i drahé nabídky.
          </p>
          <div class="landing-cta-row">
            <a href="/login" data-link class="btn btn-primary btn-lg">Začít zdarma</a>
            <a href="#features" class="btn btn-secondary btn-lg">Jak to funguje</a>
          </div>
          ${renderFreshnessBanner(teaserFreshness, "Tržní feed")}
          ${renderModelBanner()}
          <div class="landing-stats">${teaserHtml}</div>
        </div>
      </div>

      <section id="features" class="landing-features">
        <div class="landing-features-inner">
          <h2 class="t-h1" style="text-align:center">Dvě situace, jeden model</h2>
          <p class="t-body t-secondary" style="text-align:center;max-width:520px;margin:12px auto 0">
            Ať potřebuješ nacenit vlastní nemovitost, nebo prověřit konkrétní nabídku.
          </p>
          <div class="landing-features-grid">
            <div class="landing-feature">
              <div class="landing-feature-icon purple">&#8364;</div>
              <h3 class="t-h3">Nacenění nemovitosti</h3>
              <p>Odhad typické inzerované ceny, běžné pásmo trhu a faktory, které cenu ovlivňují.</p>
            </div>
            <div class="landing-feature">
              <div class="landing-feature-icon green">&#128269;</div>
              <h3 class="t-h3">Insight do ceny</h3>
              <p>Okamžité vyhodnocení, zda je konkrétní nabídka pod trhem, v pásmu, nebo nad trhem.</p>
            </div>
            <div class="landing-feature">
              <div class="landing-feature-icon orange">&#9889;</div>
              <h3 class="t-h3">Premium dashboard</h3>
              <p>Automatický monitoring nových nabídek s výraznými cenovými odchylkami. Den, týden, měsíc.</p>
            </div>
          </div>
        </div>
      </section>

      <footer class="landing-footer">Valuo &middot; Praha a okolí &middot; Data z Bezrealitky, RealityMix a RE/MAX</footer>
    </div>
  `;
}

/* Page: Login */
function renderLogin() {
  const authStatusHtml = state.config?.auth?.message
    ? `<div class="status-msg error" style="margin:0 0 16px">${state.config.auth.message}</div>`
    : "";

  return `
    <div class="login-page">
      <div class="login-card">
        <div class="login-logo">Valuo</div>
        <h1 class="t-h1">Přihlášení</h1>
        <p class="t-body">Zadej svůj email a pošleme ti přihlašovací odkaz. Bez hesla.</p>
        ${authStatusHtml}
        <form id="auth-form" class="login-form">
          <div class="field">
            <label class="field-label" for="auth-email">Email</label>
            <input id="auth-email" name="email" type="email" class="field-input" placeholder="napr. investor@firma.cz" required />
          </div>
          <div id="auth-status"></div>
          <button id="auth-submit" class="btn btn-primary btn-full btn-lg" type="submit">Poslat magic link</button>
        </form>
        <p class="login-footnote">
          <a href="/" data-link>&larr; Zpět na úvod</a>
        </p>
      </div>
    </div>
  `;
}

function bindLogin() {
  const form = document.getElementById("auth-form");
  const emailEl = document.getElementById("auth-email");
  const statusEl = document.getElementById("auth-status");
  const submitBtn = document.getElementById("auth-submit");
  if (!form) return;

  updateAuthSubmitButton(submitBtn);

  const cooldownTimer = window.setInterval(() => {
    if (!document.body.contains(form)) {
      window.clearInterval(cooldownTimer);
      return;
    }
    updateAuthSubmitButton(submitBtn);
  }, 1000);

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const email = emailEl.value.trim();
    if (!email) { showStatus(statusEl, "Zadej email.", "error"); return; }
    if (!state.supabase) {
      showStatus(statusEl, state.config?.auth?.message || "Auth ještě není připravený.", "error");
      return;
    }
    if (authCooldownRemainingMs() > 0) {
      showStatus(statusEl, `Další přihlašovací odkaz můžeš poslat za ${authCooldownRemainingSeconds()} s.`, "info");
      updateAuthSubmitButton(submitBtn);
      return;
    }

    state.authSubmitting = true;
    updateAuthSubmitButton(submitBtn);

    try {
      const redirectUrl = new URL(`${state.config.appBaseUrl}/auth/callback`);
      const { error } = await state.supabase.auth.signInWithOtp({ email, options: { emailRedirectTo: redirectUrl.toString() } });
      if (error) {
        if ((error.message || "").toLowerCase().includes("rate limit")) {
          state.authRateLimitedUntil = Date.now() + 60_000;
        }
        showStatus(statusEl, humanizeAuthError(error), "error");
        return;
      }

      state.authRateLimitedUntil = Date.now() + 60_000;
      showStatus(statusEl, `Magic link jsme poslali na ${email}. Klikni na odkaz v emailu. Další odeslání bude možné přibližně za minutu.`, "success");
    } finally {
      state.authSubmitting = false;
      updateAuthSubmitButton(submitBtn);
    }
  });
}

function showStatus(el, msg, type = "info") {
  if (!el) return;
  el.className = `status-msg ${type}`;
  el.textContent = msg;
}

/* Layout: App Shell (sidebar + content) */
function renderAppShell(pageContent, activePage) {
  const email = state.me?.user?.email || state.session?.user?.email || "";
  const initial = email ? email[0].toUpperCase() : "?";
  const isPremium = state.me?.usage?.premium;
  const usage = state.me?.usage;

  let quotaHtml = "";
  if (usage && !isPremium) {
    const pct = Math.min(100, Math.round((usage.freeUsed / usage.freeLimit) * 100));
    quotaHtml = `
      <div style="padding:12px 12px 0">
        <div class="quota-bar-wrap">
          <div class="quota-bar"><div class="quota-bar-fill" style="width:${pct}%"></div></div>
          <span class="quota-label">${usage.freeUsed}/${usage.freeLimit}</span>
        </div>
      </div>`;
  }

  return `
    <div class="app-layout">
      <div class="mobile-header">
        <span style="font-weight:700">Valuo</span>
        <button class="mobile-toggle" id="mobile-toggle">&#9776;</button>
      </div>
      <div id="mobile-overlay" class="mobile-overlay" style="display:none"></div>
      <aside class="sidebar" id="sidebar">
        <div class="sidebar-logo">Valuo</div>
        <div class="sidebar-top-link">
          <button class="sidebar-link sidebar-link-home" data-nav="/">
            <span class="sidebar-link-icon">&#8592;</span> Zpět na domovskou stránku
          </button>
        </div>
        <nav class="sidebar-nav">
          <div class="sidebar-section-label">Nástroje</div>
          <button class="sidebar-link ${activePage === "naceneni" ? "active" : ""}" data-nav="/app/naceneni">
            <span class="sidebar-link-icon">&#8364;</span> Nacenění
          </button>
          <button class="sidebar-link ${activePage === "insight" ? "active" : ""}" data-nav="/app/insight">
            <span class="sidebar-link-icon">&#128269;</span> Insight do ceny
          </button>
          <div class="sidebar-section-label">Premium</div>
          <button class="sidebar-link ${activePage === "prehled" ? "active" : ""}" data-nav="/app/prehled">
            <span class="sidebar-link-icon">&#9889;</span> Dashboard výkyvů
          </button>
        </nav>
        ${quotaHtml}
        <div class="sidebar-bottom">
          ${isPremium ? '<div style="padding:4px 12px"><span class="badge badge-accent">Premium</span></div>' : `<button class="sidebar-link" id="sidebar-upgrade"><span class="sidebar-link-icon">&#11088;</span> Upgradovat na premium</button>`}
          <button class="sidebar-user sidebar-account-link ${activePage === "ucet" ? "active" : ""}" data-nav="/app/ucet" type="button">
            <div class="sidebar-avatar">${initial}</div>
            <span style="overflow:hidden;text-overflow:ellipsis">${email}</span>
          </button>
          <button class="sidebar-link" id="sidebar-logout">
            <span class="sidebar-link-icon">&#8594;</span> Odhlásit se
          </button>
        </div>
      </aside>
      <div class="main-content">${pageContent}</div>
    </div>
  `;
}

function bindAppShell() {
  document.querySelectorAll("[data-nav]").forEach(btn => {
    btn.addEventListener("click", () => {
      closeMobileMenu();
      navigate(btn.dataset.nav);
    });
  });

  document.getElementById("sidebar-logout")?.addEventListener("click", async () => {
    await state.supabase?.auth.signOut();
    state.me = null;
    state.session = null;
    navigate("/login", true);
  });

  document.getElementById("sidebar-upgrade")?.addEventListener("click", startCheckout);

  document.getElementById("mobile-toggle")?.addEventListener("click", () => {
    state.mobileMenuOpen = !state.mobileMenuOpen;
    document.getElementById("sidebar")?.classList.toggle("open", state.mobileMenuOpen);
    const overlay = document.getElementById("mobile-overlay");
    if (overlay) overlay.style.display = state.mobileMenuOpen ? "block" : "none";
  });

  document.getElementById("mobile-overlay")?.addEventListener("click", closeMobileMenu);
}

function closeMobileMenu() {
  state.mobileMenuOpen = false;
  document.getElementById("sidebar")?.classList.remove("open");
  const overlay = document.getElementById("mobile-overlay");
  if (overlay) overlay.style.display = "none";
}

/* Page: Nacenění / Insight (shared form) */
const formMeta = {
  naceneni: {
    title: "Nacenění nemovitosti",
    subtitle: "Odhad typické ceny, tržní pásmo a faktory ovlivňující cenu.",
    showInputPrice: false,
    submitLabel: "Spočítat odhad",
    experienceMode: "pricing",
    enablePrefill: false
  },
  insight: {
    title: "Insight do ceny",
    subtitle: "Vyhodnocení, zda je nabídka pod trhem, v pásmu, nebo nad trhem.",
    priceLabel: "Cena inzerátu Kč",
    submitLabel: "Vyhodnotit cenu",
    experienceMode: "insight",
    showInputPrice: true,
    enablePrefill: true
  }
};

function renderFormPage(mode) {
  const meta = formMeta[mode];
  return `
    <div class="page-header">
      <div class="page-header-left">
        <h1>${meta.title}</h1>
        <p>${meta.subtitle}</p>
      </div>
    </div>
    <div class="page-body">
      <div class="form-page-grid">
        <div class="card">
          <form id="prediction-form" class="prediction-form">
            ${meta.enablePrefill ? `
            <div class="prefill-row">
              <div class="field">
                <label class="field-label">Link na inzerát</label>
                <input id="listing-url" name="listingUrl" type="url" class="field-input" placeholder="https://www.bezrealitky.cz/..." />
              </div>
              <button id="prefill-btn" class="btn btn-secondary" type="button" style="margin-bottom:1px">Načíst</button>
            </div>
            <div id="prefill-status"></div>` : ""}

            <div class="field">
              <label class="field-label">Adresa</label>
              <input name="address" type="text" class="field-input" placeholder="Např. Poděbradská 777/9" required />
            </div>

            <div class="form-row form-row-2">
              <div class="field">
                <label class="field-label">Lokalita</label>
                <input name="districtPrague" type="text" class="field-input" placeholder="Např. Vysočany" required list="districts" />
                <datalist id="districts">
                  <option>Vysočany</option><option>Žižkov</option><option>Smíchov</option>
                  <option>Vinohrady</option><option>Holešovice</option><option>Stodůlky</option>
                  <option>Karlín</option><option>Modřany</option><option>Letňany</option>
                  <option>Libeň</option><option>Praha okolí</option><option>Říčany</option>
                  <option>Černošice</option><option>Kladno</option>
                </datalist>
              </div>
              <div class="field">
                <label class="field-label">Typ nemovitosti</label>
                <select name="propertyType" class="field-input">
                  <option value="flat">Byt</option>
                  <option value="house">Dům</option>
                </select>
              </div>
            </div>

            <div class="form-row ${meta.showInputPrice ? "form-row-3" : "form-row-2"}">
              <div class="field">
                <label class="field-label">Dispozice</label>
                <input name="disposition" type="text" class="field-input" placeholder="Např. 2+kk" value="2+kk" />
              </div>
              <div class="field">
                <label class="field-label">Plocha m²</label>
                <input name="floorAreaM2" type="number" min="10" step="0.1" value="55" class="field-input" required />
              </div>
              ${meta.showInputPrice ? `<div class="field">
                <label class="field-label">${meta.priceLabel}</label>
                <input name="askingPriceCzk" type="number" min="1" step="1000" class="field-input" placeholder="Volitelné" />
              </div>` : ""}
            </div>

            <div class="form-row form-row-3">
              <div class="field">
                <label class="field-label">Stav</label>
                <select name="condition" class="field-input">
                  <option value="unknown">Nezadáno</option>
                  <option value="new">Novostavba</option>
                  <option value="very_good">Po rekonstrukci</option>
                  <option value="good">Dobrý</option>
                  <option value="before_reconstruction">Před rekonstrukcí</option>
                </select>
              </div>
              <div class="field">
                <label class="field-label">Vlastnictví</label>
                <select name="ownership" class="field-input">
                  <option value="unknown">Nezadáno</option>
                  <option value="osobni">Osobní</option>
                  <option value="druzstevni">Družstevní</option>
                </select>
              </div>
              <div class="field">
                <label class="field-label">Konstrukce</label>
                <select name="construction" class="field-input">
                  <option value="unknown">Nezadáno</option>
                  <option value="brick">Cihla</option>
                  <option value="panel">Panel</option>
                  <option value="mixed">Smíšená</option>
                </select>
              </div>
            </div>

            <div class="form-row form-row-3">
              <div class="field">
                <label class="field-label">Patro</label>
                <input name="floorNo" type="number" step="1" class="field-input" placeholder="Volitelné" />
              </div>
              <div class="field">
                <label class="field-label">Energetická třída</label>
                <select name="energyLabel" class="field-input">
                  <option value="unknown">Nezadáno</option>
                  <option value="a">A</option><option value="b">B</option><option value="c">C</option>
                  <option value="d">D</option><option value="e">E</option><option value="f">F</option>
                  <option value="g">G</option>
                </select>
              </div>
              <div class="field">
                <label class="field-label">Výbava</label>
                <div style="display:flex;flex-direction:column;gap:8px;padding-top:4px">
                  <label class="checkbox-row"><input name="hasElevator" type="checkbox" /> Výtah</label>
                  <label class="checkbox-row"><input name="hasParking" type="checkbox" /> Parkování</label>
                </div>
              </div>
            </div>

            <button id="predict-submit" class="btn btn-primary btn-lg btn-full" type="submit">${meta.submitLabel}</button>
          </form>
        </div>

        <div class="result-panel">
          <div class="card" id="result-card">
            <div class="result-empty">
              <div class="result-empty-icon">&#127968;</div>
              <p class="t-small t-tertiary">Vyplň formulář a odešli pro výsledek.</p>
            </div>
          </div>
        </div>
      </div>
    </div>
  `;
}

function bindFormPage(mode) {
  const meta = formMeta[mode];
  const form = document.getElementById("prediction-form");
  const resultCard = document.getElementById("result-card");
  const prefillBtn = document.getElementById("prefill-btn");
  const listingUrl = document.getElementById("listing-url");
  const prefillStatus = document.getElementById("prefill-status");

  if (meta.enablePrefill) {
    prefillBtn?.addEventListener("click", () => loadPrefill(listingUrl, prefillStatus, form));
    listingUrl?.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); loadPrefill(listingUrl, prefillStatus, form); } });
    listingUrl?.addEventListener("input", () => { if (normalizeUrl(listingUrl.value) !== state.lastPrefilledUrl) state.prefillExtraFields = {}; });
  } else {
    state.prefillExtraFields = {};
    state.lastPrefilledUrl = "";
  }

  form?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(form);
    const payload = {
      experienceMode: meta.experienceMode,
      address: fd.get("address"),
      districtPrague: fd.get("districtPrague"),
      propertyType: fd.get("propertyType"),
      disposition: fd.get("disposition"),
      floorAreaM2: Number(fd.get("floorAreaM2")),
      condition: fd.get("condition"),
      ownership: fd.get("ownership"),
      construction: fd.get("construction"),
      floorNo: fd.get("floorNo") ? Number(fd.get("floorNo")) : undefined,
      hasElevator: fd.get("hasElevator") === "on",
      hasParking: fd.get("hasParking") === "on",
      energyLabel: fd.get("energyLabel"),
      askingPriceCzk: fd.get("askingPriceCzk") ? Number(fd.get("askingPriceCzk")) : undefined
    };
    for (const [k, v] of Object.entries(state.prefillExtraFields)) {
      if (v != null) payload[k] = typeof v === "boolean" ? v : Number(v);
    }

    resultCard.innerHTML = `<div class="result-empty"><p class="t-small t-secondary">Počítám odhad...</p></div>`;

    const resp = await apiFetch("/api/predict", { method: "POST", body: JSON.stringify(payload) });
    const json = await resp.json();

    if (!resp.ok) {
      if (resp.status === 402) {
        resultCard.innerHTML = `
          <div class="upgrade-inline">
            <h3 class="t-h3">Free limit vyčerpaný</h3>
            <p>${json.error}</p>
            <button class="btn btn-primary" id="result-upgrade">Upgradovat na premium</button>
          </div>`;
        document.getElementById("result-upgrade")?.addEventListener("click", startCheckout);
        if (json.usage) { state.me = { ...(state.me || {}), usage: json.usage }; }
        return;
      }
      resultCard.innerHTML = `<div class="result-empty"><p class="t-small" style="color:var(--red)">Chyba: ${json.error || "Neznámá chyba"}</p></div>`;
      return;
    }

    renderResultCard(resultCard, json, meta.experienceMode);
    if (json.usage) { state.me = { ...(state.me || {}), usage: json.usage }; }
  });
}

function renderResultCard(el, r, mode) {
  const marketLabels = {
    under_market: "Pod trhem", within_range: "V pásmu trhu",
    over_market: "Nad trhem", unknown: "Neznámé"
  };
  const badgeClass = r.marketPosition === "under_market" ? "badge-green" : r.marketPosition === "over_market" ? "badge-red" : "badge-neutral";
  const confidenceClass = r.confidenceLabel === "high" ? "badge-green" : r.confidenceLabel === "medium" ? "badge-orange" : "badge-red";
  const confidenceHints = (r.warningFlags || []).slice(0, 3).map(humanizeWarning).join(", ");

  const effects = r.featureEffects.map(e => {
    const cls = e.impactCzk >= 0 ? "positive" : "negative";
    return `<div class="effect-item"><span class="effect-name">${humanizeFeature(e.featureName)}</span><span class="effect-value ${cls}">${formatCurrency(e.impactCzk)}</span></div>`;
  }).join("");

  const notes = r.notes.length ? `<div class="result-notes"><ul>${r.notes.map(n => `<li>${n}</li>`).join("")}</ul></div>` : "";

  const delta = r.deltaVsInputPriceCzk !== null ? `<p class="t-small t-secondary" style="margin-top:4px">Rozdíl: ${formatCurrency(r.deltaVsInputPriceCzk)}</p>` : "";
  const confidenceCopy = r.confidenceLabel === "low"
    ? `<p class="t-small" style="color:var(--orange);margin-top:8px">Tento odhad ber opatrněji. ${confidenceHints || "Model měl horší vstupní podklady."}</p>`
    : confidenceHints
      ? `<p class="t-small t-secondary" style="margin-top:8px">Confidence ovlivňuje: ${confidenceHints}</p>`
      : "";

  el.innerHTML = `
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:4px">
      <span class="t-tiny t-tertiary">Model ${r.modelVersion}</span>
      <span class="badge ${badgeClass}">${marketLabels[r.marketPosition]}</span>
    </div>
    <div class="result-price">${formatCurrency(r.estimatedPriceCzk)}</div>
    <p class="result-range">Pásmo ${formatCurrency(r.typicalRangeLowCzk)} – ${formatCurrency(r.typicalRangeHighCzk)}</p>
    ${delta}
    <p class="t-small t-secondary">${r.resolvedDistrictPrague}</p>
    <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:10px">
      <span class="badge ${confidenceClass}">Confidence ${r.confidenceLabel}</span>
      <span class="badge badge-neutral">Comparables ${r.comparablesCount}</span>
      <span class="badge badge-neutral">Input quality ${Math.round((r.inputQualityScore || 0) * 100)}%</span>
    </div>
    ${confidenceCopy}
    <div class="result-divider"></div>
    <div class="t-tiny t-tertiary" style="margin-bottom:8px">Faktory</div>
    <div class="effect-list">${effects}</div>
    ${notes}
    <div style="margin-top:16px">
      <span class="badge ${r.usage?.premium ? "badge-accent" : "badge-neutral"}">${r.usage?.premium ? "Premium" : `Zbývá ${r.usage?.freeRemaining ?? "?"} predikcí`}</span>
    </div>
  `;
}

async function loadPrefill(urlEl, statusEl, form) {
  const url = normalizeUrl(urlEl?.value || "");
  if (!url) { showStatus(statusEl, "Vlož platný odkaz.", "error"); return; }

  showStatus(statusEl, "Načítám údaje z inzerátu...", "info");
  try {
    const resp = await fetch("/api/prefill-listing", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ url }) });
    const json = await resp.json();
    if (!resp.ok) throw new Error(json.error || "Selhalo");

    const f = json.fields;
    const setVal = (name, val) => {
      const el = form.elements.namedItem(name);
      if (!el) return;
      if (el.type === "checkbox") { el.checked = Boolean(val); return; }
      el.value = val == null ? "" : String(val);
    };
    setVal("address", f.address);
    setVal("districtPrague", f.districtPrague);
    setVal("propertyType", f.propertyType);
    setVal("disposition", f.disposition || "");
    setVal("floorAreaM2", f.floorAreaM2);
    setVal("askingPriceCzk", f.askingPriceCzk ?? "");
    setVal("condition", f.condition || "unknown");
    setVal("ownership", f.ownership || "unknown");
    setVal("construction", f.construction || "unknown");
    setVal("floorNo", f.floorNo ?? "");
    setVal("energyLabel", f.energyLabel || "unknown");
    setVal("hasElevator", Boolean(f.hasElevator));
    setVal("hasParking", Boolean(f.hasParking));

    state.prefillExtraFields = { landAreaM2: f.landAreaM2, totalFloors: f.totalFloors, hasCellar: f.hasCellar, hasBalconyOrLoggia: f.hasBalconyOrLoggia };
    state.lastPrefilledUrl = json.listingUrl;
    urlEl.value = json.listingUrl;

    const src = { bezrealitky: "Bezrealitky", realitymix: "RealityMix", remax: "RE/MAX" };
    showStatus(statusEl, `Předvyplněno z ${src[json.source] || json.source}. Zkontroluj údaje.`, "success");
  } catch (err) {
    state.prefillExtraFields = {};
    showStatus(statusEl, `Chyba: ${err.message}`, "error");
  }
}

/* Page: Dashboard (premium) */
function renderDashboardPage() {
  const isPremium = state.me?.usage?.premium;
  const freshness = state.teaser?.freshness;

  if (!isPremium) {
    return `
      <div class="page-header">
        <div class="page-header-left">
          <h1>Dashboard výkyvů</h1>
          <p>Extrémně levné a drahé nabídky na trhu.</p>
        </div>
      </div>
      <div class="page-body">
        <div class="card">
          <div class="paywall">
            <div class="paywall-icon">&#128274;</div>
            <h2 class="t-h2">Přístup jen pro premium</h2>
            <p>Dashboard zobrazuje nabídky, které model vyhodnotil jako výrazně nad nebo pod tržní cenou. Pro přístup je potřeba premium předplatné.</p>
            <button class="btn btn-primary btn-lg" id="paywall-upgrade">Upgradovat na premium</button>
          </div>
        </div>
      </div>
    `;
  }

  return `
    <div class="page-header">
      <div class="page-header-left">
        <h1>Dashboard výkyvů</h1>
        <p>Nabídky s výraznou odchylkou od modelu.</p>
      </div>
    </div>
    <div class="page-body">
      <div class="card">
        ${renderFreshnessBanner(freshness, "Dashboard")}
        ${renderModelBanner()}
        <div class="dashboard-filters">
          <button class="filter-btn active" data-window="1d">Den</button>
          <button class="filter-btn" data-window="7d">Týden</button>
          <button class="filter-btn" data-window="30d">Měsíc</button>
          <div class="filter-separator"></div>
          <button class="filter-btn active" data-direction="under">Pod cenou</button>
          <button class="filter-btn" data-direction="over">Nad cenou</button>
          <div class="filter-separator"></div>
          <select class="filter-select" id="dash-source">
            <option value="">Všechny zdroje</option>
            <option value="bezrealitky">Bezrealitky</option>
            <option value="realitymix">RealityMix</option>
            <option value="remax">RE/MAX</option>
          </select>
          <select class="filter-select" id="dash-type">
            <option value="">Všechny typy</option>
            <option value="flat">Byt</option>
            <option value="house">Dům</option>
          </select>
          <input class="filter-input" id="dash-district" type="text" placeholder="Lokalita..." />
          <label class="filter-toggle filter-toggle-danger">
            <input id="dash-include-filtered" type="checkbox" ${state.dashboardIncludeFiltered ? "checked" : ""} />
            <span>Povolit všechny inzeráty bez filtru</span>
          </label>
        </div>
        <div id="dash-status" class="t-small t-secondary" style="margin-bottom:12px">Načítám...</div>
        <div id="dash-table" class="opp-table"></div>
      </div>
    </div>
  `;
}

function bindDashboardPage() {
  if (!state.me?.usage?.premium) {
    document.getElementById("paywall-upgrade")?.addEventListener("click", startCheckout);
    return;
  }

  const loadDash = () => loadDashboard();

  document.querySelectorAll("[data-window]").forEach(btn => {
    btn.addEventListener("click", () => {
      state.dashboardWindow = btn.dataset.window;
      document.querySelectorAll("[data-window]").forEach(b => b.classList.toggle("active", b === btn));
      loadDash();
    });
  });

  document.querySelectorAll("[data-direction]").forEach(btn => {
    btn.addEventListener("click", () => {
      state.dashboardDirection = btn.dataset.direction;
      document.querySelectorAll("[data-direction]").forEach(b => b.classList.toggle("active", b === btn));
      loadDash();
    });
  });

  ["dash-source", "dash-type"].forEach(id => {
    document.getElementById(id)?.addEventListener("change", loadDash);
  });
  document.getElementById("dash-district")?.addEventListener("input", loadDash);
  document.getElementById("dash-include-filtered")?.addEventListener("change", (event) => {
    state.dashboardIncludeFiltered = Boolean(event.target.checked);
    loadDash();
  });

  loadDash();
}

/* Page: Account */
function renderAccountPage() {
  const email = state.me?.user?.email || state.session?.user?.email || "Neznámý email";
  const usage = state.me?.usage;
  const premium = Boolean(usage?.premium);
  const premiumStatus = usage?.premiumStatus || "free";
  const cancelAtPeriodEnd = Boolean(usage?.cancelAtPeriodEnd);
  const currentPeriodEnd = usage?.currentPeriodEnd ? formatDateTime(usage.currentPeriodEnd) : "Není k dispozici";
  const deleteAvailable = Boolean(state.config?.account?.deletionAvailable);

  const statusBadgeClass = premium ? "badge-accent" : cancelAtPeriodEnd ? "badge-orange" : "badge-neutral";
  const statusLabel = premium
    ? cancelAtPeriodEnd
      ? "Premium končí"
      : "Premium aktivní"
    : premiumStatus === "canceled"
      ? "Předplatné ukončeno"
      : "Free účet";

  return `
    <div class="page-header">
      <div class="page-header-left">
        <h1>Účet</h1>
        <p>Správa předplatného, přístupu a úplného smazání účtu.</p>
      </div>
    </div>
    <div class="page-body">
      <div class="account-grid">
        <div class="card">
          <div class="card-header">
            <div>
              <div class="card-title">Profil</div>
              <div class="card-subtitle">Základní informace o přihlášeném účtu.</div>
            </div>
            <span class="badge ${statusBadgeClass}">${statusLabel}</span>
          </div>
          <div class="account-summary-list">
            <div class="account-summary-item">
              <span class="account-summary-label">Email</span>
              <strong>${email}</strong>
            </div>
            <div class="account-summary-item">
              <span class="account-summary-label">Tarif</span>
              <strong>${premium ? "Premium" : "Free"}</strong>
            </div>
            <div class="account-summary-item">
              <span class="account-summary-label">Stav předplatného</span>
              <strong>${premiumStatus}</strong>
            </div>
            <div class="account-summary-item">
              <span class="account-summary-label">Období do</span>
              <strong>${currentPeriodEnd}</strong>
            </div>
          </div>
        </div>

        <div class="card">
          <div class="card-header">
            <div>
              <div class="card-title">Předplatné</div>
              <div class="card-subtitle">Kup premium nebo otevři správu Stripe a předplatné zruš.</div>
            </div>
          </div>
          <div class="account-subscription-panel">
            <p class="t-body">
              ${premium
                ? "Máš aktivní premium s neomezenými predikcemi a přístupem do dashboardu výkyvů."
                : "Aktuálně jsi na free plánu se 3 predikcemi na účet."}
            </p>
            ${cancelAtPeriodEnd ? `<div class="status-msg info">Zrušení předplatného je naplánované na konec aktuálního období (${currentPeriodEnd}).</div>` : ""}
            <div class="account-actions">
              ${premium
                ? `<button class="btn btn-secondary btn-lg" id="account-manage-subscription">Spravovat / zrušit předplatné</button>`
                : `<button class="btn btn-primary btn-lg" id="account-upgrade">Koupit premium</button>`}
            </div>
          </div>
        </div>

        <div class="card account-danger-card">
          <div class="card-header">
            <div>
              <div class="card-title">Smazání účtu</div>
              <div class="card-subtitle">Nenávratně smaže přihlášení i lokální data účtu.</div>
            </div>
          </div>
          <div class="account-danger-zone">
            <p class="t-body t-secondary">
              Pro potvrzení napiš email účtu <strong>${email}</strong>. Tato akce smaže účet úplně.
            </p>
            <div class="field">
              <label class="field-label" for="delete-account-confirmation">Potvrzovací email</label>
              <input id="delete-account-confirmation" type="email" class="field-input" placeholder="${email}" ${deleteAvailable ? "" : "disabled"} />
            </div>
            <div id="account-delete-status"></div>
            <div class="account-actions">
              <button class="btn btn-danger btn-lg" id="account-delete-btn" ${deleteAvailable ? "" : "disabled"}>
                Smazat účet
              </button>
            </div>
            ${deleteAvailable ? "" : `<p class="t-small t-tertiary">Mazání účtu bude dostupné po doplnění <code>SUPABASE_SERVICE_ROLE_KEY</code> do workeru.</p>`}
          </div>
        </div>
      </div>
    </div>
  `;
}

function bindAccountPage() {
  document.getElementById("account-upgrade")?.addEventListener("click", startCheckout);
  document.getElementById("account-manage-subscription")?.addEventListener("click", startBillingPortal);
  document.getElementById("account-delete-btn")?.addEventListener("click", deleteAccount);
}

async function loadDashboard() {
  const statusEl = document.getElementById("dash-status");
  const tableEl = document.getElementById("dash-table");
  if (!statusEl || !tableEl) return;
  const freshness = state.teaser?.freshness;

  statusEl.textContent = "Načítám příležitosti...";

  const params = new URLSearchParams({ window: state.dashboardWindow, direction: state.dashboardDirection });
  const src = document.getElementById("dash-source")?.value;
  const typ = document.getElementById("dash-type")?.value;
  const dist = document.getElementById("dash-district")?.value?.trim();
  if (src) params.set("source", src);
  if (typ) params.set("propertyType", typ);
  if (dist) params.set("district", dist);
  if (state.dashboardIncludeFiltered) params.set("includeFiltered", "true");

  const resp = await apiFetch(`/api/dashboard/opportunities?${params}`, { method: "GET", headers: {} });
  const json = await resp.json();

  if (!resp.ok) { statusEl.textContent = json.error || "Chyba."; return; }

  const freshnessSuffix = freshness?.generatedAt ? ` Aktualizováno ${formatDateTime(freshness.generatedAt)}.` : "";
  statusEl.textContent = `${json.opportunities.length} příležitostí${state.dashboardIncludeFiltered ? " bez výchozího filtru" : ""}.${freshnessSuffix}`;

  if (!json.opportunities.length) {
    if (freshness?.isStale || freshness?.isDegraded) {
      tableEl.innerHTML = `<p class="t-small t-tertiary" style="padding:20px 0;text-align:center">${humanizeStaleReason(freshness.staleReason, freshness.degradedSources || [])}</p>`;
      return;
    }
    tableEl.innerHTML = `<p class="t-small t-tertiary" style="padding:20px 0;text-align:center">Žádné nabídky v tomto filtru.</p>`;
    return;
  }

  tableEl.innerHTML = json.opportunities.map(row => `
    <div class="opp-row">
      <div>
        <div class="opp-address">${row.addressText}</div>
        <div class="opp-meta">${row.districtPrague} &middot; ${row.source} &middot; ${row.propertyType === "flat" ? "Byt" : "Dům"}</div>
      </div>
      <div class="opp-prices">
        <span>Inzerát ${formatCurrency(row.askingPriceCzk)}</span>
        <span>Model ${formatCurrency(row.predictedPriceCzk)}</span>
        <strong>${formatCurrency(row.deviationCzk)} &middot; ${formatPercent(row.deviationPct)}</strong>
      </div>
      <div class="opp-actions">
        <span class="badge ${row.marketPosition === "under_market" ? "badge-green" : "badge-red"}">${row.marketPosition === "under_market" ? "Pod cenou" : "Nad cenou"}</span>
        ${state.dashboardIncludeFiltered && row.isFilteredDefault ? `<span class="badge badge-red">Nízká jistota</span>` : ""}
        <a class="opp-link" href="${row.listingUrl}" target="_blank" rel="noreferrer">Otevřít &rarr;</a>
      </div>
      ${state.dashboardIncludeFiltered && row.isFilteredDefault ? `<div class="opp-warning">Skryto v defaultním režimu: ${(row.filterReasons || []).map(humanizeWarning).join(", ")}</div>` : ""}
    </div>
  `).join("");
}

/* Checkout */
async function startCheckout() {
  const resp = await apiFetch("/api/billing/create-checkout-session", { method: "POST", body: JSON.stringify({}) });
  const json = await resp.json();
  if (!resp.ok) { alert(json.error || "Checkout se nepodařilo vytvořit."); return; }
  window.location.href = json.url;
}

async function startBillingPortal() {
  const resp = await apiFetch("/api/billing/create-portal-session", { method: "POST", body: JSON.stringify({}) });
  const json = await resp.json();
  if (!resp.ok) { alert(json.error || "Správu předplatného se nepodařilo otevřít."); return; }
  window.location.href = json.url;
}

async function deleteAccount() {
  const input = document.getElementById("delete-account-confirmation");
  const statusEl = document.getElementById("account-delete-status");
  const confirmation = input?.value?.trim() || "";
  const email = state.me?.user?.email || state.session?.user?.email || "";

  if (!confirmation) {
    showStatus(statusEl, "Pro potvrzení zadej email účtu.", "error");
    return;
  }
  if (confirmation.toLowerCase() !== email.toLowerCase()) {
    showStatus(statusEl, "Potvrzovací email se neshoduje s účtem.", "error");
    return;
  }
  if (!window.confirm("Opravdu chceš účet smazat? Tuto akci nejde vrátit.")) {
    return;
  }

  showStatus(statusEl, "Mažu účet...", "info");
  const resp = await apiFetch("/api/account/delete", {
    method: "POST",
    body: JSON.stringify({ confirmation })
  });
  const json = await resp.json();

  if (!resp.ok) {
    showStatus(statusEl, json.error || "Smazání účtu selhalo.", "error");
    return;
  }

  await state.supabase?.auth.signOut();
  state.me = null;
  state.session = null;
  navigate("/login", true);
}

/* Main Render */
function render() {
  const path = currentPath();
  const app = document.getElementById("app");
  if (path === "/" || path === "") {
    app.innerHTML = renderLanding();
    bindLinks();
    return;
  }

  if (path === "/login") {
    if (state.session) { navigate("/app/naceneni", true); return; }
    app.innerHTML = renderLogin();
    bindLinks();
    bindLogin();
    return;
  }
  if (path === "/auth/callback") {
    app.innerHTML = `<div class="login-page"><div class="login-card"><p class="t-body t-secondary">Dokončuji přihlášení...</p></div></div>`;
    return;
  }
  if (!state.session) {
    navigate("/login", true);
    return;
  }
  if (path === "/app" || path === "/app/") {
    navigate("/app/naceneni", true);
    return;
  }

  if (path === "/app/naceneni") {
    app.innerHTML = renderAppShell(renderFormPage("naceneni"), "naceneni");
    bindAppShell();
    bindFormPage("naceneni");
    bindLinks();
    return;
  }

  if (path === "/app/insight") {
    app.innerHTML = renderAppShell(renderFormPage("insight"), "insight");
    bindAppShell();
    bindFormPage("insight");
    bindLinks();
    return;
  }

  if (path === "/app/prehled") {
    app.innerHTML = renderAppShell(renderDashboardPage(), "prehled");
    bindAppShell();
    bindDashboardPage();
    bindLinks();
    return;
  }

  if (path === "/app/ucet") {
    app.innerHTML = renderAppShell(renderAccountPage(), "ucet");
    bindAppShell();
    bindAccountPage();
    bindLinks();
    return;
  }
  app.innerHTML = renderAppShell(`<div class="page-header"><div class="page-header-left"><h1>404</h1><p>Stránka nenalezena.</p></div></div><div class="page-body"><a href="/app/naceneni" data-link class="btn btn-primary">Zpět na nacenění</a></div>`, "");
  bindAppShell();
  bindLinks();
}

function bindLinks() {
  document.querySelectorAll("a[data-link]").forEach(a => {
    a.addEventListener("click", (e) => {
      e.preventDefault();
      navigate(a.getAttribute("href"));
    });
  });
}

async function completeAuthCallback() {
  if (currentPath() !== "/auth/callback" || !state.supabase) {
    return false;
  }

  const url = new URL(window.location.href);
  const tokenHash = url.searchParams.get("token_hash");
  if (tokenHash) {
    const { error } = await state.supabase.auth.verifyOtp({
      token_hash: tokenHash,
      type: url.searchParams.get("type") || "email"
    });
    if (error) {
      return false;
    }
    const { data: { session } } = await state.supabase.auth.getSession();
    state.session = session;
    return Boolean(session);
  }

  const fragment = new URLSearchParams(window.location.hash.replace(/^#/, ""));
  const accessToken = fragment.get("access_token");
  const refreshToken = fragment.get("refresh_token");
  if (!accessToken || !refreshToken) {
    return false;
  }

  const { data, error } = await state.supabase.auth.setSession({
    access_token: accessToken,
    refresh_token: refreshToken
  });
  if (error) {
    return false;
  }
  state.session = data.session;
  return Boolean(data.session);
}

/* Boot */
async function boot() {
  const configResp = await fetch("/api/config");
  state.config = await configResp.json();
  const [healthResp, teaserResp] = await Promise.all([
    fetch("/api/health").catch(() => null),
    fetch("/api/dashboard/teaser").catch(() => null)
  ]);
  if (healthResp?.ok) state.health = await healthResp.json();
  if (teaserResp?.ok) state.teaser = await teaserResp.json();
  if (state.config?.auth?.configured && state.config?.auth?.supabaseUrl && state.config?.auth?.supabaseAnonKey) {
    state.supabase = createClient(state.config.auth.supabaseUrl, state.config.auth.supabaseAnonKey, {
      auth: { persistSession: true, autoRefreshToken: true, detectSessionInUrl: false }
    });

    const { data: { session } } = await state.supabase.auth.getSession();
    state.session = session;

    state.supabase.auth.onAuthStateChange((_event, s) => {
      state.session = s;
      if (s) refreshMe().then(() => render());
      else render();
    });
  }
  if (await completeAuthCallback()) {
    window.history.replaceState({}, "", "/app/naceneni");
    await refreshMe();
    render();
    return;
  }

  if (currentPath() === "/auth/callback") {
    navigate("/login", true);
    return;
  }
  const billingState = new URL(window.location.href).searchParams.get("billing");
  if (billingState === "success" && state.session) {
    await refreshMe();
  }
  if (state.session) {
    await refreshMe();
  }

  render();
}

async function refreshMe() {
  if (!state.session?.access_token) { state.me = null; return; }
  const resp = await apiFetch("/api/me", { method: "GET", headers: {} });
  if (resp.ok) state.me = await resp.json();
  else state.me = null;
}

boot();
