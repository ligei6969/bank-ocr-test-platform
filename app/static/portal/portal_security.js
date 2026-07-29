"use strict";

(() => {
  let csrfToken = null;
  let tokenPromise = null;

  async function getCsrfToken() {
    if (csrfToken) {
      return csrfToken;
    }
    if (!tokenPromise) {
      tokenPromise = fetch("/csrf-token", {
        cache: "no-store",
        credentials: "same-origin",
      })
        .then(async (response) => {
          if (!response.ok) {
            throw new Error("CSRF token request failed");
          }
          const data = await response.json();
          if (!data || typeof data.csrf_token !== "string" || !data.csrf_token) {
            throw new Error("CSRF token response is invalid");
          }
          csrfToken = data.csrf_token;
          return csrfToken;
        })
        .catch((error) => {
          tokenPromise = null;
          throw error;
        });
    }
    return tokenPromise;
  }

  async function fetchWithCsrf(resource, options = {}) {
    const token = await getCsrfToken();
    const headers = new Headers(options.headers || {});
    headers.set("X-CSRF-Token", token);
    return fetch(resource, {
      ...options,
      credentials: "same-origin",
      headers,
    });
  }

  function showLogoutMessage(form, message) {
    let status = form.nextElementSibling;
    if (!status || !status.classList.contains("logout-security-message")) {
      status = document.createElement("p");
      status.className = "logout-security-message";
      status.setAttribute("role", "alert");
      form.insertAdjacentElement("afterend", status);
    }
    status.textContent = message;
  }

  async function initializeLogoutForms() {
    const forms = [...document.querySelectorAll('form[action="/logout"]')];
    if (forms.length === 0) {
      return;
    }

    for (const form of forms) {
      const button = form.querySelector('button[type="submit"]');
      if (button) {
        button.disabled = true;
      }
      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        if (!csrfToken) {
          showLogoutMessage(
            form,
            "安全校验初始化失败，请刷新页面重试。",
          );
          return;
        }
        if (button) {
          button.disabled = true;
        }
        try {
          const response = await fetchWithCsrf("/logout", {
            method: "POST",
          });
          if (response.status === 403) {
            showLogoutMessage(
              form,
              "请求安全校验失败，请刷新页面后重试。",
            );
            return;
          }
          if (!response.ok && !response.redirected) {
            showLogoutMessage(form, "退出失败，请稍后重试。");
            return;
          }
          window.location.href = "/login";
        } catch {
          showLogoutMessage(
            form,
            "安全校验初始化失败，请刷新页面重试。",
          );
        } finally {
          if (button) {
            button.disabled = false;
          }
        }
      });
    }

    try {
      await getCsrfToken();
      for (const form of forms) {
        const button = form.querySelector('button[type="submit"]');
        if (button) {
          button.disabled = false;
        }
      }
    } catch {
      for (const form of forms) {
        showLogoutMessage(
          form,
          "安全校验初始化失败，请刷新页面重试。",
        );
      }
    }
  }

  window.portalSecurity = Object.freeze({
    fetchWithCsrf,
    getCsrfToken,
  });

  document.addEventListener("DOMContentLoaded", initializeLogoutForms);
})();
