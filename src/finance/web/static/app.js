(function () {
  "use strict";

  function csrfToken() {
    const meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.getAttribute("content") || "" : "";
  }

  function splitSelectors(value, trigger) {
    if (!value) return [];
    const nodes = [];
    for (const raw of value.split(",")) {
      const selector = raw.trim();
      if (!selector) continue;
      if (selector === "this") {
        nodes.push(trigger);
      } else {
        nodes.push(...document.querySelectorAll(selector));
      }
    }
    return nodes;
  }

  function formBody(trigger) {
    if (trigger instanceof HTMLFormElement) return new FormData(trigger);
    if (trigger.form instanceof HTMLFormElement) return new FormData(trigger.form);
    return undefined;
  }

  function targetFor(trigger) {
    const selector = trigger.getAttribute("hx-target");
    return selector ? document.querySelector(selector) : trigger;
  }

  function swapInto(target, html, mode) {
    if (!target) return;
    if (mode === "delete") {
      target.remove();
    } else if (mode === "outerHTML") {
      target.outerHTML = html;
    } else {
      target.innerHTML = html;
    }
  }

  async function runHxPost(trigger, event) {
    if (event) event.preventDefault();
    const message = trigger.getAttribute("hx-confirm");
    if (message && !window.confirm(message)) return;

    const url = trigger.getAttribute("hx-post");
    if (!url) return;
    const target = targetFor(trigger);
    const swap = trigger.getAttribute("hx-swap") || "innerHTML";
    const indicators = splitSelectors(trigger.getAttribute("hx-indicator"), trigger);
    const disabled = splitSelectors(trigger.getAttribute("hx-disabled-elt"), trigger);
    indicators.push(trigger);

    for (const node of indicators) node.classList.add("htmx-request");
    for (const node of disabled) node.setAttribute("disabled", "disabled");

    try {
      const response = await fetch(url, {
        method: "POST",
        headers: {
          "X-CSRF-Token": csrfToken(),
          "X-Requested-With": "XMLHttpRequest",
        },
        body: formBody(trigger),
        credentials: "same-origin",
      });
      if (response.status === 204) {
        if (swap === "delete" && target) target.remove();
      } else {
        const html = await response.text();
        swapInto(target, html, swap);
      }
      const hook = trigger.getAttribute("hx-on::after-request") || "";
      if (response.ok && hook.includes("this.reset") && trigger instanceof HTMLFormElement) {
        trigger.reset();
      }
    } catch (error) {
      if (target) {
        target.innerHTML =
          '<div class="bg-red-50 border border-red-200 text-red-800 rounded px-3 py-2 text-sm">' +
          "Request failed: " +
          String(error).replace(/[<>&]/g, "") +
          "</div>";
      }
    } finally {
      for (const node of indicators) node.classList.remove("htmx-request");
      for (const node of disabled) node.removeAttribute("disabled");
      bindDynamic(document);
    }
  }

  async function runHxGet(trigger) {
    const url = trigger.getAttribute("hx-get");
    if (!url) return;
    const target = targetFor(trigger);
    const swap = trigger.getAttribute("hx-swap") || "innerHTML";
    try {
      const response = await fetch(url, {
        headers: {"X-Requested-With": "XMLHttpRequest"},
        credentials: "same-origin",
      });
      const html = await response.text();
      if (response.ok) swapInto(target, html, swap);
    } catch {
      // Polling widgets are advisory. Keep the last visible state on failure.
    }
  }

  function bindHx(root) {
    for (const element of root.querySelectorAll("[hx-post]")) {
      if (element.dataset.financeBoundPost === "1") continue;
      element.dataset.financeBoundPost = "1";
      if (element instanceof HTMLFormElement) {
        element.addEventListener("submit", (event) => runHxPost(element, event));
      } else {
        element.addEventListener("click", (event) => runHxPost(element, event));
      }
    }

    for (const element of root.querySelectorAll("[hx-get]")) {
      if (element.dataset.financeBoundGet === "1") continue;
      element.dataset.financeBoundGet = "1";
      const trigger = element.getAttribute("hx-trigger") || "";
      if (trigger.includes("load")) runHxGet(element);
      const match = trigger.match(/every\s+(\d+)s/);
      if (match) window.setInterval(() => runHxGet(element), Number(match[1]) * 1000);
    }
  }

  function bindReloads(root) {
    for (const button of root.querySelectorAll("[data-reload-button]")) {
      if (button.dataset.financeReloadBound === "1") continue;
      button.dataset.financeReloadBound = "1";
      button.addEventListener("click", () => window.location.reload());
    }

    for (const panel of root.querySelectorAll("[data-auto-reload]")) {
      if (panel.dataset.financeAutoReloadBound === "1") continue;
      panel.dataset.financeAutoReloadBound = "1";
      let remaining = Number(panel.getAttribute("data-auto-reload") || "0");
      const countdown = panel.querySelector("[data-countdown]");
      const timer = window.setInterval(() => {
        remaining -= 1;
        if (countdown) countdown.textContent = String(Math.max(remaining, 0));
        if (remaining <= 0) {
          window.clearInterval(timer);
          window.location.reload();
        }
      }, 1000);
    }
  }

  function drawMonthlyChart() {
    const canvas = document.getElementById("monthlyChart");
    if (!(canvas instanceof HTMLCanvasElement)) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const months = JSON.parse(canvas.dataset.months || "[]");
    const spent = JSON.parse(canvas.dataset.spent || "[]");
    const income = JSON.parse(canvas.dataset.income || "[]");
    const dpr = window.devicePixelRatio || 1;
    const rect = canvas.getBoundingClientRect();
    const width = Math.max(320, rect.width || canvas.parentElement.clientWidth || 480);
    const height = Number(canvas.getAttribute("height") || "180");
    canvas.width = width * dpr;
    canvas.height = height * dpr;
    canvas.style.width = width + "px";
    canvas.style.height = height + "px";
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, width, height);

    const values = spent.concat(income).map(Number);
    const max = Math.max(1, ...values);
    const padding = 28;
    const plotHeight = height - padding * 2;
    const groupWidth = (width - padding * 2) / Math.max(1, months.length);
    ctx.font = "12px system-ui, sans-serif";
    ctx.fillStyle = "#737373";
    ctx.textAlign = "center";

    months.forEach((month, index) => {
      const x = padding + index * groupWidth + groupWidth * 0.15;
      const barWidth = Math.max(8, groupWidth * 0.28);
      const spentHeight = (Number(spent[index] || 0) / max) * plotHeight;
      const incomeHeight = (Number(income[index] || 0) / max) * plotHeight;
      ctx.fillStyle = "rgba(220,38,38,0.75)";
      ctx.fillRect(x, height - padding - spentHeight, barWidth, spentHeight);
      ctx.fillStyle = "rgba(21,128,61,0.75)";
      ctx.fillRect(x + barWidth + 4, height - padding - incomeHeight, barWidth, incomeHeight);
      ctx.fillStyle = "#737373";
      ctx.fillText(String(month), x + barWidth, height - 8);
    });
  }

  function bindDynamic(root) {
    bindHx(root);
    bindReloads(root);
  }

  document.addEventListener("DOMContentLoaded", () => {
    bindDynamic(document);
    drawMonthlyChart();
  });
})();
