"""
app.py — local web UI for the AI Test Case Generator.

    pip install flask
    python app.py          ->  open http://127.0.0.1:5000

It runs your existing main.py as a background process, streams its output to the page, and lets you
answer every console question (Y/n, Enter, m/r/s/a, ...) with buttons. Nothing in main.py/tcgen changes.
"""
from __future__ import annotations

import codecs
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_file

import config

ROOT = Path(__file__).resolve().parent
RUNS = (ROOT / config.RUNS_DIR).resolve()
DATA_FILE = (ROOT / config.TEST_DATA_FILE).resolve()
MAIN_CMD = os.environ.get("TCG_MAIN", str(ROOT / "main.py"))      # override only for testing
FILES = ["Automation_Master_Controller.xlsx", "testcase.xlsx", "test_script.py", "testcase.json"]

app = Flask(__name__)
state = {"proc": None, "buf": "", "started": None}
lock = threading.Lock()


def _reader(proc):
    dec = codecs.getincrementaldecoder("utf-8")(errors="replace")
    fd = proc.stdout.fileno()
    while True:
        chunk = os.read(fd, 4096)
        if not chunk:
            break
        with lock:
            state["buf"] += dec.decode(chunk)
    with lock:
        state["buf"] += f"\n[process finished, exit code {proc.wait()}]\n"


def running() -> bool:
    p = state["proc"]
    return p is not None and p.poll() is None


@app.post("/api/start")
def start():
    if running():
        return jsonify(error="A run is already in progress."), 409
    d = request.get_json(force=True)
    url, prompt = (d.get("url") or "").strip(), (d.get("prompt") or "").strip()
    if not url or not prompt:
        return jsonify(error="URL and test description are required."), 400
    cmd = [sys.executable, "-u", MAIN_CMD, "--url", url, "--prompt", prompt,
           "--login", "yes" if d.get("login") else "no"]
    env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.Popen(cmd, cwd=ROOT, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, bufsize=0)
    with lock:
        state.update(proc=proc, buf="")
    threading.Thread(target=_reader, args=(proc,), daemon=True).start()
    return jsonify(ok=True)


@app.post("/api/send")
def send():
    if not running():
        return jsonify(error="No run in progress."), 409
    text = (request.get_json(force=True).get("text") or "")
    try:
        state["proc"].stdin.write((text + "\n").encode("utf-8"))
        state["proc"].stdin.flush()
    except OSError:
        return jsonify(error="Process is not accepting input."), 409
    with lock:
        state["buf"] += f"\n>>> {text if text else '(Enter)'}\n"
    return jsonify(ok=True)


@app.post("/api/stop")
def stop():
    if running():
        state["proc"].terminate()
    return jsonify(ok=True)


@app.get("/api/poll")
def poll():
    pos = int(request.args.get("pos", 0))
    with lock:
        buf = state["buf"]
    return jsonify(text=buf[pos:], pos=len(buf), running=running())


@app.get("/api/runs")
def runs():
    out = []
    if RUNS.is_dir():
        for d in sorted((p for p in RUNS.iterdir() if p.is_dir()), reverse=True)[:1]:
            files = [f for f in FILES if (d / f).is_file()]
            out.append({"name": d.name, "files": files})
    return jsonify(out)


@app.get("/download/<run>/<name>")
def download(run, name):
    p = (RUNS / run / name).resolve()
    if RUNS not in p.parents or name not in FILES or not p.is_file():
        abort(404)
    return send_file(p, as_attachment=True)


