const SESSION_KEY = "commitmentops.session";
const ORGANIZATION_KEY = "commitmentops.organization";

const DEFAULT_BRAND = {
  display_name: "CommitmentOps",
  logo_url: null,
  primary_color: "#1B3A4B",
  accent_color: "#B08D57",
  favicon_url: null,
};

const WORKFLOW_TYPES = [
  ["grant", "Grant"],
  ["compliance", "Compliance"],
  ["vendor_contract", "Vendor contract"],
  ["customer_implementation", "Customer implementation"],
  ["regulatory_reporting", "Regulatory reporting"],
  ["general", "General"],
];

const root = document.querySelector("#app");
let config = { supabase_url: null, supabase_anon_key: null };
let organizations = [];
let activeOrganization = null;

function el(tag, attributes = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attributes)) {
    if (value == null) continue;
    if (key === "text") node.textContent = value;
    else if (key === "dataset") Object.assign(node.dataset, value);
    else if (key.startsWith("on") && typeof value === "function") {
      node.addEventListener(key.slice(2), value);
    } else node.setAttribute(key, value);
  }
  for (const child of children) {
    if (child) node.append(child);
  }
  return node;
}

function labelFor(value) {
  return String(value || "").replaceAll("_", " ");
}

function readSession() {
  try {
    const parsed = JSON.parse(sessionStorage.getItem(SESSION_KEY) || "null");
    if (!parsed?.accessToken || !parsed?.refreshToken) return null;
    return parsed;
  } catch {
    return null;
  }
}

function writeSession(session) {
  if (session) sessionStorage.setItem(SESSION_KEY, JSON.stringify(session));
  else sessionStorage.removeItem(SESSION_KEY);
}

function selectedOrganizationId() {
  return sessionStorage.getItem(ORGANIZATION_KEY);
}

function rememberOrganization(organization) {
  activeOrganization = organization;
  if (organization) sessionStorage.setItem(ORGANIZATION_KEY, organization.id);
  else sessionStorage.removeItem(ORGANIZATION_KEY);
  applyBrand(organization?.branding || DEFAULT_BRAND);
}

function safeColor(value, fallback) {
  return /^#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3})?$/.test(value || "")
    ? value
    : fallback;
}

function applyBrand(brand) {
  const resolved = brand || DEFAULT_BRAND;
  document.documentElement.style.setProperty(
    "--primary",
    safeColor(resolved.primary_color, DEFAULT_BRAND.primary_color),
  );
  document.documentElement.style.setProperty(
    "--accent",
    safeColor(resolved.accent_color, DEFAULT_BRAND.accent_color),
  );
  document.title = resolved.display_name || DEFAULT_BRAND.display_name;
  const favicon = document.querySelector("#app-favicon");
  const faviconUrl = resolved.favicon_url || "/assets/favicon.svg";
  if (faviconUrl.startsWith("https://") || faviconUrl.startsWith("/assets/")) {
    favicon.setAttribute("href", faviconUrl);
  }
}

function banner(message, tone = "danger") {
  return el("div", { class: "banner", role: "alert", dataset: { tone }, text: message });
}

function brandMark(brand) {
  if (brand.logo_url && brand.logo_url.startsWith("https://")) {
    return el("img", { src: brand.logo_url, alt: "" });
  }
  const initial = (brand.display_name || "C").trim().charAt(0).toUpperCase();
  return el("div", { class: "monogram", "aria-hidden": "true", text: initial });
}

async function loadConfig() {
  const response = await fetch("/api/v1/client-config");
  if (!response.ok) throw new Error("config");
  config = await response.json();
}

async function supabaseToken(grantType, body) {
  const response = await fetch(
    `${config.supabase_url}/auth/v1/token?grant_type=${grantType}`,
    {
      method: "POST",
      headers: {
        apikey: config.supabase_anon_key,
        "Content-Type": "application/json",
      },
      body: JSON.stringify(body),
    },
  );
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(payload.msg || payload.error_description || "sign-in failed");
    error.status = response.status;
    throw error;
  }
  return {
    accessToken: payload.access_token,
    refreshToken: payload.refresh_token,
    expiresAt: Date.now() + Number(payload.expires_in || 3600) * 1000,
  };
}

