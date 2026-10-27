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

/* attention-rules */
const WORKFLOW_TYPE_LABELS = Object.fromEntries(WORKFLOW_TYPES);
const WORKFLOW_STATUS_LABELS = {
  draft: "Draft",
  active: "Active",
  completed: "Completed",
  cancelled: "Cancelled",
};
const WORK_LABELS = {
  not_started: "Not started",
  in_progress: "In progress",
  blocked: "Blocked",
  completed: "Completed",
  cancelled: "Cancelled",
};
const REVIEW_LABELS = {
  pending_review: "Awaiting review",
  approved: "Approved",
  rejected: "Rejected",
};

function readableLabel(value, labels) {
  if (value == null || value === "") return "";
  if (labels && Object.prototype.hasOwnProperty.call(labels, value)) {
    return labels[value];
  }
  return String(value)
    .replaceAll("_", " ")
    .replace(/\b[a-z]/g, (letter) => letter.toUpperCase());
}

function parseCalendarDate(value) {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(value || ""));
  if (!match) return null;
  return {
    year: Number(match[1]),
    month: Number(match[2]),
    day: Number(match[3]),
  };
}

function localCalendarDate(now = new Date()) {
  return {
    year: now.getFullYear(),
    month: now.getMonth() + 1,
    day: now.getDate(),
  };
}

function compareCalendarDates(left, right) {
  if (left.year !== right.year) return left.year - right.year;
  if (left.month !== right.month) return left.month - right.month;
  return left.day - right.day;
}

function isPastDue(requirement, today) {
  const due = parseCalendarDate(requirement?.due_date);
  if (!due) return false;
  if (requirement.status === "completed" || requirement.status === "cancelled") {
    return false;
  }
  return compareCalendarDates(due, today) < 0;
}

function attentionReasons(requirement, today) {
  const reasons = [];
  if (isPastDue(requirement, today)) reasons.push("Past due");
  if (requirement?.status === "blocked") reasons.push("Blocked");
  if (requirement?.review_status === "pending_review") reasons.push("Awaiting review");
  return reasons;
}

function attentionItems(workflows, today = localCalendarDate()) {
  const items = [];
  for (const workflow of workflows || []) {
    if (!workflow?.id) continue;
    for (const requirement of workflow.requirements || []) {
      const reasons = attentionReasons(requirement, today);
      if (!reasons.length) continue;
      items.push({
        workflowId: workflow.id,
        workflowName: workflow.name || "",
        title: requirement?.title || "",
        ownerName: requirement?.owner?.name || "",
        dueDate: parseCalendarDate(requirement?.due_date) ? requirement.due_date : "",
        reasons,
      });
    }
  }
  return items;
}

function formatCalendarDate(value) {
  const parts = parseCalendarDate(value);
  if (!parts) return "";
  return new Date(parts.year, parts.month - 1, parts.day).toLocaleDateString(
    undefined,
    { year: "numeric", month: "long", day: "numeric" },
  );
}

function provenanceLabel(requirement) {
  const provenance = requirement?.provenance;
  if (!provenance) return "";
  if (provenance === "user_provided") return "Entered by a person";
  if (provenance === "ai_inferred" && requirement.review_status === "pending_review") {
    return "Suggested, awaiting approval";
  }
  if (provenance === "ai_inferred" && requirement.review_status === "approved") {
    return "Suggested, approved by a person";
  }
  if (provenance === "ai_inferred" && requirement.review_status === "rejected") {
    return "Suggested, not approved";
  }
  if (provenance === "document_explicit") return "Taken from a source document";
  if (provenance === "organization_template") return "From an organization template";
  if (provenance === "system_derived") return "Recorded by the system";
  return "";
}
/* end-attention-rules */

