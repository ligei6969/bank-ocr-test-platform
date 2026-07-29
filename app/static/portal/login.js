"use strict";

document.addEventListener("DOMContentLoaded", () => {
  const form = document.querySelector("#loginForm");
  const submitButton = document.querySelector("#loginSubmit");
  const errorMessage = document.querySelector("#loginError");
  const query = new URLSearchParams(window.location.search);
  let submitting = false;
  let securityReady = false;

  if (query.get("error") === "1") {
    errorMessage.textContent = "用户名或密码错误。";
    errorMessage.hidden = false;
  }

  window.portalSecurity
    .getCsrfToken()
    .then(() => {
      securityReady = true;
      submitButton.disabled = false;
    })
    .catch(() => {
      errorMessage.textContent = "安全校验初始化失败，请刷新页面重试。";
      errorMessage.hidden = false;
    });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (submitting || !securityReady) {
      if (!securityReady) {
        errorMessage.textContent = "安全校验初始化失败，请刷新页面重试。";
        errorMessage.hidden = false;
      }
      return;
    }

    submitting = true;
    submitButton.disabled = true;
    submitButton.textContent = "登录中...";
    errorMessage.hidden = true;

    try {
      const response = await window.portalSecurity.fetchWithCsrf("/login", {
        method: "POST",
        body: new FormData(form),
      });
      if (response.status === 403) {
        errorMessage.textContent = "请求安全校验失败，请刷新页面后重试。";
        errorMessage.hidden = false;
        return;
      }
      if (response.redirected) {
        window.location.href = response.url;
        return;
      }
      errorMessage.textContent = "登录请求失败，请稍后重试。";
      errorMessage.hidden = false;
    } catch {
      errorMessage.textContent = "安全校验初始化失败，请刷新页面重试。";
      errorMessage.hidden = false;
    } finally {
      submitting = false;
      submitButton.disabled = !securityReady;
      submitButton.textContent = "登录";
    }
  });
});