async function ensureSession() {
  const session = readSession();
  if (!session) return null;
  if (session.expiresAt - Date.now() > 60_000) return session;
  try {
    const refreshed = await supabaseToken("refresh_token", {
      refresh_token: session.refreshToken,
    });
    writeSession(refreshed);
    return refreshed;
  } catch {
    writeSession(null);
    return null;
  }
}

async function api(path, options = {}) {
  const session = await ensureSession();
  if (!session) {
    const expired = new Error("expired");
    expired.status = 401;
    throw expired;
  }
  const headers = {
    Authorization: `Bearer ${session.accessToken}`,
    Accept: "application/json",
  };
  if (options.organization) headers["X-Organization-ID"] = options.organization;
  if (options.body) headers["Content-Type"] = "application/json";
  const response = await fetch(path, {
    method: options.method || "GET",
    headers,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  if (response.status === 401 && !options.retried) {
    writeSession({ ...session, expiresAt: 0 });
    return api(path, { ...options, retried: true });
  }
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(messageFrom(payload, response.status));
    error.status = response.status;
    throw error;
  }
  return payload;
}

function messageFrom(payload, status) {
  const details = payload?.error?.details;
  if (Array.isArray(details) && details.length) {
    return details.map((item) => item.message).filter(Boolean).join(" ");
  }
  if (payload?.error?.message) return payload.error.message;
  if (status === 401) return "Your session has expired. Sign in again.";
  return "The request could not be completed.";
}

async function loadOrganizations() {
  organizations = await api("/api/v1/organizations");
  const stored = selectedOrganizationId();
  const match = organizations.find((item) => item.id === stored) || organizations[0] || null;
  rememberOrganization(match);
}

function shell(content) {
  const brand = activeOrganization?.branding || DEFAULT_BRAND;
  const actions = [el("button", {
    class: "button-secondary",
    type: "button",
    text: "Log out",
    onclick: () => signOut(),
  })];
  if (organizations.length > 1) {
    const select = el("select", {
      class: "org-select",
      "aria-label": "Organization",
      onchange: (event) => {
        const next = organizations.find((item) => item.id === event.target.value);
        rememberOrganization(next);
        if ((location.hash || "#/workflows") === "#/workflows") render();
        else location.hash = "#/workflows";
      },
    }, organizations.map((item) => el("option", {
      value: item.id,
      text: item.branding.display_name,
      selected: item.id === activeOrganization?.id ? "selected" : null,
    })));
    actions.unshift(select);
  }
  return el("div", {}, [
    el("header", { class: "app-header" }, [
      el("div", { class: "brand" }, [
        brandMark(brand),
        el("h1", { text: brand.display_name }),
      ]),
      el("div", { class: "header-actions" }, actions),
    ]),
    el("main", { class: "page" }, [content]),
  ]);
}

function signInView(notice = "") {
  applyBrand(DEFAULT_BRAND);
  const configured = Boolean(config.supabase_url && config.supabase_anon_key);
  const form = el("form", { class: "panel" }, [
    brandMark(DEFAULT_BRAND),
    el("h1", { text: "CommitmentOps" }),
    el("p", { class: "lede", text: "Sign in to the accountability workspace for your organization." }),
  ]);
  if (!configured) {
    form.append(banner("Sign-in is not configured for this environment."));
  }
  if (notice) form.append(banner(notice));
  const email = el("input", { id: "email", type: "email", autocomplete: "username", required: "required" });
  const password = el("input", { id: "password", type: "password", autocomplete: "current-password", required: "required" });
  const submit = el("button", { class: "button", type: "submit", text: "Sign in" });
  form.append(
    el("label", { class: "field" }, [el("span", { text: "Email" }), email]),
    el("label", { class: "field" }, [el("span", { text: "Password" }), password]),
    el("div", { class: "field" }, [submit]),
  );
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    submit.disabled = true;
    submit.textContent = "Signing in…";
    try {
      const session = await supabaseToken("password", {
        email: email.value.trim(),
        password: password.value,
      });
      writeSession(session);
      location.hash = "#/workflows";
      await render();
    } catch (error) {
      submit.disabled = false;
      submit.textContent = "Sign in";
      const message = error.status === 400
        ? "Email or password is incorrect."
        : "Sign-in is unavailable right now.";
      form.querySelector(".banner")?.remove();
      form.insertBefore(banner(message), form.querySelector("label"));
    }
  });
  if (!configured) submit.disabled = true;
  return el("div", { class: "sign-in" }, [form]);
}