function labelFor(value) {
  return readableLabel(value);
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

function reasonClass(reason) {
  if (reason === "Past due") return "chip reason-past-due";
  if (reason === "Blocked") return "chip reason-blocked";
  return "chip reason-review";
}

function attentionView(workflows) {
  const items = attentionItems(workflows);
  if (!items.length) {
    return el("div", { class: "card" }, [
      el("h3", { text: "Nothing needs attention" }),
      el("p", {
        class: "muted",
        text: "No requirements are past due, blocked, or awaiting review.",
      }),
    ]);
  }
  return el("div", { class: "stack" }, items.map((item) => el("a", {
    class: "card card-link attention-item",
    href: `#/workflows/${item.workflowId}`,
  }, [
    el("p", { class: "muted", text: item.workflowName }),
    item.title ? el("h3", { text: item.title }) : null,
    el("div", { class: "meta" }, item.reasons.map((reason) => (
      el("span", { class: reasonClass(reason), text: reason })
    ))),
    el("div", { class: "meta" }, [
      item.ownerName ? el("span", { text: item.ownerName }) : null,
      item.dueDate ? el("span", {
        class: item.reasons.includes("Past due") ? "is-past-due" : "muted",
        text: formatCalendarDate(item.dueDate),
      }) : null,
    ]),
  ])));
}

function dashboardView(workflows, errorMessage = "") {
  const items = workflows.map((workflow) => el("a", {
    class: "card card-link",
    href: `#/workflows/${workflow.id}`,
  }, [
    el("h3", { text: workflow.name }),
    el("div", { class: "meta" }, [
      el("span", { class: "chip", text: readableLabel(workflow.workflow_type, WORKFLOW_TYPE_LABELS) }),
      el("span", { class: "chip", text: readableLabel(workflow.status, WORKFLOW_STATUS_LABELS) }),
      el("span", { class: "muted", text: `${workflow.requirements.length} requirements` }),
    ]),
  ]));
  return shell(el("section", {}, [
    errorMessage ? banner(errorMessage) : null,
    el("h2", { text: "Needs attention" }),
    attentionView(workflows),
    el("h2", { class: "section-title", text: "Workflows" }),
    el("p", { class: "lede", text: "Commitments for this organization." }),
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
    el("p", { class: "lede", text: "Add requirements after the workflow is created." }),
    form,
  ]));
}

function supportedStatusActions(requirement) {
  if (!requirement.is_authoritative) return [];
  const actions = [];
  if (requirement.status === "not_started" || requirement.status === "blocked") {
    actions.push(["start_work", "Start work"]);
  }
  if (requirement.status === "in_progress") {
    actions.push(["block", "Block"]);
  }
  if (requirement.status === "not_started" || requirement.status === "in_progress") {
    actions.push(["complete", "Complete"]);
  }
  return actions;
}

function fact(label, value, valueClass = "") {
  return el("div", { class: "fact" }, [
    el("dt", { text: label }),
    el("dd", { class: valueClass, text: value }),
  ]);
}

function requirementCard(workflow, requirement) {
  const pastDue = isPastDue(requirement, localCalendarDate());
  const ownerName = requirement.owner?.name || "";
  const ownerEmail = requirement.owner?.email || "";
  const ownerText = [ownerName, ownerEmail].filter(Boolean).join(" · ");
  const dueText = formatCalendarDate(requirement.due_date);
  const provenance = provenanceLabel(requirement);
  const recordedEvidence = Array.isArray(requirement.evidence) ? requirement.evidence : [];
  const card = el("article", { class: "card requirement" }, [
    el("h3", { text: requirement.title }),
    requirement.description ? el("p", { text: requirement.description }) : null,
    el("dl", { class: "facts" }, [
      fact("Review", readableLabel(requirement.review_status, REVIEW_LABELS) || "Not recorded"),
      fact(
        "Work",
        readableLabel(requirement.status, WORK_LABELS) || "Not recorded",
        requirement.status === "blocked" ? "is-blocked" : "",
      ),
      fact("Owner", ownerText || "Not recorded"),
      fact("Due date", dueText || "Not recorded", pastDue ? "is-past-due" : ""),
      fact("Evidence expected", requirement.evidence_expectation || "Not recorded"),
    ]),
    recordedEvidence.length
      ? el("div", { class: "evidence-list" }, [
          el("p", { class: "fact-label", text: "Evidence" }),
          ...recordedEvidence.map((item) => el("div", {}, [
            el("p", { text: evidenceSummary(item) }),
            evidenceFileNote(workflow, requirement, item),
          ])),
        ])
      : el("p", { text: "No evidence recorded." }),
    provenance ? el("p", { class: "muted", text: provenance }) : null,
    requirement.review_status === "pending_review"
      ? el("p", {
          class: "review-note",
          text: "A person must approve this requirement before work can begin.",
        })
      : null,
  ]);
  const actions = supportedStatusActions(requirement);
  if (actions.length) {
    card.append(el("div", { class: "requirement-actions" }, actions.map(([action, label]) => {
      const button = el("button", {
        class: "button-secondary",
        type: "button",
        text: label,
        "data-requirement-action": "true",
      });
      button.addEventListener("click", () => {
        transitionRequirement(workflow, requirement, action, card);
      });
      return button;
    })));
  }
  card.append(submissionControls(workflow, requirement));
  return card;
}

async function transitionRequirement(workflow, requirement, action, card) {
  const buttons = [...card.querySelectorAll("[data-requirement-action]")];
  for (const button of buttons) button.disabled = true;
  card.querySelector(".banner")?.remove();
  try {
    await api(
      `/api/v1/workflows/${workflow.id}/requirements/${requirement.id}/transitions`,
      {
        method: "POST",
        organization: activeOrganization.id,
        body: { action },
      },
    );
  } catch (error) {
    if (error.status === 401) return showExpired();
    for (const button of buttons) button.disabled = false;
    card.prepend(banner(error.message));
    return;
  }
  try {
    const fresh = await reloadWorkflow(workflow.id);
    mount(detailView(fresh));
  } catch (error) {
    if (error.status === 401) return showExpired();
    card.prepend(banner("The status was saved. Reload this page to see it."));
  }
}

function requirementForm(workflow) {
  const title = el("input", {
    id: "requirement-title",
    maxlength: "500",
    required: "required",
  });
  const description = el("textarea", {
    id: "requirement-description",
    required: "required",
  });
  const ownerName = el("input", {
    id: "requirement-owner-name",
    maxlength: "200",
    required: "required",
  });
  const ownerEmail = el("input", {
    id: "requirement-owner-email",
    type: "email",
    maxlength: "320",
    required: "required",
  });
  const evidence = el("textarea", {
    id: "requirement-evidence",
    required: "required",
  });
  const dueDate = el("input", { id: "requirement-due-date", type: "date" });
  const submit = el("button", {
    class: "button",
    type: "submit",
    text: "Add requirement",
  });
  const form = el("form", { class: "card requirement-form" }, [
    el("h3", { text: "Add a requirement" }),
    el("p", {
      class: "muted",
      text: "Record what was promised, who owns it, and what evidence is expected.",
    }),
    el("div", { class: "form-grid" }, [
      el("label", { class: "field wide" }, [el("span", { text: "Title" }), title]),
      el("label", { class: "field wide" }, [
        el("span", { text: "Description" }),
        description,
      ]),
      el("label", { class: "field" }, [el("span", { text: "Owner name" }), ownerName]),
      el("label", { class: "field" }, [el("span", { text: "Owner email" }), ownerEmail]),
      el("label", { class: "field wide" }, [
        el("span", { text: "Evidence expectation" }),
        evidence,
      ]),
      el("label", { class: "field" }, [el("span", { text: "Due date" }), dueDate]),
    ]),
    el("div", { class: "toolbar" }, [submit]),
  ]);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (submit.disabled) return;
    form.querySelector(".banner")?.remove();
    const body = {
      title: title.value.trim(),
      description: description.value.trim(),
      owner: {
        name: ownerName.value.trim(),
        email: ownerEmail.value.trim(),
      },
      evidence_expectation: evidence.value.trim(),
      provenance: "user_provided",
    };
    if (dueDate.value) body.due_date = dueDate.value;
    if (
      !body.title
      || !body.description
      || !body.owner.name
      || !body.owner.email
      || !body.evidence_expectation
    ) {
      form.insertBefore(banner("Complete the required fields."), form.querySelector("h3").nextSibling);
      return;
    }
    submit.disabled = true;
    submit.textContent = "Saving…";
    try {
      await api(`/api/v1/workflows/${workflow.id}/requirements`, {
        method: "POST",
        organization: activeOrganization.id,
        body,
      });
    } catch (error) {
      if (error.status === 401) return showExpired();
      submit.disabled = false;
      submit.textContent = "Add requirement";
      form.insertBefore(banner(error.message), form.querySelector("h3").nextSibling);
      return;
    }
    try {
      const fresh = await reloadWorkflow(workflow.id);
      mount(detailView(fresh));
    } catch (error) {
      if (error.status === 401) return showExpired();
      form.insertBefore(
        banner("The requirement was saved. Reload this page to see it."),
        form.querySelector("h3").nextSibling,
      );
    }
  });
  return form;
}

