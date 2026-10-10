"""A small browser interface for the ResourceHub resource manager."""
from copy import deepcopy
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import secrets
import tempfile
from threading import RLock
import time
from urllib.parse import urlparse

import main as inventory

HOST = "127.0.0.1"
PORT = 8000
LOCK = RLock()
AUTH_FILE = "auth.json"
SESSION_COOKIE = "learn2earn_session"
SESSION_TTL = 8 * 60 * 60
PASSWORD_ROUNDS = 310_000
users = {}
sessions = {}

PAGE = r'''<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="theme-color" content="#101b2d">
  <title>ResourceHub · Resource desk</title>
  <style>
    :root{color-scheme:light;--ink:#182538;--muted:#718096;--line:#e6eaf0;--paper:#fff;--wash:#f4f6fa;--navy:#101b2d;--blue:#536dfe;--green:#16866a;--amber:#b66a12;--red:#bb4052;--shadow:0 12px 30px #17233a0b}
    *{box-sizing:border-box}[hidden]{display:none!important}body{margin:0;background:var(--wash);color:var(--ink);font:15px/1.5 Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}button,input,select{font:inherit}button{cursor:pointer}
    .shell{min-height:100vh}.topbar{height:72px;background:var(--navy);color:white;display:flex;align-items:center;justify-content:space-between;padding:0 max(24px,calc((100vw - 1320px)/2));gap:18px}.brand{display:flex;align-items:center;gap:12px;font-weight:700;letter-spacing:.01em}.brand-mark{height:36px;width:36px;border-radius:11px;background:#526dff;display:grid;place-items:center;font-size:18px}.brand small{display:block;color:#aab6ca;font-size:11px;font-weight:500;letter-spacing:.08em;text-transform:uppercase}.top-right{display:flex;align-items:center;gap:10px;color:#c2ccda;font-size:13px}.live{height:8px;width:8px;background:#42d3a5;border-radius:50%;box-shadow:0 0 0 4px #42d3a522}
    main{max-width:1320px;margin:auto;padding:36px 24px 60px}.heading{display:flex;justify-content:space-between;align-items:flex-end;gap:18px;margin-bottom:26px}.eyebrow{color:var(--blue);font-weight:700;text-transform:uppercase;letter-spacing:.12em;font-size:11px}.heading h1{font-size:30px;line-height:1.2;margin:7px 0}.heading p{color:var(--muted);margin:0}.today{color:var(--muted);font-size:13px;white-space:nowrap}
    .stats{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:15px;margin-bottom:22px}.stat,.panel{background:var(--paper);border:1px solid var(--line);border-radius:15px;box-shadow:var(--shadow)}.stat{padding:19px 20px;position:relative;overflow:hidden}.stat:after{content:"";position:absolute;width:64px;height:64px;border-radius:50%;right:-18px;top:-20px;background:#536dfe0b}.stat-label{font-size:12px;color:var(--muted);font-weight:600}.stat-value{font-size:27px;font-weight:750;letter-spacing:-.04em;margin-top:7px}.stat-foot{font-size:11px;color:#99a3b2;margin-top:2px}
    .layout{display:grid;grid-template-columns:minmax(0,1fr) 330px;gap:18px;align-items:start}.panel{padding:21px}.panel-head{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:17px}.panel-head h2{font-size:17px;margin:0;letter-spacing:-.02em}.subtle{color:var(--muted);font-size:12px;margin-top:3px}.actions{display:flex;gap:9px;align-items:center}.search{width:205px;padding:9px 11px;border:1px solid var(--line);border-radius:9px;background:#fff;outline:none}.search:focus,input:focus,select:focus{border-color:#8997ff;box-shadow:0 0 0 3px #536dfe19;outline:none}.btn{border:0;border-radius:9px;background:var(--blue);color:#fff;font-weight:650;padding:10px 14px;box-shadow:0 4px 10px #536dfe25}.btn:hover{background:#4059e8}.btn.secondary{background:#f0f2ff;color:#4059e8;box-shadow:none}.btn.secondary:hover{background:#e5e9ff}.btn.full{width:100%;margin-top:4px}.table-wrap{overflow:auto}table{width:100%;border-collapse:collapse;white-space:nowrap}th{text-align:left;color:#8a95a5;font-size:10px;text-transform:uppercase;letter-spacing:.09em;padding:11px 10px;border-bottom:1px solid var(--line)}td{padding:13px 10px;border-bottom:1px solid #eff1f5;font-size:13px}tbody tr:last-child td{border-bottom:0}.resource-name{font-weight:650}.resource-id{color:#97a1af;font-size:11px;margin-top:2px}.badge{display:inline-flex;align-items:center;gap:6px;border-radius:99px;padding:4px 8px;font-size:11px;font-weight:650;background:#eaf7f2;color:var(--green)}.badge:before{content:"";width:6px;height:6px;border-radius:50%;background:currentColor}.badge.low{background:#fff4e6;color:var(--amber)}.badge.out{background:#fff0f1;color:var(--red)}.empty{text-align:center;padding:32px;color:var(--muted)}
    .side{display:grid;gap:16px}.form-panel h2{font-size:16px;margin:0}.form-panel .subtle{margin-bottom:16px}.tabs{display:flex;background:#f1f3f8;padding:4px;border-radius:10px;margin-bottom:17px}.tab{border:0;background:transparent;border-radius:7px;flex:1;padding:8px;color:var(--muted);font-weight:650;font-size:12px}.tab.active{color:var(--ink);background:#fff;box-shadow:0 1px 4px #14213d13}.field{margin-bottom:12px}.field label{display:block;font-size:11px;color:#5c697b;font-weight:650;margin-bottom:5px}.field input,.field select{width:100%;border:1px solid var(--line);border-radius:9px;padding:10px 11px;background:#fff;color:var(--ink)}.form-message{min-height:18px;margin-top:10px;font-size:12px;color:var(--green)}.form-message.error{color:var(--red)}.loan-list{display:grid;gap:11px}.loan-row{display:flex;justify-content:space-between;gap:10px;align-items:center;border-bottom:1px solid #eff1f5;padding-bottom:10px}.loan-row:last-child{border:0;padding:0}.loan-who{font-size:12px;font-weight:650}.loan-what{font-size:11px;color:var(--muted)}.loan-count{font-weight:700;font-size:12px;color:#394c69;white-space:nowrap}.toast{position:fixed;right:22px;bottom:22px;background:#182538;color:#fff;padding:12px 16px;border-radius:10px;box-shadow:var(--shadow);opacity:0;transform:translateY(10px);transition:.2s;pointer-events:none}.toast.show{opacity:1;transform:none}.footnote{margin-top:17px;color:#9aa4b2;font-size:11px;text-align:center}.auth-screen{min-height:100vh;display:grid;place-items:center;padding:24px;background:radial-gradient(ellipse at 18% 12%,#263963 0,transparent 42%),var(--navy)}.auth-card{width:min(430px,100%);background:white;border-radius:19px;padding:34px;box-shadow:0 25px 80px #0004}.auth-brand{display:flex;align-items:center;gap:11px;font-weight:750;margin-bottom:27px}.auth-card h1{font-size:25px;margin:0;letter-spacing:-.04em}.auth-card>p{color:var(--muted);margin:6px 0 22px;font-size:13px}.auth-tabs{margin-bottom:20px}.auth-card .field{margin-bottom:14px}.auth-error{color:var(--red);font-size:12px;min-height:18px;margin:10px 0}.account-name{color:white;font-size:12px}.logout{border:1px solid #ffffff35;color:white;background:transparent;border-radius:8px;padding:7px 10px;font-size:12px}.logout:hover{background:#ffffff14}
    @media(max-width:940px){.layout{grid-template-columns:1fr}.side{grid-template-columns:repeat(2,minmax(0,1fr))}.stats{grid-template-columns:repeat(2,1fr)}}@media(max-width:600px){.topbar{height:62px;padding:0 17px}.top-right .status-text{display:none}main{padding:25px 14px 40px}.heading{align-items:flex-start;flex-direction:column}.heading h1{font-size:26px}.today{display:none}.stats{gap:10px}.stat{padding:15px}.stat-value{font-size:24px}.layout{gap:13px}.panel{padding:16px}.panel-head{align-items:flex-start;flex-direction:column}.actions,.search{width:100%}.side{grid-template-columns:1fr}th,td{padding-left:7px;padding-right:7px}}
    .landing-screen{min-height:100vh;overflow:hidden;background:radial-gradient(ellipse at 83% 44%,#e8edff 0,transparent 34%),linear-gradient(135deg,#f8faff,#fff 58%,#eef2ff)}
    .landing-nav{height:82px;max-width:1240px;margin:auto;padding:0 28px;display:flex;align-items:center;justify-content:space-between}
    .landing-brand{display:flex;align-items:center;gap:12px;color:var(--ink);font-size:18px;font-weight:800}.landing-brand .brand-mark{color:white}
    .landing-content{max-width:1240px;min-height:calc(100vh - 82px);margin:auto;padding:48px 28px 74px;display:grid;grid-template-columns:1.02fr .98fr;align-items:center;gap:50px}
    .landing-copy .eyebrow{display:inline-flex;align-items:center;gap:8px;background:#eef1ff;padding:8px 12px;border-radius:99px}.landing-copy h1{max-width:650px;margin:22px 0 17px;font-size:clamp(42px,6vw,72px);line-height:1.02;letter-spacing:-.065em;color:#14213a}.landing-copy h1 span{color:var(--blue)}.landing-copy>p{max-width:540px;margin:0;color:#64738a;font-size:18px;line-height:1.7}.landing-actions{display:flex;align-items:center;gap:14px;margin-top:30px}.landing-actions .btn{padding:13px 19px;border-radius:11px}.landing-note{font-size:12px;color:#8793a5;margin-top:19px}
    .preview-wrap{position:relative;padding:24px}.preview-glow{position:absolute;inset:12% 4%;background:#536dfe26;filter:blur(46px);border-radius:40%}.inventory-preview{position:relative;background:#fff;border:1px solid #e8ebf3;border-radius:22px;padding:22px;box-shadow:0 30px 90px #273c7320;transform:rotate(1deg)}.preview-top{display:flex;justify-content:space-between;align-items:center;margin-bottom:22px}.preview-title{font-weight:750}.preview-status{font-size:11px;color:var(--green);background:#e9f8f2;padding:6px 9px;border-radius:99px}.preview-stats{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}.preview-stat{padding:13px;border:1px solid #edf0f5;border-radius:12px}.preview-stat strong{display:block;font-size:23px;letter-spacing:-.04em}.preview-stat small{font-size:10px;color:#8490a2}.preview-list{margin-top:20px;border:1px solid #edf0f5;border-radius:14px;overflow:hidden}.preview-row{display:grid;grid-template-columns:1fr auto auto;gap:20px;padding:13px 15px;align-items:center;border-bottom:1px solid #f0f2f6;font-size:12px}.preview-row:last-child{border-bottom:0}.preview-row span:first-child{font-weight:650}.preview-row small{color:#8490a2}.preview-pill{background:#eef1ff;color:#536dfe;padding:5px 8px;border-radius:99px;font-size:10px;font-weight:700}.landing-points{display:flex;gap:20px;margin-top:27px;color:#65748b;font-size:12px}.landing-points span{display:flex;align-items:center;gap:7px}.landing-points b{color:var(--green);font-size:15px}
    .auth-back{display:block;margin:0 auto 14px;background:none;border:0;color:#536dfe;font-size:12px;font-weight:650}
    .landing-features{max-width:1184px;margin:0 auto;padding:0 28px 76px;display:grid;grid-template-columns:repeat(3,1fr);gap:15px}.landing-feature{padding:21px 22px;background:#ffffffc9;border:1px solid #e8ebf3;border-radius:16px}.landing-feature small{color:#536dfe;font-weight:750;letter-spacing:.1em;font-size:10px}.landing-feature h2{font-size:17px;margin:10px 0 6px;letter-spacing:-.02em}.landing-feature p{margin:0;color:#718096;font-size:12px;line-height:1.65}
    .category-filter{min-width:150px;padding:11px 12px;border:1px solid var(--line);border-radius:9px;background:#fff;color:var(--ink)}.watch-list{display:grid;gap:8px}.watch-row{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:10px 11px;border-radius:10px;background:#fafbfe}.watch-name{font-size:12px;font-weight:650}.watch-detail{font-size:10px;color:var(--muted)}.watch-count{font-size:11px;font-weight:700;color:var(--amber);white-space:nowrap}.watch-count.out{color:var(--red)}
    @media(max-width:850px){.landing-content{grid-template-columns:1fr;gap:20px;padding-top:55px}.landing-copy h1{max-width:720px}.preview-wrap{max-width:620px;width:100%;margin:auto}.landing-screen{overflow:auto}}
    @media(max-width:700px){.landing-features{grid-template-columns:1fr;padding:0 20px 48px}.category-filter{width:100%}}
    @media(max-width:520px){.landing-nav{height:70px;padding:0 18px}.landing-content{padding:44px 20px 52px}.landing-copy h1{font-size:43px}.landing-copy>p{font-size:15px}.landing-actions{align-items:flex-start;flex-direction:column}.preview-wrap{padding:10px 0}.inventory-preview{padding:15px}.preview-stat{padding:9px}.preview-stat strong{font-size:19px}.preview-row{gap:8px;padding:12px 10px}.landing-points{flex-wrap:wrap;gap:10px 16px}}
    /* Dashboard polish: clearer surfaces, spacing, focus, and interaction feedback. */
    body{background:#f5f7fc}
    .topbar{position:sticky;top:0;z-index:5;background:#111d32;box-shadow:0 5px 20px #101b2d18}
    main{max-width:1400px;padding-top:42px}
    .heading{margin-bottom:30px}.heading h1{font-size:34px;letter-spacing:-.045em}.heading p{font-size:14px;margin-top:7px}
    .stats{gap:17px;margin-bottom:25px}.stat{border-radius:17px;padding:21px 22px;transition:transform .18s ease,box-shadow .18s ease}.stat:hover{transform:translateY(-3px);box-shadow:0 16px 35px #17233a12}.stat-value{font-size:30px}
    .panel{border-radius:17px;box-shadow:0 8px 28px #17233a08}.layout{gap:20px}.panel-head h2{font-size:18px}.subtle{line-height:1.5}
    .search{width:230px;padding:11px 13px}.btn{transition:transform .16s ease,background .16s ease,box-shadow .16s ease}.btn:hover{transform:translateY(-1px);box-shadow:0 7px 16px #536dfe35}.btn:active{transform:translateY(0)}.btn:disabled{cursor:not-allowed;opacity:.48;transform:none;box-shadow:none}
    table{border-spacing:0}th{padding-top:13px;padding-bottom:13px}td{padding-top:15px;padding-bottom:15px}tbody tr{transition:background .14s ease}tbody tr:hover{background:#f8f9ff}.badge{padding:5px 10px}
    .form-panel{border-top:3px solid #536dfe}.tabs{padding:5px}.tab{padding:9px}.field input,.field select{padding:11px 12px;transition:border-color .15s ease,box-shadow .15s ease}.field input:disabled,.field select:disabled{background:#f4f6fa;color:#9aa4b2;cursor:not-allowed}
    .loan-row{padding:12px 0}.loan-list{gap:0}.loan-row:hover .loan-who{color:#536dfe}.empty{border-radius:12px;background:#fafbfe}
    :focus-visible{outline:3px solid #8997ff!important;outline-offset:2px}
    @media(prefers-reduced-motion:reduce){*,*::before,*::after{scroll-behavior:auto!important;transition:none!important}}
    @media(max-width:600px){main{padding-top:28px}.heading h1{font-size:28px}.search{width:100%}.actions{display:grid;grid-template-columns:1fr 1fr}.actions .search,.actions .btn{grid-column:1/-1}.category-filter{min-width:0}.topbar{backdrop-filter:blur(12px)}}
  </style>
</head>
<body><div class="shell">
  <section class="landing-screen" id="landingScreen">
    <nav class="landing-nav"><div class="landing-brand"><div class="brand-mark">✳</div><span>ResourceHub</span></div><button class="btn" type="button" data-open-auth>Sign in</button></nav>
    <div class="landing-content">
      <div class="landing-copy"><div class="eyebrow"><span class="live"></span> CAMPUS RESOURCE MANAGEMENT</div><h1>Shared resources.<br><span>Handled simply.</span></h1><p>Keep equipment, fellows, and loans organized in one friendly campus resource desk.</p><div class="landing-actions"><button class="btn" type="button" data-open-auth>Open ResourceHub <span aria-hidden="true">→</span></button><span class="subtle">A clearer way to keep things moving.</span></div><div class="landing-points"><span><b>✓</b> Know what’s available</span><span><b>✓</b> Track every loan</span><span><b>✓</b> Stay ahead of due dates</span></div></div>
      <div class="preview-wrap"><div class="preview-glow"></div><div class="inventory-preview"><div class="preview-top"><div><div class="eyebrow">SAMPLE RESOURCE DESK</div><div class="preview-title">A quick look inside</div></div><span class="preview-status">Inventory overview</span></div><div class="preview-stats"><div class="preview-stat"><strong>18</strong><small>Total units</small></div><div class="preview-stat"><strong>12</strong><small>Available now</small></div><div class="preview-stat"><strong>6</strong><small>On loan</small></div></div><div class="preview-list"><div class="preview-row"><span>💻 Laptop</span><small>Electronics</small><span class="preview-pill">7 ready</span></div><div class="preview-row"><span>⌨️ Keyboard</span><small>Accessories</small><span class="preview-pill">3 ready</span></div><div class="preview-row"><span>🎧 Headset</span><small>Accessories</small><span class="preview-pill">2 ready</span></div></div></div></div>
    </div>
    <section class="landing-features" aria-label="ResourceHub features"><article class="landing-feature"><small>01 / INVENTORY</small><h2>Know what’s ready</h2><p>See equipment, categories, and live availability in one clear list.</p></article><article class="landing-feature"><small>02 / LOANS</small><h2>Keep every loan straight</h2><p>Record checkouts and returns with fellow details and due dates.</p></article><article class="landing-feature"><small>03 / REPORTS</small><h2>Catch issues early</h2><p>Spot low stock and overdue equipment before they become a problem.</p></article></section>
  </section>
  <section class="auth-screen" id="authScreen" hidden><div class="auth-card"><button class="auth-back" type="button" id="backLanding">← Back to ResourceHub</button><div class="auth-brand"><div class="brand-mark">✳</div><div>ResourceHub<small style="display:block;color:#718096;font-size:10px;font-weight:600;letter-spacing:.08em;text-transform:uppercase">Campus resource desk</small></div></div><h1 id="authTitle">Welcome back</h1><p id="authSubtitle">Sign in to manage campus equipment and loans.</p><div class="tabs auth-tabs"><button class="tab active" type="button" data-auth-mode="login">Sign in</button><button class="tab" type="button" data-auth-mode="register">Create account</button></div><form id="authForm"><div class="field" id="nameField" hidden><label for="authName">Your name</label><input id="authName" autocomplete="name" maxlength="80"></div><div class="field"><label for="authUsername">Username</label><input id="authUsername" autocomplete="username" minlength="3" maxlength="40" required></div><div class="field"><label for="authPassword">Password</label><input id="authPassword" type="password" autocomplete="current-password" minlength="10" required></div><button class="btn full" type="submit" id="authSubmit">Sign in</button><div class="auth-error" id="authError" role="alert"></div></form><div class="subtle">Your account is stored on this computer.</div></div></section>
  <div id="appScreen" hidden>
  <header class="topbar"><div class="brand"><div class="brand-mark">✳</div><div>ResourceHub<small>Campus resource desk</small></div></div><div class="top-right"><span class="live"></span><span class="status-text">Inventory system online</span><span class="account-name" id="accountName"></span><button class="logout" id="logoutButton">Sign out</button></div></header>
  <main>
    <section class="heading"><div><div class="eyebrow">Operations overview</div><h1>Resource desk</h1><p>Keep track of campus equipment and active loans.</p></div><div class="today" id="today"></div></section>
    <section class="stats" aria-label="Inventory summary"><article class="stat"><div class="stat-label">Total equipment</div><div class="stat-value" id="totalUnits">—</div><div class="stat-foot">units across all resources</div></article><article class="stat"><div class="stat-label">Ready to borrow</div><div class="stat-value" id="availableUnits">—</div><div class="stat-foot">currently available units</div></article><article class="stat"><div class="stat-label">On loan</div><div class="stat-value" id="borrowedUnits">—</div><div class="stat-foot">units with fellows</div></article><article class="stat"><div class="stat-label">Resource types</div><div class="stat-value" id="resourceCount">—</div><div class="stat-foot">items in your catalog</div></article></section>
    <section class="layout"><div class="panel"><div class="panel-head"><div><h2>Equipment inventory</h2><div class="subtle">Availability and stock levels</div></div><div class="actions"><input class="search" id="search" type="search" placeholder="Search equipment…" aria-label="Search equipment"><select class="category-filter" id="categoryFilter" aria-label="Filter by category"><option value="">All categories</option></select><button class="btn" id="addButton">＋ Add equipment</button></div></div><div class="table-wrap"><table><thead><tr><th>Equipment</th><th>Category</th><th>Total units</th><th>Available</th><th>Status</th></tr></thead><tbody id="resourceRows"></tbody></table></div></div>
      <aside class="side"><section class="panel form-panel"><h2>Quick transaction</h2><div class="subtle">Record an equipment loan or return.</div><div class="tabs"><button class="tab active" data-mode="borrow">Borrow</button><button class="tab" data-mode="return">Return</button></div><form id="transactionForm"><div class="field"><label for="fellow">Fellow</label><select id="fellow" required></select></div><div class="field"><label for="resource">Equipment</label><select id="resource" required></select></div><div class="field"><label for="quantity">Quantity</label><input id="quantity" type="number" min="1" step="1" value="1" required></div><button class="btn full" id="transactionButton" type="submit">Record loan</button><div class="form-message" id="transactionMessage" role="status"></div></form></section>
      <section class="panel"><div class="panel-head"><div><h2>Currently on loan</h2><div class="subtle">Outstanding equipment by fellow</div></div></div><div class="loan-list" id="loanList"></div></section><section class="panel"><div class="panel-head"><div><h2>Inventory watch</h2><div class="subtle">Items with fewer than 3 available</div></div></div><div class="watch-list" id="stockWatch"></div></section></aside>
    </section><div class="footnote">ResourceHub · Campus resource management</div>
  </main><div class="toast" id="toast" role="status"></div>
  <dialog id="addDialog" style="border:0;border-radius:15px;padding:24px;width:min(440px,calc(100% - 28px));box-shadow:0 25px 80px #101b2d33"><form id="addForm"><h2 style="margin:0 0 5px">Add equipment</h2><p class="subtle" style="margin:0 0 16px">Create a new item in the resource catalog.</p><div class="field"><label for="newId">Resource ID</label><input id="newId" placeholder="e.g. R004" required></div><div class="field"><label for="newName">Name</label><input id="newName" placeholder="e.g. Projector" required></div><div class="field"><label for="newCategory">Category</label><input id="newCategory" placeholder="e.g. Electronics" required></div><div class="field"><label for="newTotal">Total units</label><input id="newTotal" type="number" min="1" step="1" value="1" required></div><div style="display:flex;gap:9px;justify-content:flex-end;margin-top:18px"><button type="button" class="btn secondary" id="cancelAdd">Cancel</button><button class="btn" type="submit">Add equipment</button></div><div id="addMessage" class="form-message" role="status"></div></form></dialog>
  <script>
    const $ = selector => document.querySelector(selector);
    let state = null, mode = 'borrow', toastTimer;
    $('#today').textContent = new Intl.DateTimeFormat(undefined,{weekday:'long',month:'long',day:'numeric',year:'numeric'}).format(new Date());
    function esc(value){return String(value).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
    async function api(path, body){const response=await fetch(path,{method:body?'POST':'GET',headers:body?{'Content-Type':'application/json'}:{},body:body?JSON.stringify(body):undefined});const data=await response.json();if(!response.ok)throw new Error(data.error||'Something went wrong.');return data}
    function showToast(message){const box=$('#toast');box.textContent=message;box.classList.add('show');clearTimeout(toastTimer);toastTimer=setTimeout(()=>box.classList.remove('show'),2800)}
    function render(){if(!state)return;const resources=state.resources,filter=$('#search').value.trim().toLowerCase(),categoryControl=$('#categoryFilter'),oldCategory=categoryControl.value,categories=[...new Set(resources.map(r=>r.category))].sort((a,b)=>a.localeCompare(b));categoryControl.innerHTML='<option value="">All categories</option>'+categories.map(name=>`<option value="${esc(name)}">${esc(name)}</option>`).join('');if(categories.includes(oldCategory))categoryControl.value=oldCategory;const category=categoryControl.value.toLowerCase();const shown=resources.filter(r=>(r.name+' '+r.category+' '+r.id).toLowerCase().includes(filter)&&(!category||r.category.toLowerCase()===category));
      $('#totalUnits').textContent=state.summary.total;$('#availableUnits').textContent=state.summary.available;$('#borrowedUnits').textContent=state.summary.borrowed;$('#resourceCount').textContent=resources.length;
      $('#resourceRows').innerHTML=shown.length?shown.map(r=>{const status=r.available===0?['Out of stock','out']:r.available<3?['Low stock','low']:['In stock',''];return `<tr><td><div class="resource-name">${esc(r.name)}</div><div class="resource-id">${esc(r.id)}</div></td><td>${esc(r.category)}</td><td>${r.total}</td><td><strong>${r.available}</strong></td><td><span class="badge ${status[1]}">${status[0]}</span></td></tr>`}).join(''):`<tr><td colspan="5" class="empty">${resources.length?'No equipment matches your search.':'No equipment has been added yet.'}</td></tr>`;
      const lowStock=resources.filter(r=>r.available<3);$('#stockWatch').innerHTML=lowStock.length?lowStock.map(r=>`<div class="watch-row"><div><div class="watch-name">${esc(r.name)}</div><div class="watch-detail">${esc(r.id)} · ${r.available===0?'Out of stock':'Low stock'}</div></div><span class="watch-count ${r.available===0?'out':''}">${r.available} available</span></div>`).join(''):'<div class="empty" style="padding:14px">All items have healthy stock.</div>';
      const fellowSelect=$('#fellow'), previousFellow=fellowSelect.value;fellowSelect.innerHTML=state.fellows.map(f=>`<option value="${esc(f.id)}">${esc(f.name)}</option>`).join('');if(state.fellows.some(f=>f.id===previousFellow))fellowSelect.value=previousFellow;
      const select=$('#resource'), previous=select.value, choices=mode==='borrow'?resources.filter(r=>r.available>0):state.loans.filter(l=>l.fellow_id===fellowSelect.value);select.innerHTML=choices.map(r=>{const id=r.id||r.resource_id;const item=resources.find(x=>x.id===id);return `<option value="${esc(id)}">${esc(item?.name||r.name)} (${mode==='borrow'?r.available+' available':r.quantity+' on loan'})</option>`}).join('');if(choices.some(r=>(r.id||r.resource_id)===previous))select.value=previous;
      $('#resource').disabled=!choices.length;$('#transactionButton').disabled=!choices.length;$('#transactionButton').textContent=mode==='borrow'?'Record loan':'Record return';
      const loans=state.loans;$('#loanList').innerHTML=loans.length?loans.map(l=>`<div class="loan-row"><div><div class="loan-who">${esc(l.fellow_name)}</div><div class="loan-what">${esc(l.resource_name)}</div></div><div class="loan-count">${l.quantity} unit${l.quantity===1?'':'s'}</div></div>`).join(''):'<div class="empty" style="padding:15px 0">Nothing is currently on loan.</div>';
    }
    async function refresh(){state=await api('/api/state');render()}
    $('#search').addEventListener('input',render);$('#categoryFilter').addEventListener('change',render);document.querySelectorAll('.tab[data-mode]').forEach(tab=>tab.addEventListener('click',()=>{mode=tab.dataset.mode;document.querySelectorAll('.tab[data-mode]').forEach(t=>t.classList.toggle('active',t===tab));$('#transactionMessage').textContent='';render()}));$('#fellow').addEventListener('change',render);
    $('#transactionForm').addEventListener('submit',async event=>{event.preventDefault();const message=$('#transactionMessage');message.className='form-message';message.textContent='';try{const result=await api('/api/'+mode,{fellow_id:$('#fellow').value,resource_id:$('#resource').value,quantity:Number($('#quantity').value)});message.textContent=result.message;await refresh();showToast(result.message)}catch(error){message.classList.add('error');message.textContent=error.message}});
    $('#addButton').addEventListener('click',()=>{$('#addMessage').textContent='';$('#addDialog').showModal()});$('#cancelAdd').addEventListener('click',()=>$('#addDialog').close());$('#addForm').addEventListener('submit',async event=>{event.preventDefault();const message=$('#addMessage');message.className='form-message';message.textContent='';try{const result=await api('/api/resources',{id:$('#newId').value,name:$('#newName').value,category:$('#newCategory').value,total:Number($('#newTotal').value)});$('#addDialog').close();event.target.reset();$('#newTotal').value='1';await refresh();showToast(result.message)}catch(error){message.classList.add('error');message.textContent=error.message}});
    let authMode='login';
    function setAuthMode(next){authMode=next;const registering=next==='register';$('#authTitle').textContent=registering?'Create your account':'Welcome back';$('#authSubtitle').textContent=registering?'Register to manage campus equipment and loans.':'Sign in to manage campus equipment and loans.';$('#nameField').hidden=!registering;$('#authName').required=registering;$('#authPassword').autocomplete=registering?'new-password':'current-password';$('#authSubmit').textContent=registering?'Create account':'Sign in';document.querySelectorAll('[data-auth-mode]').forEach(tab=>tab.classList.toggle('active',tab.dataset.authMode===next));$('#authError').textContent=''}
    function showApp(name){$('#accountName').textContent=name;$('#landingScreen').hidden=true;$('#authScreen').hidden=true;$('#appScreen').hidden=false;refresh().catch(error=>showToast(error.message))}
    document.querySelectorAll('[data-open-auth]').forEach(button=>button.addEventListener('click',()=>{$('#landingScreen').hidden=true;$('#authScreen').hidden=false;setAuthMode('login')}));
    $('#backLanding').addEventListener('click',()=>{$('#authScreen').hidden=true;$('#landingScreen').hidden=false});
    document.querySelectorAll('[data-auth-mode]').forEach(tab=>tab.addEventListener('click',()=>setAuthMode(tab.dataset.authMode)));
    $('#authForm').addEventListener('submit',async event=>{event.preventDefault();const errorBox=$('#authError');errorBox.textContent='';const payload={username:$('#authUsername').value,password:$('#authPassword').value};if(authMode==='register')payload.name=$('#authName').value;try{const result=await api('/api/'+(authMode==='register'?'register':'login'),payload);event.target.reset();showApp(result.name)}catch(error){errorBox.textContent=error.message}});
    $('#logoutButton').addEventListener('click',async()=>{try{await api('/api/logout',{});$('#appScreen').hidden=true;$('#authScreen').hidden=true;$('#landingScreen').hidden=false;$('#authPassword').value='';setAuthMode('login')}catch(error){showToast(error.message)}});
    api('/api/me').then(me=>{if(me.authenticated)showApp(me.name)}).catch(error=>showToast(error.message));setInterval(()=>{if(!$('#appScreen').hidden)refresh().catch(()=>{})},30000);
  </script>
</div></div></body></html>'''