@app.route("/api/testdata", methods=["GET", "POST"])
def testdata():
    if request.method == "GET":
        return jsonify(text=DATA_FILE.read_text(encoding="utf-8") if DATA_FILE.exists() else "{}")
    text = request.get_json(force=True).get("text", "")
    try:
        obj = json.loads(text)
        assert isinstance(obj, dict)
    except Exception:
        return jsonify(error="Must be a valid JSON object, e.g. {\"email\": \"a@b.com\"}"), 400
    DATA_FILE.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return jsonify(ok=True)


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Test Case Generator</title>
<style>
:root{
  --bg:#eef1f6;--surface:#ffffff;--surface-2:#f6f8fb;--ink:#162033;--mute:#6b778c;--line:#e1e6ef;
  --brand:#3b5bdb;--brand-2:#7048e8;--brand-ink:#fff;--ok:#0f9d6b;--warn:#c77700;--bad:#d6402b;
  --con:#0d1320;--con-bar:#151c2e;--con-ink:#c9d4ea;--shadow:0 1px 2px rgba(16,24,40,.05),0 8px 24px -12px rgba(16,24,40,.12)}
@media (prefers-color-scheme:dark){:root{
  --bg:#0b0f19;--surface:#131a29;--surface-2:#0f1523;--ink:#e8edf7;--mute:#8d9ab3;--line:#222c42;
  --brand:#7b93ff;--brand-2:#a58bff;--brand-ink:#0b0f19;--ok:#43d39e;--warn:#f2b45a;--bad:#ff8068;
  --con:#080c15;--con-bar:#0f1523;--con-ink:#c4cfe6;--shadow:0 1px 2px rgba(0,0,0,.4),0 8px 24px -12px rgba(0,0,0,.6)}}
*{box-sizing:border-box}
html,body{margin:0}
body{background:var(--bg);color:var(--ink);font:14.5px/1.5 Inter,system-ui,-apple-system,"Segoe UI",sans-serif;-webkit-font-smoothing:antialiased}
.wrap{max-width:1180px;margin:0 auto;padding:0 20px 40px}
header{display:flex;align-items:center;gap:14px;padding:22px 0 18px}
.logo{width:40px;height:40px;border-radius:11px;background:linear-gradient(135deg,var(--brand),var(--brand-2));display:grid;place-items:center;color:#fff;box-shadow:0 6px 16px -6px var(--brand)}
.logo svg{width:22px;height:22px}
header h1{font-size:19px;margin:0;letter-spacing:-.01em}
header p{margin:0;color:var(--mute);font-size:13px}
header .sp{flex:1}
.grid{display:grid;grid-template-columns:350px minmax(0,1fr);gap:18px;align-items:start}
@media(max-width:900px){.grid{grid-template-columns:1fr}}
.col{display:flex;flex-direction:column;gap:18px;min-width:0}
.card{background:var(--surface);border:1px solid var(--line);border-radius:14px;box-shadow:var(--shadow)}
.card>.hd{display:flex;align-items:center;justify-content:space-between;padding:14px 18px;border-bottom:1px solid var(--line)}
.card>.hd h2{margin:0;font-size:13.5px;font-weight:650;letter-spacing:.01em}
.card>.bd{padding:16px 18px}
label.f{display:block;font-size:12.5px;font-weight:550;color:var(--mute);margin:0 0 6px}
.field{margin-bottom:14px}
input[type=text],textarea{width:100%;padding:10px 12px;border:1px solid var(--line);border-radius:10px;background:var(--surface-2);color:var(--ink);font:inherit;outline:none;transition:border-color .15s,box-shadow .15s}
input[type=text]:focus,textarea:focus{border-color:var(--brand);box-shadow:0 0 0 3px color-mix(in srgb,var(--brand) 22%,transparent)}
textarea{min-height:96px;resize:vertical}
textarea.mono{font:12.5px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;min-height:120px}
.switch{display:flex;align-items:center;gap:10px;cursor:pointer;user-select:none;margin:2px 0 16px}
.switch input{position:absolute;opacity:0}
.switch i{width:38px;height:22px;border-radius:99px;background:var(--line);position:relative;transition:.2s;flex:none}
.switch i::after{content:"";position:absolute;top:3px;left:3px;width:16px;height:16px;border-radius:50%;background:#fff;transition:.2s;box-shadow:0 1px 2px rgba(0,0,0,.25)}
.switch input:checked+i{background:var(--brand)}
.switch input:checked+i::after{left:19px}
.switch input:focus-visible+i{box-shadow:0 0 0 3px color-mix(in srgb,var(--brand) 30%,transparent)}
.row{display:flex;gap:10px;align-items:center}
button{font:inherit;font-weight:550;padding:9px 16px;border-radius:10px;border:1px solid var(--line);background:var(--surface);color:var(--ink);cursor:pointer;transition:.15s}
button:hover:not(:disabled){border-color:var(--brand);color:var(--brand)}
button.primary{background:linear-gradient(135deg,var(--brand),var(--brand-2));color:var(--brand-ink);border:0;box-shadow:0 6px 14px -6px var(--brand)}
button.primary:hover:not(:disabled){color:var(--brand-ink);filter:brightness(1.07)}
button.danger{color:var(--bad)}button.danger:hover:not(:disabled){border-color:var(--bad);color:var(--bad)}
button:disabled{opacity:.45;cursor:not-allowed}
button.block{flex:1}
.pill{display:inline-flex;align-items:center;gap:7px;padding:4px 11px;border-radius:99px;font-size:12px;font-weight:600;background:var(--surface-2);border:1px solid var(--line);color:var(--mute)}
.pill::before{content:"";width:7px;height:7px;border-radius:50%;background:currentColor}
.pill.run{color:var(--brand)}.pill.run::before{animation:pulse 1.1s infinite}
.pill.done{color:var(--ok)}
@keyframes pulse{50%{opacity:.25}}
.hint{font-size:12px;color:var(--mute);margin-top:12px;line-height:1.45}
/* console */
.term{border-radius:0 0 14px 14px;overflow:hidden}
.termbar{display:flex;align-items:center;gap:7px;background:var(--con-bar);padding:9px 14px}
.termbar i{width:10px;height:10px;border-radius:50%;background:#ff5f57}.termbar i:nth-child(2){background:#febc2e}.termbar i:nth-child(3){background:#28c840}
.termbar span{margin-left:8px;font:12px ui-monospace,Menlo,monospace;color:#7d8aa5}
#con{background:var(--con);color:var(--con-ink);padding:14px 16px;height:430px;overflow:auto;font:12.5px/1.6 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;white-space:pre-wrap;word-break:break-word}
#con .l{display:block}
#con .ok{color:#4ade9a}#con .bad{color:#ff7b6b}#con .h{color:#8fa8ff;font-weight:700}#con .you{color:#f5c26b}#con .dim{color:#6f7d99}#con .tot{color:#c4a8ff;font-weight:700}
#answers{display:flex;gap:8px;flex-wrap:wrap;padding:12px 18px 0;min-height:12px}
#answers button{padding:8px 16px}
.inputbar{display:flex;gap:10px;padding:12px 18px 16px}
.tok{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;padding:0 18px 18px}
.tok div{border:1px solid var(--line);border-radius:12px;padding:12px 14px;background:var(--surface-2)}
.tok b{display:block;font-size:22px;letter-spacing:-.02em}.tok span{font-size:12px;color:var(--mute)}
.tok div:nth-child(3) b{background:linear-gradient(135deg,var(--brand),var(--brand-2));-webkit-background-clip:text;background-clip:text;color:transparent}
/* previous runs: fixed height + scrollbar */
#runs{max-height:300px;overflow-y:auto;padding:6px 18px;scrollbar-width:thin;scrollbar-color:var(--line) transparent}
#runs::-webkit-scrollbar{width:8px}#runs::-webkit-scrollbar-thumb{background:var(--line);border-radius:8px}
.run{padding:12px 0;border-bottom:1px solid var(--line)}.run:last-child{border-bottom:0}
.run .n{font-size:12.5px;font-weight:600;word-break:break-all;margin-bottom:7px}
.chips{display:flex;gap:6px;flex-wrap:wrap}
.chips a{font-size:12px;font-weight:550;text-decoration:none;color:var(--brand);background:color-mix(in srgb,var(--brand) 10%,transparent);padding:3px 10px;border-radius:99px}
.chips a:hover{background:color-mix(in srgb,var(--brand) 20%,transparent)}
.empty{color:var(--mute);font-size:13px;padding:12px 0}
</style></head><body><div class="wrap">
<header>
  <div class="logo"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 11l3 3 8-8"/><path d="M20 12v6a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h9"/></svg></div>
  <div><h1>Test Case Generator</h1><p>Describe a test, watch the AI build it, download the Excel.</p></div>
  <div class="sp"></div><span id="state" class="pill">idle</span>
</header>
<div class="grid">
 <div class="col">
  <section class="card"><div class="hd"><h2>New test</h2></div><div class="bd">
   <div class="field"><label class="f" for="url">URL to test</label><input type="text" id="url" placeholder="https://example.com/page"></div>
   <div class="field"><label class="f" for="prompt">What should be tested?</label><textarea id="prompt" placeholder="e.g. Create a remark with sample data and verify it appears in the list"></textarea></div>
   <label class="switch"><input type="checkbox" id="login"><i></i><span>This site needs login first</span></label>
   <div class="row"><button class="primary block" id="go">Start</button><button class="danger" id="stop" disabled>Stop</button></div>
   <p class="hint">If login is needed, sign in inside the Firefox window that opens, then press “Continue” on the right.</p>
  </div></section>
  <section class="card"><div class="hd"><h2>Test data</h2><span class="hint" id="tdmsg" style="margin:0"></span></div><div class="bd">
   <textarea id="td" class="mono" spellcheck="false"></textarea>
   <div class="row" style="margin-top:10px"><button id="savetd">Save test_data.json</button></div>
  </div></section>
 </div>
 <div class="col">
  <section class="card"><div class="hd"><h2>Live output</h2></div>
   <div class="term"><div class="termbar"><i></i><i></i><i></i><span>generator</span></div><div id="con"><span class="dim">Nothing running yet.</span></div></div>
   <div id="answers"></div>
   <div class="inputbar"><input type="text" id="line" placeholder="Type an answer, or just press Enter to confirm" style="flex:1"><button id="send">Send</button></div>
   <div class="tok" id="tok" hidden><div><b id="tin">–</b><span>Input tokens</span></div><div><b id="tout">–</b><span>Output tokens</span></div><div><b id="tall">–</b><span>Total tokens</span></div></div>
  </section>
  <section class="card"><div class="hd"><h2>Previous runs</h2><span class="hint" style="margin:0">Most recent</span></div><div id="runs"></div></section>
 </div>
</div></div>
<script>
const $=id=>document.getElementById(id);let pos=0,full="",busy=false,tail="";
const api=(u,b)=>fetch(u,b?{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(b)}:{}).then(r=>r.json());
const esc=s=>s.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
function cls(l){
 if(/TOTAL TOKENS/.test(l))return"tot";
 if(/->\s*PASS|STATUS\s*:\s*Passed|\bPASSED\b/.test(l))return"ok";
 if(/->\s*FAIL|ABORTED|error:|FAILED|Setup problem/.test(l))return"bad";
 if(/^==|^\[\d\]|TEST CASE:|^={6,}|LOGIN:/.test(l.trim()))return"h";
 if(/^>>>/.test(l))return"you";
 if(/^\s*\[capture|^\s*!|process finished/.test(l))return"dim";
 return"";}
function render(text){const c=$("con");
 const parts=(tail+text).split("\n");tail=parts.pop();           // keep partial last line
 let html="";for(const l of parts)html+=`<span class="l ${cls(l)}">${esc(l)||" "}</span>`;
 c.querySelectorAll(".partial").forEach(e=>e.remove());
 c.insertAdjacentHTML("beforeend",html+(tail?`<span class="l partial">${esc(tail)}</span>`:""));}
function answers(){
 const el=$("answers");el.innerHTML="";if(!busy)return;const t=full.slice(-300);let opts=null;
 if(/\[m\/r\/s\/a\]/.test(t)&&!/\n\s*$/.test(t))opts=[["m","I'll do it manually"],["r","AI retry"],["s","Skip step"],["a","Abort"]];
 else if(/\[(Y\/n|y\/N)\]\s*:\s*$/.test(t))opts=[["y","Yes"],["n","No"]];
 else if(/press enter/i.test(t.trim().slice(-80))&&!/\n\s*$/.test(t))opts=[["","Continue (Enter)"]];
 (opts||[]).forEach(([v,x])=>{const b=document.createElement("button");b.textContent=x;if(v===""||v==="y")b.className="primary";b.onclick=()=>send(v);el.appendChild(b)});}
function tokens(){const m=full.match(/TOTAL TOKENS\s*:\s*input\s*([\d,]+)\s*\|\s*output\s*([\d,]+)\s*\|\s*total\s*([\d,]+)/);
 if(m){$("tok").hidden=false;$("tin").textContent=m[1];$("tout").textContent=m[2];$("tall").textContent=m[3]}}
async function send(t){await api("/api/send",{text:t});$("line").value=""}
async function tick(){
 const r=await api("/api/poll?pos="+pos);
 if(r.text){const c=$("con"),atEnd=c.scrollTop+c.clientHeight>c.scrollHeight-60;
  if(pos===0&&!full){c.innerHTML="";tail=""}render(r.text);full+=r.text;if(atEnd)c.scrollTop=c.scrollHeight}
 pos=r.pos;const was=busy;busy=r.running;
 $("go").disabled=busy;$("stop").disabled=!busy;
 $("state").textContent=busy?"running":(full?"finished":"idle");$("state").className="pill "+(busy?"run":(full?"done":""));
 answers();tokens();if(was&&!busy)loadRuns();}
$("go").onclick=async()=>{const r=await api("/api/start",{url:$("url").value,prompt:$("prompt").value,login:$("login").checked});
 if(r.error)return alert(r.error);pos=0;full="";tail="";$("tok").hidden=true;$("con").innerHTML='<span class="dim">Starting…</span>'};
$("stop").onclick=()=>api("/api/stop",{});
$("send").onclick=()=>send($("line").value);$("line").onkeydown=e=>{if(e.key==="Enter")send($("line").value)};
async function loadRuns(){const r=await api("/api/runs");$("runs").innerHTML=r.length?r.map(x=>
 `<div class="run"><div class="n">${x.name}</div><div class="chips">${x.files.map(f=>`<a href="/download/${x.name}/${f}">${f}</a>`).join("")||'<span class="empty">no files</span>'}</div></div>`).join(""):'<div class="empty">No runs yet.</div>'}
async function loadTd(){$("td").value=(await api("/api/testdata")).text}
$("savetd").onclick=async()=>{const r=await api("/api/testdata",{text:$("td").value});$("tdmsg").textContent=r.error||"Saved ✓";setTimeout(()=>$("tdmsg").textContent="",3000)};
loadRuns();loadTd();setInterval(tick,600);
</script></div></body></html>
"""


@app.get("/")
def index():
    return PAGE


if __name__ == "__main__":
    print("Open http://127.0.0.1:5057")
    app.run(host="127.0.0.1", port=5057, threaded=True)