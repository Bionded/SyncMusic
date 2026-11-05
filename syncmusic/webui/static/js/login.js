// The login screen: shown instead of the app when the optional login is on and this browser has no
// session. A successful login reloads the page; api.js reloads on a 401 `auth_required` (session ended elsewhere).
import { html, setHtml } from "./dom.js";
import { t, addStrings } from "./i18n.js";
import { post } from "./api.js";
import { logo } from "./icons.js";
import { errorMessage } from "./ui.js";

addStrings({
  "login.title": "Sign in to SyncMusic",
  "login.password": "Password",
  "login.submit": "Sign in",
  "login.busy": "Signing in…",
  "login.wrong": "Wrong password.",
  "login.lost": "Lost the password? On the host, run syncmusic set-password.",
});

export function showLogin() {
  document.body.classList.add("login-mode");
  setHtml(document.body, html`<main class="login" id="login">
    <form class="card login-card" data-login>
      <div class="login-brand">${logo(40)}<h1>${t("login.title")}</h1></div>
      <div class="field"><label for="login-pw">${t("login.password")}</label>
        <input id="login-pw" type="password" name="password" autocomplete="current-password" required autofocus></div>
      <p class="hint warn" data-login-error role="alert" hidden></p>
      <button type="submit" class="btn primary" data-login-submit>${t("login.submit")}</button>
      <p class="muted login-lost">${t("login.lost")}</p>
    </form></main>`);
  const form = document.querySelector("[data-login]");
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const btn = form.querySelector("[data-login-submit]"), err = form.querySelector("[data-login-error]");
    btn.disabled = true;
    btn.textContent = t("login.busy");
    err.hidden = true;
    try {
      await post("/auth/login", { password: form.password.value });
      location.reload();
    } catch (x) {
      err.textContent = x.code === "wrong_password" ? t("login.wrong") : errorMessage(x);
      err.hidden = false;
      btn.disabled = false;
      btn.textContent = t("login.submit");
      form.password.select();
    }
  });
}