def save_users():
    """Atomically write user password hashes to the local auth file."""
    path = os.path.abspath(AUTH_FILE)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=os.path.dirname(path),
                                         delete=False) as f:
            temp_path = f.name
            json.dump({"users": users}, f, indent=2)
            f.write("\n")
        os.replace(temp_path, path)
    finally:
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)


def load_users():
    """Load account hashes; refuse to overwrite an unreadable account file."""
    try:
        with open(AUTH_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)
    except FileNotFoundError:
        return
    if not isinstance(saved, dict) or not isinstance(saved.get("users"), dict):
        raise RuntimeError("auth.json has an invalid format; account data was not loaded.")
    for username, user in saved["users"].items():
        if (not isinstance(username, str) or not isinstance(user, dict)
                or not isinstance(user.get("name"), str)
                or not isinstance(user.get("salt"), str)
                or not isinstance(user.get("password_hash"), str)):
            raise RuntimeError("auth.json has an invalid account record.")
        try:
            salt, digest = bytes.fromhex(user["salt"]), bytes.fromhex(user["password_hash"])
        except ValueError as error:
            raise RuntimeError("auth.json contains an invalid password hash.") from error
        if len(salt) != 16 or len(digest) != 32:
            raise RuntimeError("auth.json contains an invalid password hash.")
    users.update(saved["users"])