function loadingView(message) {
  return shell(el("p", { class: "lede", role: "status", text: message }));
}

function dashboardView(workflows, errorMessage = "") {
  const items = workflows.map((workflow) => el("a", {
    class: "card card-link",
    href: `#/workflows/${workflow.id}`,
  }, [
    el("h3", { text: workflow.name }),
    el("div", { class: "meta" }, [
      el("span", { class: "chip", text: labelFor(workflow.workflow_type) }),
      el("span", { class: "chip", text: labelFor(workflow.status) }),
      el("span", { class: "muted", text: `${workflow.requirements.length} requirements` }),
    ]),
  ]));
  return shell(el("section", {}, [
    el("h2", { text: "Workflows" }),
    el("p", { class: "lede", text: "Commitments for this organization." }),
    errorMessage ? banner(errorMessage) : null,
    el("div", { class: "toolbar" }, [
      el("span", { class: "muted", text: workflows.length ? `${workflows.length} workflows` : "" }),
      el("a", { class: "button", href: "#/workflows/new", text: "New workflow" }),
    ]),
    workflows.length
      ? el("div", { class: "stack" }, items)
      : el("div", { class: "card" }, [
          el("h3", { text: "No workflows yet" }),
          el("p", { class: "muted", text: "Create the first workflow for this organization." }),
        ]),
  ]));
}

function createView(errorMessage = "") {
  const name = el("input", { id: "workflow-name", maxlength: "300", required: "required" });
  const type = el("select", { id: "workflow-type" }, WORKFLOW_TYPES.map(([value, text]) => (
    el("option", { value, text })
  )));
  const submit = el("button", { class: "button", type: "submit", text: "Create workflow" });
  const form = el("form", { class: "card" }, [
    errorMessage ? banner(errorMessage) : null,
    el("label", { class: "field" }, [el("span", { text: "Name" }), name]),
    el("label", { class: "field" }, [el("span", { text: "Type" }), type]),
    el("div", { class: "toolbar" }, [
      el("a", { class: "button-secondary", href: "#/workflows", text: "Cancel" }),
      submit,
    ]),
  ]);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!name.value.trim()) {
      mount(createView("Enter a workflow name."));
      return;
    }
    submit.disabled = true;
    try {
      const created = await api("/api/v1/workflows", {
        method: "POST",
        organization: activeOrganization.id,
        body: { name: name.value.trim(), workflow_type: type.value },
      });
      location.hash = `#/workflows/${created.id}`;
      await render();
    } catch (error) {
      if (error.status === 401) return showExpired();
      mount(createView(error.message));
    }
  });
  return shell(el("section", {}, [
    el("h2", { text: "New workflow" }),
    el("p", { class: "lede", text: "Uses the existing workflow contract. Requirements are added later." }),
    form,
  ]));
}

