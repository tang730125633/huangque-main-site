/* 黄雀 duotone-mini 图标集 — 功能位用（16-24px）：白线 1.7 + 单一强调色（__ACC__ 占位，渲染时注入），无网点。
   骨架与 assets/icons-duotone/hq_*.svg 同源，是其小尺寸简化变体。 */
(function () {
  var L = 'stroke="#eaf1fa" stroke-width="1.7"';
  var A = 'stroke="__ACC__" stroke-width="1.7"';
  var D = {
    home: '<path d="M3 11l9-8 9 8M5 10v10h14V10" ' + L + '/><path d="M10 20v-6h4v6" ' + A + '/>',
    sparkles: '<path d="M11.5 3.4l1.9 5.1 5.1 1.9-5.1 1.9-1.9 5.1-1.9-5.1-5.1-1.9 5.1-1.9z" ' + L + '/><path d="M18.8 15.4l.6 1.7 1.7.6-1.7.6-.6 1.7-.6-1.7-1.7-.6 1.7-.6z" fill="__ACC__"/>',
    search: '<circle cx="11" cy="11" r="7" ' + L + '/><path d="M21 21l-4-4" ' + A + '/>',
    link: '<path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71" ' + A + '/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71" ' + L + '/>',
    image: '<rect x="3" y="3" width="18" height="18" rx="2" ' + L + '/><circle cx="8.5" cy="8.5" r="1.6" fill="__ACC__"/><path d="M21 15l-5-5L5 21" ' + L + '/>',
    video: '<rect x="2" y="6" width="14" height="12" rx="2" ' + L + '/><path d="M22 8l-6 4 6 4z" fill="__ACC__"/>',
    mic: '<rect x="9" y="2" width="6" height="12" rx="3" ' + L + '/><path d="M5 10a7 7 0 0 0 14 0" ' + A + '/><path d="M12 17v4" ' + L + '/>',
    edit: '<path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z" ' + L + '/><path d="M12 20h9" ' + A + '/>',
    layers: '<path d="M12 2l9 5-9 5-9-5z" ' + A + '/><path d="M3 12l9 5 9-5M3 17l9 5 9-5" ' + L + '/>',
    folder: '<path d="M3 7a2 2 0 0 1 2-2h4l2 3h8a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" ' + L + '/><path d="M7 13.5h4" ' + A + '/>',
    coins: '<circle cx="8" cy="8" r="6" ' + L + '/><path d="M18.1 6.6A6 6 0 1 1 9.9 18.5" ' + A + '/><path d="M7 6h1v4" ' + L + '/><path d="M16.7 12.6h1v4" ' + A + '/>',
    gear: '<circle cx="12" cy="12" r="6.6" ' + L + '/><circle cx="12" cy="12" r="2.6" ' + A + '/><path d="M12 2.8v2.6M12 18.6v2.6M2.8 12h2.6M18.6 12h2.6M5.5 5.5l1.8 1.8M16.7 16.7l1.8 1.8M18.5 5.5l-1.8 1.8M7.3 16.7l-1.8 1.8" ' + L + '/>',
    bell: '<path d="M18 8a6 6 0 1 0-12 0c0 7-3 9-3 9h18s-3-2-3-9" ' + L + '/><path d="M13.7 21a2 2 0 0 1-3.4 0" ' + L + '/><circle cx="18" cy="5" r="1.7" fill="__ACC__"/>',
    message: '<path d="M21 11.5a8.4 8.4 0 0 1-9 8 9 9 0 0 1-4-1l-5 1 1-4a8.4 8.4 0 0 1-1-4 8.4 8.4 0 0 1 9-8 8.4 8.4 0 0 1 9 8z" ' + L + '/><circle cx="8.6" cy="11.5" r="1" fill="__ACC__"/><circle cx="12" cy="11.5" r="1" fill="__ACC__"/><circle cx="15.4" cy="11.5" r="1" fill="__ACC__"/>',
    checkCircle: '<path d="M22 11.08V12a10 10 0 1 1-5.93-9.14" ' + L + '/><path d="M22 4L12 14.01l-3-3" ' + A + '/>',
    alert: '<path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z" ' + L + '/><path d="M12 9v4M12 17h.01" ' + A + '/>',
    clock: '<circle cx="12" cy="12" r="9" ' + L + '/><path d="M12 7v5l3 2" ' + A + '/>',
    send: '<path d="M21 3.4L10.4 14M21 3.4l-6.7 17.2-3.9-6.6-6.6-3.9z" ' + L + '/><path d="M4.4 7h2.8M3 10.2h2" ' + A + '/>',
    trend: '<path d="M3 17l6-6 4 4 7-7" ' + L + '/><path d="M15 8h6v6" ' + A + '/>',
    users: '<circle cx="9" cy="7" r="4" ' + L + '/><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2" ' + L + '/><path d="M16 3.13a4 4 0 0 1 0 7.75M23 21v-2a4 4 0 0 0-3-3.87" ' + A + '/>',
    userPlus: '<circle cx="9" cy="7" r="4" ' + L + '/><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2" ' + L + '/><path d="M19 8v6M22 11h-6" ' + A + '/>',
    sliders: '<path d="M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3" ' + L + '/><path d="M1 14h6M9 8h6M17 16h6" ' + A + '/>',
    refresh: '<path d="M23 4v6h-6" ' + A + '/><path d="M1 20v-6h6" ' + L + '/><path d="M3.5 9a9 9 0 0 1 14.8-3.4L23 10" ' + A + '/><path d="M1 14l4.7 4.4A9 9 0 0 0 20.5 15" ' + L + '/>',
    user: '<circle cx="12" cy="8" r="4" ' + L + '/><path d="M4 20a8 8 0 0 1 16 0" ' + A + '/>',
    lock: '<rect x="4" y="11" width="16" height="10" rx="2" ' + L + '/><path d="M8 11V7a4 4 0 0 1 8 0v4" ' + A + '/>',
  };
  window.HQIconDuoPaths = D;
})();