def snapshot():
    """Return UI-ready data while callers hold LOCK."""
    inventory.refresh_state()
    resources = deepcopy(inventory.resources)
    loans = []
    for resource in resources:
        for fellow_id in inventory.fellows:
            quantity = inventory.outstanding(fellow_id, resource["id"])
            if quantity > 0:
                loans.append({
                    "fellow_id": fellow_id,
                    "fellow_name": inventory.fellows[fellow_id],
                    "resource_id": resource["id"],
                    "resource_name": resource["name"],
                    "quantity": quantity,
                })
    total = sum(item["total"] for item in resources)
    available = sum(item["available"] for item in resources)
    return {
        "resources": resources,
        "fellows": [{"id": key, "name": name} for key, name in inventory.fellows.items()],
        "loans": loans,
        "summary": {"total": total, "available": available, "borrowed": total - available},
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "ResourceHub/1.0"

    def respond(self, status, payload, content_type="application/json; charset=utf-8", headers=None):
        body = payload.encode("utf-8") if isinstance(payload, str) else json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if size < 1 or size > 16_384:
                raise ValueError("Request body must be between 1 and 16384 bytes.")
            if "application/json" not in self.headers.get("Content-Type", ""):
                raise ValueError("Send request data as JSON.")
            data = json.loads(self.rfile.read(size))
            if not isinstance(data, dict):
                raise ValueError("Request data must be a JSON object.")
            return data
        except (ValueError, json.JSONDecodeError) as error:
            raise ValueError("Invalid request data.") from error

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            self.respond(200, PAGE, "text/html; charset=utf-8")
        elif path == "/api/state":
            with LOCK:
                if not self.current_user():
                    self.respond(401, {"error": "Please sign in to continue."})
                    return
                self.respond(200, snapshot())
        elif path == "/api/me":
            with LOCK:
                username = self.current_user()
                self.respond(200, {"authenticated": bool(username),
                                   "name": users[username]["name"] if username else ""})
        else:
            self.respond(404, {"error": "Page not found."})

    def current_user(self):
        """Return the active username and extend its inactivity timeout."""
        from http.cookies import SimpleCookie
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
            token = cookie[SESSION_COOKIE].value
        except Exception:
            return None
        session = sessions.get(token)
        now = time.time()
        if not session or session["expires"] < now:
            sessions.pop(token, None)
            return None
        session["expires"] = now + SESSION_TTL
        return session["username"]

    def set_session(self, username):
        token = secrets.token_urlsafe(32)
        sessions[token] = {"username": username, "expires": time.time() + SESSION_TTL}
        return f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_TTL}"

    def clear_session(self):
        from http.cookies import SimpleCookie
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
            token = cookie[SESSION_COOKIE].value
            sessions.pop(token, None)
        except Exception:
            pass
        return f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"

    def do_POST(self):
        path = urlparse(self.path).path
        if path in ("/api/register", "/api/login", "/api/logout"):
            self.handle_auth(path)
            return
        if path not in ("/api/resources", "/api/borrow", "/api/return"):
            self.respond(404, {"error": "Action not found."})
            return
        try:
            data = self.read_json()
            if path == "/api/resources":
                if any(not isinstance(data.get(key), str) for key in ("id", "name", "category")):
                    raise ValueError("ID, name and category must be text.")
                if type(data.get("total")) is not int:
                    raise ValueError("Total units must be a whole number.")
            else:
                if any(not isinstance(data.get(key), str) for key in ("fellow_id", "resource_id")):
                    raise ValueError("Fellow and equipment IDs must be text.")
                if type(data.get("quantity")) is not int:
                    raise ValueError("Quantity must be a whole number.")
            with LOCK:
                if not self.current_user():
                    self.respond(401, {"error": "Your session expired. Please sign in again."})
                    return
                before_resources = deepcopy(inventory.resources)
                before_records = deepcopy(inventory.borrow_records)
                if path == "/api/resources":
                    success, message = inventory.add_resource(
                        data.get("id", ""), data.get("name", ""),
                        data.get("category", ""), data.get("total"),
                    )
                elif path == "/api/borrow":
                    success, message = inventory.borrow(
                        data.get("fellow_id", ""), data.get("resource_id", ""), data.get("quantity"),
                    )
                else:
                    success, message = inventory.return_item(
                        data.get("fellow_id", ""), data.get("resource_id", ""), data.get("quantity"),
                    )
                if not success:
                    self.respond(400, {"error": message})
                    return
                try:
                    inventory.save_data()
                except (OSError, TypeError, ValueError) as error:
                    inventory.resources[:] = before_resources
                    inventory.borrow_records[:] = before_records
                    self.respond(500, {"error": f"Could not save this change: {error}"})
                    return
            self.respond(200, {"message": message})
        except ValueError as error:
            self.respond(400, {"error": str(error)})

    def handle_auth(self, path):
        try:
            data = self.read_json()
            with LOCK:
                if path == "/api/logout":
                    cookie = self.clear_session()
                    self.respond(200, {"message": "You have been signed out."}, headers={"Set-Cookie": cookie})
                    return

                username = data.get("username", "")
                password = data.get("password", "")
                if not isinstance(username, str) or not isinstance(password, str):
                    raise ValueError("Enter a username and password.")
                username = username.strip().lower()
                if path == "/api/register":
                    name = data.get("name", "")
                    if not isinstance(name, str) or not name.strip():
                        raise ValueError("Enter your name.")
                    if len(name.strip()) > 80:
                        raise ValueError("Your name must be 80 characters or fewer.")
                    if len(username) < 3 or len(username) > 40 or not all(
                            char.isalnum() or char in "._-" for char in username):
                        raise ValueError("Username must be 3–40 characters and use letters, numbers, dots, dashes or underscores.")
                    if len(password) < 10:
                        raise ValueError("Use a password with at least 10 characters.")
                    if username in users:
                        self.respond(409, {"error": "That username is already registered."})
                        return
                    salt = secrets.token_bytes(16)
                    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_ROUNDS)
                    users[username] = {"name": name.strip(), "salt": salt.hex(), "password_hash": digest.hex()}
                    try:
                        save_users()
                    except OSError as error:
                        users.pop(username, None)
                        self.respond(500, {"error": f"Could not save your account: {error}"})
                        return
                else:
                    user = users.get(username)
                    if not user:
                        self.respond(401, {"error": "Username or password is incorrect."})
                        return
                    expected = bytes.fromhex(user["password_hash"])
                    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                                  bytes.fromhex(user["salt"]), PASSWORD_ROUNDS)
                    if not hmac.compare_digest(actual, expected):
                        self.respond(401, {"error": "Username or password is incorrect."})
                        return
                cookie = self.set_session(username)
                self.respond(200, {"message": "Signed in.", "name": users[username]["name"]},
                             headers={"Set-Cookie": cookie})
        except ValueError as error:
            self.respond(400, {"error": str(error)})

    def log_message(self, fmt, *args):
        print("%s - %s" % (self.address_string(), fmt % args))


def main():
    load_users()
    inventory.load_data()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"ResourceHub is running at http://{HOST}:{PORT}")
    print("Press Ctrl+C to stop the server.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping ResourceHub...")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