function detailView(workflow) {
  const requirements = workflow.requirements.length
    ? el("div", { class: "stack" }, workflow.requirements.map((requirement) => el("article", { class: "card requirement" }, [
        el("h3", { text: requirement.title }),
        el("p", { text: requirement.description }),
        el("div", { class: "requirement-meta" }, [
          el("span", { class: "chip", text: labelFor(requirement.status) }),
          el("span", { class: "chip", text: labelFor(requirement.review_status) }),
          el("span", { class: "muted", text: requirement.owner?.name || "Unassigned" }),
          el("span", { class: "muted", text: requirement.due_date || "No due date" }),
        ]),
        el("p", { text: requirement.evidence_expectation }),
      ])))
    : el("div", { class: "card" }, [
        el("h3", { text: "No requirements yet" }),
        el("p", { class: "muted", text: "This workflow has no persisted requirements." }),
      ]);
  return shell(el("section", {}, [
    el("p", {}, [el("a", { href: "#/workflows", text: "Back to workflows" })]),
    el("h2", { text: workflow.name }),
    el("div", { class: "meta" }, [
      el("span", { class: "chip", text: labelFor(workflow.workflow_type) }),
      el("span", { class: "chip", text: labelFor(workflow.status) }),
    ]),
    el("h3", { text: "Requirements" }),
    requirements,
  ]));
}

function membershipView() {
  return shell(el("section", { class: "card" }, [
    el("h2", { text: "No organization membership" }),
    el("p", { text: "An administrator must add this account to an organization before workflows are available." }),
  ]));
}

function showExpired() {
  writeSession(null);
  rememberOrganization(null);
  organizations = [];
  mount(signInView("Your session has expired. Sign in again."));
}

async function signOut() {
  const session = readSession();
  writeSession(null);
  rememberOrganization(null);
  organizations = [];
  if (session && config.supabase_url && config.supabase_anon_key) {
    await fetch(`${config.supabase_url}/auth/v1/logout`, {
      method: "POST",
      headers: {
        apikey: config.supabase_anon_key,
        Authorization: `Bearer ${session.accessToken}`,
      },
    }).catch(() => {});
  }
  location.hash = "#/sign-in";
  mount(signInView());
}

function parseRoute() {
  const hash = location.hash.replace(/^#/, "") || "/workflows";
  const parts = hash.split("/").filter(Boolean);
  if (parts[0] === "sign-in") return { name: "sign-in" };
  if (parts[0] === "workflows" && parts[1] === "new") return { name: "create" };
  if (parts[0] === "workflows" && parts[1]) return { name: "detail", id: parts[1] };
  return { name: "dashboard" };
}

function mount(view) {
  root.replaceChildren(view);
}

async function render() {
  const route = parseRoute();
  if (!(await ensureSession()) || route.name === "sign-in") {
    mount(signInView());
    return;
  }
  mount(loadingView("Loading organization…"));
  try {
    if (!activeOrganization) await loadOrganizations();
  } catch (error) {
    if (error.status === 401) return showExpired();
    mount(shell(banner(error.message)));
    return;
  }
  if (!activeOrganization) {
    mount(membershipView());
    return;
  }
  try {
    if (route.name === "create") {
      mount(createView());
      return;
    }
    if (route.name === "detail") {
      mount(loadingView("Loading workflow…"));
      const workflow = await api(`/api/v1/workflows/${route.id}`, {
        organization: activeOrganization.id,
      });
      mount(detailView(workflow));
      return;
    }
    mount(loadingView("Loading workflows…"));
    const workflows = await api("/api/v1/workflows", {
      organization: activeOrganization.id,
    });
    mount(dashboardView(workflows));
  } catch (error) {
    if (error.status === 401) return showExpired();
    mount(route.name === "detail"
      ? shell(el("section", {}, [
          el("p", {}, [el("a", { href: "#/workflows", text: "Back to workflows" })]),
          banner(error.message),
        ]))
      : dashboardView([], error.message));
  }
}

window.addEventListener("hashchange", () => {
  render();
});

loadConfig()
  .catch(() => {
    config = { supabase_url: null, supabase_anon_key: null };
  })
  .finally(render);