async function reloadWorkflow(workflowId) {
  return api(`/api/v1/workflows/${workflowId}`, {
    organization: activeOrganization.id,
  });
}

function detailView(workflow) {
  const requirements = workflow.requirements.length
    ? el("div", { class: "stack" }, workflow.requirements.map((requirement) => (
      requirementCard(workflow, requirement)
    )))
    : el("div", { class: "card" }, [
        el("h3", { text: "No requirements yet" }),
        el("p", { class: "muted", text: "Add the first requirement for this workflow." }),
      ]);
  return shell(el("section", {}, [
    el("p", {}, [el("a", { href: "#/workflows", text: "Back to workflows" })]),
    el("div", { class: "workflow-identity" }, [
      el("h2", { text: workflow.name }),
      el("div", { class: "meta" }, [
        el("span", {
          class: "chip",
          text: readableLabel(workflow.workflow_type, WORKFLOW_TYPE_LABELS),
        }),
        el("span", {
          class: "chip",
          text: readableLabel(workflow.status, WORKFLOW_STATUS_LABELS),
        }),
      ]),
    ]),
    el("h3", { text: "Requirements" }),
    requirements,
    requirementForm(workflow),
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

function evidenceSummary(item) {
  const description = item?.description || "Evidence recorded.";
  if (item?.identity_verified !== false) return description;
  const name = item.submitter_name
    ? `${item.submitter_name} (unverified)`
    : "Unverified submitter";
  return `${description} — ${name}`;
}

function evidenceFileNote(workflow, requirement, item) {
  const file = item?.file;
  if (!file) return null;
  const kilobytes = Math.max(1, Math.round(Number(file.byte_size) / 1024));
  const note = el("p", {
    class: "muted",
    text: `${file.original_filename} · ${kilobytes} KB.`,
  });
  if (file.scan_status !== "clean") {
    return el("div", {}, [
      note,
      el("p", { class: "muted", text: "Not scanned. Download is unavailable." }),
    ]);
  }
  const button = el("button", {
    class: "button-secondary",
    type: "button",
    text: "Download",
  });
  button.addEventListener("click", async () => {
    try {
      await api(
        `/api/v1/workflows/${workflow.id}/requirements/${requirement.id}/evidence/${item.id}/file`,
      );
      note.textContent = "This file is not available for download.";
    } catch (error) {
      note.textContent = error.message || "This file is not available for download.";
    }
  });
  return el("div", {}, [note, button]);
}

function submissionControls(workflow, requirement) {
  const closed = workflow.status === "cancelled" || requirement.status === "cancelled";
  const holder = el("div", { class: "submission-links" });
  if (!closed) {
    const button = el("button", {
      class: "button-secondary",
      type: "button",
      text: "Create submission link",
    });
    button.addEventListener("click", () => createSubmissionLink(
      workflow,
      requirement,
      button,
      holder,
    ));
    holder.append(el("div", { class: "requirement-actions" }, [button]));
  }
  loadSubmissionLinks(workflow, requirement, holder);
  return holder;
}

async function createSubmissionLink(workflow, requirement, button, holder) {
  button.disabled = true;
  holder.querySelector(".banner")?.remove();
  try {
    const created = await api(
      `/api/v1/workflows/${workflow.id}/requirements/${requirement.id}/submission-links`,
      { method: "POST", organization: activeOrganization.id },
    );
    const issued = holder.querySelector(".issued-link");
    if (issued) issued.remove();
    const url = `${location.origin}${location.pathname}${created.submission_path}`;
    const field = el("input", {
      class: "submission-url",
      readonly: "readonly",
      value: url,
      "aria-label": "Submission link",
    });
    holder.prepend(el("div", { class: "issued-link field" }, [
      el("span", { text: "Copy this link now. It will not be shown again." }),
      field,
    ]));
    field.focus();
    field.select();
    await loadSubmissionLinks(workflow, requirement, holder);
  } catch (error) {
    if (error.status === 401) return showExpired();
    holder.prepend(banner(error.message));
  } finally {
    button.disabled = false;
  }
}

async function loadSubmissionLinks(workflow, requirement, holder) {
  holder.querySelector(".link-list")?.remove();
  try {
    const links = await api(
      `/api/v1/workflows/${workflow.id}/requirements/${requirement.id}/submission-links`,
      { organization: activeOrganization.id },
    );
    if (!links.length) return;
    const list = el("div", { class: "link-list stack" });
    for (const link of links) {
      const expires = new Date(link.expires_at).toLocaleDateString(undefined, {
        year: "numeric",
        month: "long",
        day: "numeric",
      });
      const state = link.revoked_at ? "Revoked" : `Used ${link.use_count} of ${link.max_uses}`;
      const row = el("p", { class: "muted", text: `${state}. Expires ${expires}.` });
      if (!link.revoked_at) {
        const revoke = el("button", {
          class: "button-secondary",
          type: "button",
          text: "Revoke link",
        });
        revoke.addEventListener("click", async () => {
          revoke.disabled = true;
          try {
            await api(
              `/api/v1/workflows/${workflow.id}/requirements/${requirement.id}/submission-links/${link.id}/revoke`,
              { method: "POST", organization: activeOrganization.id },
            );
            await loadSubmissionLinks(workflow, requirement, holder);
          } catch (error) {
            if (error.status === 401) return showExpired();
            revoke.disabled = false;
            holder.prepend(banner(error.message));
          }
        });
        list.append(el("div", {}, [row, revoke]));
      } else {
        list.append(row);
      }
    }
    holder.append(list);
  } catch (error) {
    if (error.status === 401) return showExpired();
    holder.append(banner(error.message));
  }
}

async function publicApi(path, token, options = {}) {
  const headers = {
    Accept: "application/json",
    "X-Submission-Token": token,
  };
  let body;
  if (options.form) {
    body = options.form;
  } else if (options.body) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(options.body);
  }
  const response = await fetch(path, {
    method: options.method || "GET",
    headers,
    body,
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(messageFrom(payload, response.status));
    error.status = response.status;
    throw error;
  }
  return payload;
}

function publicSubmissionView(token) {
  applyBrand(DEFAULT_BRAND);
  const section = el("section", { class: "card" }, [
    el("h2", { text: "Submit evidence" }),
    el("p", { class: "muted", text: "Loading the request…" }),
  ]);
  publicApi("/api/v1/submission-context", token)
    .then((context) => mount(publicSubmissionForm(token, context)))
    .catch((error) => {
      section.replaceChildren(
        el("h2", { text: "Submit evidence" }),
        banner(error.message || "This submission link is not valid."),
      );
    });
  return publicShell(section);
}

function publicSubmissionForm(token, context) {
  const description = el("textarea", {
    id: "submission-description",
    required: "required",
    maxlength: "4000",
  });
  const reference = el("input", {
    id: "submission-reference",
    type: "url",
    maxlength: "1000",
    placeholder: "https://",
  });
  const file = el("input", {
    id: "submission-file",
    type: "file",
    accept: ".pdf,.docx,.xlsx,.csv,.png,.jpg,.jpeg",
  });
  const name = el("input", {
    id: "submission-name",
    maxlength: "200",
  });
  const submit = el("button", { class: "button", type: "submit", text: "Submit evidence" });
  const form = el("form", { class: "card" }, [
    el("p", { class: "eyebrow", text: context.organization_name }),
    el("h2", { text: context.requirement_title }),
    el("p", { text: context.evidence_expectation }),
    el("label", { class: "field" }, [
      el("span", { text: "What are you submitting?" }),
      description,
    ]),
    el("label", { class: "field" }, [
      el("span", { text: "File" }),
      file,
    ]),
    el("p", {
      class: "muted",
      text: "PDF, DOCX, XLSX, CSV, PNG, or JPEG. Maximum 10 MB. Spreadsheet formulas and CSV values are stored as data and are not run.",
    }),
    el("label", { class: "field" }, [
      el("span", { text: "HTTPS link" }),
      reference,
    ]),
    el("label", { class: "field" }, [
      el("span", { text: "Your name (optional)" }),
      name,
    ]),
    el("p", {
      class: "muted",
      text: "Sending this records the evidence for review. It does not complete the requirement.",
    }),
    submit,
  ]);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (submit.disabled) return;
    form.querySelector(".banner")?.remove();
    const selected = file.files && file.files[0];
    const link = reference.value.trim();
    if (!selected && !link) {
      form.prepend(banner("Add a file or an HTTPS link."));
      return;
    }
    submit.disabled = true;
    submit.textContent = "Submitting…";
    const formData = new FormData();
    formData.append("description", description.value.trim());
    if (link) formData.append("reference", link);
    if (name.value.trim()) formData.append("submitter_name", name.value.trim());
    if (selected) formData.append("file", selected);
    try {
      await publicApi("/api/v1/submissions", token, {
        method: "POST",
        form: formData,
      });
      description.value = "";
      reference.value = "";
      name.value = "";
      file.value = "";
      form.prepend(el("div", {
        class: "banner",
        role: "status",
        dataset: { tone: "ok" },
        text: "Submission received. It is recorded for review and does not complete the requirement.",
      }));
    } catch (error) {
      form.prepend(banner(error.message));
    } finally {
      submit.disabled = false;
      submit.textContent = "Submit evidence";
    }
  });
  return publicShell(form);
}

function publicShell(content) {
  return el("div", {}, [
    el("header", { class: "app-header" }, [
      el("div", { class: "brand" }, [
        brandMark(DEFAULT_BRAND),
        el("h1", { text: "CommitmentOps" }),
      ]),
    ]),
    el("main", { class: "page" }, [content]),
  ]);
}

function parseRoute() {
  const hash = location.hash.replace(/^#/, "") || "/workflows";
  const parts = hash.split("/").filter(Boolean);
  if (parts[0] === "s" && parts[1]) return { name: "submit", token: decodeURIComponent(parts[1]) };
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
  if (route.name === "submit") {
    mount(publicSubmissionView(route.token));
    return;
  }
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
