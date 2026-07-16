/* Workbench auth guard: httpOnly cookie session check. */
(function () {
  function redirect() {
    var here = (location.pathname.split("/").pop() || "dashboard").replace(/\.html$/i, "");
    location.replace("../login?redirect=" + encodeURIComponent(here));
  }

  fetch("/api/auth/me", { credentials: "same-origin", cache: "no-store" })
    .then(function (res) {
      if (!res.ok) redirect();
    })
    .catch(redirect);
})();
