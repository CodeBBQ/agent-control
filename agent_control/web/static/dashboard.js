"use strict";

function relativeTime(date) {
  const seconds = Math.round((date.getTime() - Date.now()) / 1000);
  const formatter = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
  const units = [
    ["day", 86400],
    ["hour", 3600],
    ["minute", 60],
    ["second", 1],
  ];
  for (const [unit, size] of units) {
    if (Math.abs(seconds) >= size || unit === "second") {
      return formatter.format(Math.round(seconds / size), unit);
    }
  }
}

async function operatorPost(path, payload) {
  const token = document.querySelector('meta[name="agent-control-csrf"]')?.content;
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Agent-Control-CSRF": token || "" },
    body: JSON.stringify(payload),
    credentials: "same-origin",
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || "Operation failed");
  return data;
}

document.addEventListener("DOMContentLoaded", () => {
  const updatePanel = document.querySelector("[data-dashboard-update]");
  if (updatePanel) {
    const form = updatePanel.querySelector("[data-update-form]");
    const select = form.querySelector('select[name="branch"]');
    const button = form.querySelector('button[type="submit"]');
    const branchName = updatePanel.querySelector("[data-current-branch]");
    const status = updatePanel.querySelector("[data-update-status]");
    const prForm = updatePanel.querySelector("[data-pr-form]");
    const prButton = prForm.querySelector('button[type="submit"]');
    const prMerge = prForm.querySelector('input[name="merge"]');
    const prPreview = updatePanel.querySelector("[data-pr-preview]");
    const prStatus = updatePanel.querySelector("[data-pr-action-status]");
    let pr = null;
    let prLookupId = 0;
    let prBusy = false;

    const previewPR = async () => {
      const lookup = ++prLookupId;
      pr = null;
      prButton.disabled = true;
      prMerge.checked = false;
      const branch = select.value;
      if (!branch) {
        prPreview.textContent = "Select a branch to check for an open PR.";
        return;
      }
      prPreview.textContent = "Looking up PR for " + branch + "…";
      try {
        const response = await fetch(
          "/maintenance/pr?branch=" + encodeURIComponent(branch), { cache: "no-store" },
        );
        const item = await response.json();
        if (lookup !== prLookupId || branch !== select.value) return;
        if (!response.ok) throw new Error(item.detail || "No matching open PR");
        pr = item;
        prPreview.textContent = "#" + item.number + " · " + item.title +
          " · " + (item.draft ? "Draft" : "Ready") +
          " · HEAD " + item.head.slice(0, 12) +
          " · " + (item.checks_green ? "checks passed" : "checks not verified");
        prButton.disabled = prBusy;
      } catch (error) {
        if (lookup !== prLookupId) return;
        prPreview.textContent = error.message;
      }
    };
    select.addEventListener("change", () => {
      button.disabled = !select.value || polling || prBusy;
      void previewPR();
    });
    prForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const current = pr;
      if (!current || prBusy || polling || select.value !== current.branch) return;
      const merge = prMerge.checked;
      if (merge && !current.checks_green) {
        prStatus.textContent = "Merge requires successful current-head checks.";
        return;
      }
      const question = merge
        ? "Mark PR #" + current.number + " ready, MERGE into " + current.base +
          " and DELETE its remote branch?\nHEAD: " + current.head
        : "Mark PR #" + current.number + " ready?\nHEAD: " + current.head;
      if (!window.confirm(question)) return;
      prBusy = true;
      prButton.disabled = true;
      button.disabled = true;
      prStatus.textContent = merge ? "Marking ready, then merging…" : "Marking ready…";
      try {
        const result = await operatorPost("/maintenance/pr", {
          branch: current.branch, number: current.number, head: current.head, merge,
          confirmation: (merge ? "merge:" : "ready:") +
            current.number + ":" + current.head.slice(0, 12),
        });
        prStatus.textContent = result.status === "merged"
          ? "PR #" + result.number + " merged; remote branch deletion requested."
          : "PR #" + result.number + " marked ready.";
        await checkBranches();
      } catch (error) {
        prStatus.textContent = error.message +
          " Check the PR on GitHub: ready may already have succeeded.";
        await previewPR();
      } finally {
        prBusy = false;
        button.disabled = !select.value;
        prButton.disabled = !pr || polling;
      }
    });
    let polling = false;
    const checkBranches = async () => {
      button.disabled = true;
      try {
        const response = await fetch("/maintenance/branches", { cache: "no-store" });
        const state = await response.json();
        if (!response.ok) throw new Error(state.detail || "Branch list unavailable");
        select.replaceChildren();
        for (const branch of state.branches) {
          const option = document.createElement("option");
          option.value = branch;
          option.textContent = branch;
          select.append(option);
        }
        select.value = state.branches.includes(state.current) ? state.current : "";
        branchName.textContent = state.current;
        button.disabled = !select.value || prBusy;
        status.textContent = "";
        await previewPR();
      } catch (error) {
        branchName.textContent = "unavailable";
        status.textContent = error.message;
      }
    };
    updatePanel.addEventListener("toggle", () => {
      if (updatePanel.open) void checkBranches();
    });
    document.addEventListener("pointerdown", (event) => {
      if (updatePanel.open && !updatePanel.contains(event.target)) updatePanel.open = false;
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && updatePanel.open) updatePanel.open = false;
    });
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (polling || !select.value) return;
      const branch = select.value;
      if (!window.confirm("Update Agent Control to " + branch + " and restart the managed service?")) {
        return;
      }
      button.disabled = true;
      prButton.disabled = true;
      try {
        const queued = await operatorPost("/maintenance/update", { branch });
        polling = true;
        status.textContent = "Update queued for " + branch + "…";
        let tries = 0;
        const poll = async () => {
          if (++tries > 180) {
            status.textContent = "Timed out waiting for update. Inspect the managed log.";
            polling = false;
            button.disabled = false;
            return;
          }
          try {
            const response = await fetch("/maintenance/update-status", { cache: "no-store" });
            if (!response.ok) throw new Error("Service restarting");
            const state = await response.json();
            if (state.id !== queued.id) throw new Error("Waiting for requested update");
            if (state.status === "complete") {
              window.location.reload();
              return;
            }
            if (state.status === "failed") {
              status.textContent = state.message || "Update failed. Inspect the update worker log.";
              polling = false;
              button.disabled = false;
              return;
            }
            status.textContent = "Update: " + state.status + "…";
          } catch (_) {
            status.textContent = "Waiting for restarted Agent Control…";
          }
          window.setTimeout(poll, 1000);
        };
        window.setTimeout(poll, 800);
      } catch (error) {
        polling = false;
        button.disabled = false;
        prButton.disabled = !pr || prBusy;
        status.textContent = error.message;
      }
    });
  }

  const form = document.querySelector("#settings-form");
  if (form) form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const feedback = document.querySelector("#settings-feedback");
    const button = form.querySelector('button[type="submit"]');
    button.disabled = true;
    feedback.textContent = "Saving…";
    try {
      await operatorPost("/settings", {
        revision: form.dataset.revision,
        owner_login: document.querySelector("#owner-login").value.trim() || null,
        repositories: document.querySelector("#repositories").value,
      });
      window.location.reload();
    } catch (error) {
      feedback.textContent = error.message;
      button.disabled = false;
    }
  });
  const shell = document.querySelector("[data-app-shell]");
  const rail = document.querySelector("[data-inspector-rail]");
  if (shell && rail) {
    const buttons = [...rail.querySelectorAll("[data-inspector-tab]")];
    const setInspector = (expanded) => {
      shell.classList.toggle("inspector-open", expanded);
      for (const button of buttons) button.setAttribute("aria-expanded", String(expanded));
      try { window.sessionStorage.setItem("agent-control-inspector-open", expanded ? "1" : "0"); }
      catch (_) { /* Session storage may be disabled; layout still works. */ }
    };
    let initial = false;
    try { initial = window.sessionStorage.getItem("agent-control-inspector-open") === "1"; }
    catch (_) { /* Use collapsed state when storage is unavailable. */ }
    setInspector(initial);
    for (const button of buttons) button.addEventListener("click", () => {
      setInspector(true);
      const section = rail.querySelector('[data-inspector-section="' + button.dataset.inspectorTab + '"]');
      if (section) {
        section.open = true;
        section.scrollIntoView({ block: "nearest" });
      }
    });
    rail.querySelector("[data-inspector-close]")?.addEventListener("click", () => setInspector(false));
    const roadmapToggle = document.querySelector("[data-roadmap-toggle]");
    const setRoadmap = (expanded) => {
      shell.classList.toggle("roadmap-mobile-open", expanded);
      roadmapToggle?.setAttribute("aria-expanded", String(expanded));
    };
    roadmapToggle?.addEventListener("click", () => {
      setRoadmap(!shell.classList.contains("roadmap-mobile-open"));
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && shell.classList.contains("roadmap-mobile-open")) {
        setRoadmap(false);
        roadmapToggle?.focus();
      }
    });
  }

  const toolbar = document.querySelector("[data-workstream-toolbar]");
  if (toolbar) {
    const list = document.querySelector("[data-workstream-list]");
    const cards = [...list.querySelectorAll("[data-workstream-card]")];
    const empty = document.querySelector("[data-filter-empty]");
    const filterButtons = [...toolbar.querySelectorAll("[data-workstream-filter]")];
    const sortSelect = toolbar.querySelector("[data-workstream-sort]");
    const matches = (card, filter) => {
      if (filter === "active") return card.dataset.bucket === "in_progress";
      if (filter === "blocked") return card.dataset.blocked === "true";
      if (filter === "review") return card.dataset.review === "true";
      if (filter === "closed") return card.dataset.bucket === "closed";
      return true;
    };
    for (const filter of ["all", "active", "blocked", "review", "closed"]) {
      const count = toolbar.querySelector('[data-filter-count="' + filter + '"]');
      if (count) count.textContent = "(" + cards.filter((card) => matches(card, filter)).length + ")";
    }
    const order = new Map(cards.map((card, index) => [card, index]));
    const numeric = (card, name) => Number(card.dataset[name]);
    let currentFilter = "all";
    const refresh = () => {
      const sort = sortSelect.value;
      const ordered = [...cards].sort((a, b) => {
        if (sort === "recent") {
          return b.dataset.lastActivity.localeCompare(a.dataset.lastActivity) ||
            order.get(a) - order.get(b);
        }
        if (sort === "milestone") {
          return a.dataset.milestone.localeCompare(b.dataset.milestone) ||
            numeric(a, "issue") - numeric(b, "issue");
        }
        return numeric(a, "priority") - numeric(b, "priority") ||
          b.dataset.lastActivity.localeCompare(a.dataset.lastActivity) ||
          order.get(a) - order.get(b);
      });
      list.replaceChildren(...ordered);
      let visible = 0;
      for (const card of cards) {
        card.hidden = !matches(card, currentFilter);
        if (!card.hidden) visible += 1;
      }
      if (empty) empty.hidden = visible > 0 || cards.length === 0;
    };
    for (const button of filterButtons) {
      button.addEventListener("click", () => {
        currentFilter = button.dataset.workstreamFilter;
        for (const item of filterButtons) {
          item.setAttribute("aria-pressed", String(item === button));
        }
        refresh();
      });
    }
    sortSelect.addEventListener("change", refresh);
    refresh();
  }

  const projectPicker = document.querySelector("[data-project-picker]");
  if (projectPicker) {
    document.addEventListener("pointerdown", (event) => {
      if (projectPicker.open && !projectPicker.contains(event.target)) projectPicker.open = false;
    });
    projectPicker.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && projectPicker.open) {
        event.preventDefault();
        projectPicker.open = false;
        projectPicker.querySelector("summary")?.focus();
      }
    });
  }

  for (const form of document.querySelectorAll("[data-session-form]")) {
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const input = form.querySelector('input[name="url"]');
      const feedback = form.querySelector("[data-session-feedback]");
      const button = form.querySelector('button[type="submit"]');
      button.disabled = true;
      feedback.textContent = "Saving locally…";
      try {
        await operatorPost("/workstreams/" + encodeURIComponent(form.dataset.workstream) + "/sessions", {
          url: input.value.trim(), action: "add",
        });
        input.value = "";
        window.location.reload();
      } catch (error) {
        feedback.textContent = error.message;
        button.disabled = false;
      }
    });
  }
  for (const button of document.querySelectorAll("[data-session-remove]")) {
    button.addEventListener("click", async () => {
      if (!window.confirm("Remove this locally stored session link?")) return;
      button.disabled = true;
      try {
        await operatorPost("/workstreams/" + encodeURIComponent(button.dataset.workstream) + "/sessions", {
          url: button.dataset.url, action: "remove",
        });
        window.location.reload();
      } catch (error) {
        button.disabled = false;
        window.alert(error.message);
      }
    });
  }

  for (const element of document.querySelectorAll("[data-relative-time]")) {
    const value = new Date(element.getAttribute("datetime"));
    if (!Number.isNaN(value.getTime())) {
      element.textContent = relativeTime(value);
      element.title = value.toLocaleString();
    }
  }
});

// Read-only pages track the persisted service snapshot; never reload settings forms.
if (/^\/$|^\/(?:projects|workstreams|agents|owner-actions|failures)(?:\/|$)/.test(window.location.pathname)) {
  window.setInterval(() => {
    if (document.visibilityState === "visible") window.location.reload();
  }, 120000);
}
