"""A small browser interface for the Learn2Earn resource manager."""
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import RLock
from urllib.parse import urlparse

import main as inventory

HOST = "127.0.0.1"
PORT = 8000
LOCK = RLock()

PAGE = r'''<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="theme-color" content="#101b2d">
  <title>Learn2Earn · Resource desk</title>
  <style>
    :root{color-scheme:light;--ink:#182538;--muted:#718096;--line:#e6eaf0;--paper:#fff;--wash:#f4f6fa;--navy:#101b2d;--blue:#536dfe;--green:#16866a;--amber:#b66a12;--red:#bb4052;--shadow:0 12px 30px #17233a0b}
    *{box-sizing:border-box}body{margin:0;background:var(--wash);color:var(--ink);font:15px/1.5 Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}button,input,select{font:inherit}button{cursor:pointer}
    .shell{min-height:100vh}.topbar{height:72px;background:var(--navy);color:white;display:flex;align-items:center;justify-content:space-between;padding:0 max(24px,calc((100vw - 1320px)/2));gap:18px}.brand{display:flex;align-items:center;gap:12px;font-weight:700;letter-spacing:.01em}.brand-mark{height:36px;width:36px;border-radius:11px;background:#526dff;display:grid;place-items:center;font-size:18px}.brand small{display:block;color:#aab6ca;font-size:11px;font-weight:500;letter-spacing:.08em;text-transform:uppercase}.top-right{display:flex;align-items:center;gap:10px;color:#c2ccda;font-size:13px}.live{height:8px;width:8px;background:#42d3a5;border-radius:50%;box-shadow:0 0 0 4px #42d3a522}
    main{max-width:1320px;margin:auto;padding:36px 24px 60px}.heading{display:flex;justify-content:space-between;align-items:flex-end;gap:18px;margin-bottom:26px}.eyebrow{color:var(--blue);font-weight:700;text-transform:uppercase;letter-spacing:.12em;font-size:11px}.heading h1{font-size:30px;line-height:1.2;margin:7px 0}.heading p{color:var(--muted);margin:0}.today{color:var(--muted);font-size:13px;white-space:nowrap}
    .stats{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:15px;margin-bottom:22px}.stat,.panel{background:var(--paper);border:1px solid var(--line);border-radius:15px;box-shadow:var(--shadow)}.stat{padding:19px 20px;position:relative;overflow:hidden}.stat:after{content:"";position:absolute;width:64px;height:64px;border-radius:50%;right:-18px;top:-20px;background:#536dfe0b}.stat-label{font-size:12px;color:var(--muted);font-weight:600}.stat-value{font-size:27px;font-weight:750;letter-spacing:-.04em;margin-top:7px}.stat-foot{font-size:11px;color:#99a3b2;margin-top:2px}
    .layout{display:grid;grid-template-columns:minmax(0,1fr) 330px;gap:18px;align-items:start}.panel{padding:21px}.panel-head{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:17px}.panel-head h2{font-size:17px;margin:0;letter-spacing:-.02em}.subtle{color:var(--muted);font-size:12px;margin-top:3px}.actions{display:flex;gap:9px;align-items:center}.search{width:205px;padding:9px 11px;border:1px solid var(--line);border-radius:9px;background:#fff;outline:none}.search:focus,input:focus,select:focus{border-color:#8997ff;box-shadow:0 0 0 3px #536dfe19;outline:none}.btn{border:0;border-radius:9px;background:var(--blue);color:#fff;font-weight:650;padding:10px 14px;box-shadow:0 4px 10px #536dfe25}.btn:hover{background:#4059e8}.btn.secondary{background:#f0f2ff;color:#4059e8;box-shadow:none}.btn.secondary:hover{background:#e5e9ff}.btn.full{width:100%;margin-top:4px}.table-wrap{overflow:auto}table{width:100%;border-collapse:collapse;white-space:nowrap}th{text-align:left;color:#8a95a5;font-size:10px;text-transform:uppercase;letter-spacing:.09em;padding:11px 10px;border-bottom:1px solid var(--line)}td{padding:13px 10px;border-bottom:1px solid #eff1f5;font-size:13px}tbody tr:last-child td{border-bottom:0}.resource-name{font-weight:650}.resource-id{color:#97a1af;font-size:11px;margin-top:2px}.badge{display:inline-flex;align-items:center;gap:6px;border-radius:99px;padding:4px 8px;font-size:11px;font-weight:650;background:#eaf7f2;color:var(--green)}.badge:before{content:"";width:6px;height:6px;border-radius:50%;background:currentColor}.badge.low{background:#fff4e6;color:var(--amber)}.badge.out{background:#fff0f1;color:var(--red)}.empty{text-align:center;padding:32px;color:var(--muted)}
    .side{display:grid;gap:16px}.form-panel h2{font-size:16px;margin:0}.form-panel .subtle{margin-bottom:16px}.tabs{display:flex;background:#f1f3f8;padding:4px;border-radius:10px;margin-bottom:17px}.tab{border:0;background:transparent;border-radius:7px;flex:1;padding:8px;color:var(--muted);font-weight:650;font-size:12px}.tab.active{color:var(--ink);background:#fff;box-shadow:0 1px 4px #14213d13}.field{margin-bottom:12px}.field label{display:block;font-size:11px;color:#5c697b;font-weight:650;margin-bottom:5px}.field input,.field select{width:100%;border:1px solid var(--line);border-radius:9px;padding:10px 11px;background:#fff;color:var(--ink)}.form-message{min-height:18px;margin-top:10px;font-size:12px;color:var(--green)}.form-message.error{color:var(--red)}.loan-list{display:grid;gap:11px}.loan-row{display:flex;justify-content:space-between;gap:10px;align-items:center;border-bottom:1px solid #eff1f5;padding-bottom:10px}.loan-row:last-child{border:0;padding:0}.loan-who{font-size:12px;font-weight:650}.loan-what{font-size:11px;color:var(--muted)}.loan-count{font-weight:700;font-size:12px;color:#394c69;white-space:nowrap}.toast{position:fixed;right:22px;bottom:22px;background:#182538;color:#fff;padding:12px 16px;border-radius:10px;box-shadow:var(--shadow);opacity:0;transform:translateY(10px);transition:.2s;pointer-events:none}.toast.show{opacity:1;transform:none}.footnote{margin-top:17px;color:#9aa4b2;font-size:11px;text-align:center}
    @media(max-width:940px){.layout{grid-template-columns:1fr}.side{grid-template-columns:repeat(2,minmax(0,1fr))}.stats{grid-template-columns:repeat(2,1fr)}}@media(max-width:600px){.topbar{height:62px;padding:0 17px}.top-right .status-text{display:none}main{padding:25px 14px 40px}.heading{align-items:flex-start;flex-direction:column}.heading h1{font-size:26px}.today{display:none}.stats{gap:10px}.stat{padding:15px}.stat-value{font-size:24px}.layout{gap:13px}.panel{padding:16px}.panel-head{align-items:flex-start;flex-direction:column}.actions,.search{width:100%}.side{grid-template-columns:1fr}th,td{padding-left:7px;padding-right:7px}}
  </style>
</head>
<body><div class="shell">
  <header class="topbar"><div class="brand"><div class="brand-mark">✳</div><div>Learn2Earn<small>Campus resource desk</small></div></div><div class="top-right"><span class="live"></span><span class="status-text">Inventory system online</span></div></header>
  <main>
    <section class="heading"><div><div class="eyebrow">Operations overview</div><h1>Resource desk</h1><p>Keep track of campus equipment and active loans.</p></div><div class="today" id="today"></div></section>
    <section class="stats" aria-label="Inventory summary"><article class="stat"><div class="stat-label">Total equipment</div><div class="stat-value" id="totalUnits">—</div><div class="stat-foot">units across all resources</div></article><article class="stat"><div class="stat-label">Ready to borrow</div><div class="stat-value" id="availableUnits">—</div><div class="stat-foot">currently available units</div></article><article class="stat"><div class="stat-label">On loan</div><div class="stat-value" id="borrowedUnits">—</div><div class="stat-foot">units with fellows</div></article><article class="stat"><div class="stat-label">Resource types</div><div class="stat-value" id="resourceCount">—</div><div class="stat-foot">items in your catalog</div></article></section>
    <section class="layout"><div class="panel"><div class="panel-head"><div><h2>Equipment inventory</h2><div class="subtle">Availability and stock levels</div></div><div class="actions"><input class="search" id="search" type="search" placeholder="Search equipment…" aria-label="Search equipment"><button class="btn" id="addButton">＋ Add equipment</button></div></div><div class="table-wrap"><table><thead><tr><th>Equipment</th><th>Category</th><th>Total units</th><th>Available</th><th>Status</th></tr></thead><tbody id="resourceRows"></tbody></table></div></div>
      <aside class="side"><section class="panel form-panel"><h2>Quick transaction</h2><div class="subtle">Record an equipment loan or return.</div><div class="tabs"><button class="tab active" data-mode="borrow">Borrow</button><button class="tab" data-mode="return">Return</button></div><form id="transactionForm"><div class="field"><label for="fellow">Fellow</label><select id="fellow" required></select></div><div class="field"><label for="resource">Equipment</label><select id="resource" required></select></div><div class="field"><label for="quantity">Quantity</label><input id="quantity" type="number" min="1" step="1" value="1" required></div><button class="btn full" id="transactionButton" type="submit">Record loan</button><div class="form-message" id="transactionMessage" role="status"></div></form></section>
      <section class="panel"><div class="panel-head"><div><h2>Currently on loan</h2><div class="subtle">Outstanding equipment by fellow</div></div></div><div class="loan-list" id="loanList"></div></section></aside>
    </section><div class="footnote">Learn2Earn · Campus resource management</div>
  </main><div class="toast" id="toast" role="status"></div>
  <dialog id="addDialog" style="border:0;border-radius:15px;padding:24px;width:min(440px,calc(100% - 28px));box-shadow:0 25px 80px #101b2d33"><form id="addForm"><h2 style="margin:0 0 5px">Add equipment</h2><p class="subtle" style="margin:0 0 16px">Create a new item in the resource catalog.</p><div class="field"><label for="newId">Resource ID</label><input id="newId" placeholder="e.g. R004" required></div><div class="field"><label for="newName">Name</label><input id="newName" placeholder="e.g. Projector" required></div><div class="field"><label for="newCategory">Category</label><input id="newCategory" placeholder="e.g. Electronics" required></div><div class="field"><label for="newTotal">Total units</label><input id="newTotal" type="number" min="1" step="1" value="1" required></div><div style="display:flex;gap:9px;justify-content:flex-end;margin-top:18px"><button type="button" class="btn secondary" id="cancelAdd">Cancel</button><button class="btn" type="submit">Add equipment</button></div><div id="addMessage" class="form-message" role="status"></div></form></dialog>
  <script>
    const $ = selector => document.querySelector(selector);
    let state = null, mode = 'borrow', toastTimer;
    $('#today').textContent = new Intl.DateTimeFormat(undefined,{weekday:'long',month:'long',day:'numeric',year:'numeric'}).format(new Date());
    function esc(value){return String(value).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
    async function api(path, body){const response=await fetch(path,{method:body?'POST':'GET',headers:body?{'Content-Type':'application/json'}:{},body:body?JSON.stringify(body):undefined});const data=await response.json();if(!response.ok)throw new Error(data.error||'Something went wrong.');return data}
    function showToast(message){const box=$('#toast');box.textContent=message;box.classList.add('show');clearTimeout(toastTimer);toastTimer=setTimeout(()=>box.classList.remove('show'),2800)}
    function render(){if(!state)return;const resources=state.resources,filter=$('#search').value.trim().toLowerCase();const shown=resources.filter(r=>(r.name+' '+r.category+' '+r.id).toLowerCase().includes(filter));
      $('#totalUnits').textContent=state.summary.total;$('#availableUnits').textContent=state.summary.available;$('#borrowedUnits').textContent=state.summary.borrowed;$('#resourceCount').textContent=resources.length;
      $('#resourceRows').innerHTML=shown.length?shown.map(r=>{const status=r.available===0?['Out of stock','out']:r.available<3?['Low stock','low']:['In stock',''];return `<tr><td><div class="resource-name">${esc(r.name)}</div><div class="resource-id">${esc(r.id)}</div></td><td>${esc(r.category)}</td><td>${r.total}</td><td><strong>${r.available}</strong></td><td><span class="badge ${status[1]}">${status[0]}</span></td></tr>`}).join(''):`<tr><td colspan="5" class="empty">${resources.length?'No equipment matches your search.':'No equipment has been added yet.'}</td></tr>`;
      const fellowSelect=$('#fellow'), previousFellow=fellowSelect.value;fellowSelect.innerHTML=state.fellows.map(f=>`<option value="${esc(f.id)}">${esc(f.name)}</option>`).join('');if(state.fellows.some(f=>f.id===previousFellow))fellowSelect.value=previousFellow;
      const select=$('#resource'), previous=select.value, choices=mode==='borrow'?resources.filter(r=>r.available>0):state.loans.filter(l=>l.fellow_id===fellowSelect.value);select.innerHTML=choices.map(r=>{const id=r.id||r.resource_id;const item=resources.find(x=>x.id===id);return `<option value="${esc(id)}">${esc(item?.name||r.name)} (${mode==='borrow'?r.available+' available':r.quantity+' on loan'})</option>`}).join('');if(choices.some(r=>(r.id||r.resource_id)===previous))select.value=previous;
      $('#resource').disabled=!choices.length;$('#transactionButton').disabled=!choices.length;$('#transactionButton').textContent=mode==='borrow'?'Record loan':'Record return';
      const loans=state.loans;$('#loanList').innerHTML=loans.length?loans.map(l=>`<div class="loan-row"><div><div class="loan-who">${esc(l.fellow_name)}</div><div class="loan-what">${esc(l.resource_name)}</div></div><div class="loan-count">${l.quantity} unit${l.quantity===1?'':'s'}</div></div>`).join(''):'<div class="empty" style="padding:15px 0">Nothing is currently on loan.</div>';
    }
    async function refresh(){state=await api('/api/state');render()}
    $('#search').addEventListener('input',render);document.querySelectorAll('.tab').forEach(tab=>tab.addEventListener('click',()=>{mode=tab.dataset.mode;document.querySelectorAll('.tab').forEach(t=>t.classList.toggle('active',t===tab));$('#transactionMessage').textContent='';render()}));$('#fellow').addEventListener('change',render);
    $('#transactionForm').addEventListener('submit',async event=>{event.preventDefault();const message=$('#transactionMessage');message.className='form-message';message.textContent='';try{const result=await api('/api/'+mode,{fellow_id:$('#fellow').value,resource_id:$('#resource').value,quantity:Number($('#quantity').value)});message.textContent=result.message;await refresh();showToast(result.message)}catch(error){message.classList.add('error');message.textContent=error.message}});
    $('#addButton').addEventListener('click',()=>{$('#addMessage').textContent='';$('#addDialog').showModal()});$('#cancelAdd').addEventListener('click',()=>$('#addDialog').close());$('#addForm').addEventListener('submit',async event=>{event.preventDefault();const message=$('#addMessage');message.className='form-message';message.textContent='';try{const result=await api('/api/resources',{id:$('#newId').value,name:$('#newName').value,category:$('#newCategory').value,total:Number($('#newTotal').value)});$('#addDialog').close();event.target.reset();$('#newTotal').value='1';await refresh();showToast(result.message)}catch(error){message.classList.add('error');message.textContent=error.message}});
    refresh().catch(error=>showToast(error.message));setInterval(()=>refresh().catch(()=>{}),30000);
  </script>
</div></body></html>'''


def snapshot():
    """Return UI-ready data while callers hold LOCK."""
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
    server_version = "Learn2Earn/1.0"

    def respond(self, status, payload, content_type="application/json; charset=utf-8"):
        body = payload.encode("utf-8") if isinstance(payload, str) else json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
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
                self.respond(200, snapshot())
        else:
            self.respond(404, {"error": "Page not found."})

    def do_POST(self):
        path = urlparse(self.path).path
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

    def log_message(self, fmt, *args):
        print("%s - %s" % (self.address_string(), fmt % args))


def main():
    inventory.load_data()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Learn2Earn is running at http://{HOST}:{PORT}")
    print("Press Ctrl+C to stop the server.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Learn2Earn...")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
