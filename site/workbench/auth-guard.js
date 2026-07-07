/* Workbench auth guard: legacy bearer-token fallback plus httpOnly cookie session. */
(function () {
  function redirect() {
    var here = (location.pathname.split("/").pop() || "dashboard").replace(/\.html$/i, "");
    location.replace("../login?redirect=" + encodeURIComponent(here));
  }

  try {
    if (localStorage.getItem("hq_token")) return;
  } catch (e) {}

  fetch("/api/auth/me", { credentials: "same-origin", cache: "no-store" })
    .then(function (res) {
      if (!res.ok) redirect();
    })
    .catch(redirect);
})();
