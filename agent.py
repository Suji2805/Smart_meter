"""
AI Smart Energy Meter — agent.py CLOUD VERSION
===============================================
Deployed on Railway.app — runs 24/7 without laptop
"""

from flask import Flask, request, jsonify, render_template_string
import threading, requests, time, csv, os, io, warnings
from datetime import datetime
from collections import deque
import numpy as np

# ── Safe imports ─────────────────────────────────────────
try:
    from gtts import gTTS
    GTTS_OK = True
    print("[OK] gTTS ready")
except:
    GTTS_OK = False
    print("[WARN] gTTS not available")

try:
    from sklearn.ensemble import IsolationForest
    SKLEARN_OK = True
    print("[OK] sklearn ready")
except:
    SKLEARN_OK = False
    print("[WARN] sklearn not available — Z-score only")

# ✅ pyttsx3 REMOVED — does not work on cloud servers (no audio device)
VOICE_OK = False

warnings.filterwarnings('ignore')
app = Flask(__name__)

# ══════════════════════════════════════════════════════════
#   CONFIGURATION
# ══════════════════════════════════════════════════════════
TELEGRAM_BOT_TOKEN = "8403644829:AAFLt4BVV7AmAjRgJXS3_hcHJpVpC9WN9oc"
TELEGRAM_CHAT_ID   = "5784033458"
TELEGRAM_ENABLED   = True
ALERT_COOLDOWN_SEC = 30
LOG_FILE           = "energy_log.csv"

# ── Data storage ─────────────────────────────────────────
readings_current = deque(maxlen=100)
readings_voltage = deque(maxlen=100)
readings_power   = deque(maxlen=100)
readings_time    = deque(maxlen=100)

total_readings   = 0
theft_count      = 0
overload_count   = 0
last_alert_time  = 0
last_alert_type  = "NORMAL"
energy_total     = 0.0
last_energy_time = time.time()

# ── AI Detector ──────────────────────────────────────────
class TheftDetector:
    def __init__(self):
        self.mean_i  = 0.0
        self.std_i   = 0.0
        self.mean_p  = 0.0
        self.std_p   = 0.0
        self.ready   = False
        self.trained = False
        self.history = []
        self.iso     = None

    def update_baseline(self):
        if len(readings_current) >= 30:
            self.mean_i = float(np.mean(readings_current))
            self.std_i  = float(np.std(readings_current))
            self.mean_p = float(np.mean(readings_power))
            self.std_p  = float(np.std(readings_power))
            if self.std_i > 0.05:
                self.ready = True

    def check(self, current, voltage, power):
        if not self.ready:
            return False, "Learning", f"Collecting {len(readings_current)}/30"
        z_i = abs((current - self.mean_i) / (self.std_i + 0.001))
        z_p = abs((power   - self.mean_p) / (self.std_p + 0.001))
        if z_i > 3.0:
            return True, "Z-Score", f"Current {z_i:.1f} sigma above normal"
        if z_p > 3.0:
            return True, "Z-Score", f"Power {z_p:.1f} sigma above normal"
        if SKLEARN_OK:
            self.history.append([current, voltage, power])
            if len(self.history) >= 30:
                if len(self.history) % 10 == 0 or not self.trained:
                    try:
                        self.iso = IsolationForest(contamination=0.1, random_state=42)
                        self.iso.fit(self.history)
                        self.trained = True
                    except: pass
                if self.trained and self.iso:
                    try:
                        if self.iso.predict([[current, voltage, power]])[0] == -1:
                            return True, "ML", "ML model flagged unusual pattern"
                    except: pass
        return False, "Normal", f"I:{z_i:.1f}σ P:{z_p:.1f}σ normal"

    def info(self):
        return {
            "ready":   self.ready,
            "trained": self.trained,
            "samples": len(self.history),
            "mean_i":  round(self.mean_i, 3),
            "mean_p":  round(self.mean_p, 1)
        }

detector = TheftDetector()

# ── Telegram text ────────────────────────────────────────
def send_telegram_text(atype, detail):
    if not TELEGRAM_ENABLED: return
    icons = {"THEFT":"🚨","OVERLOAD":"⚡","OVERVOLTAGE":"📈","UNDERVOLTAGE":"📉"}
    msg = (
        f"{icons.get(atype,'⚠️')} *{atype} ALERT*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"{detail}\n"
        f"🕐 {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Check immediately!"
    )
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"},
            timeout=5
        )
        print("[OK] Telegram text sent")
    except Exception as e:
        print(f"[WARN] Telegram text: {e}")

# ── Telegram voice note ──────────────────────────────────
def send_telegram_voice(atype, current, power):
    if not TELEGRAM_ENABLED or not GTTS_OK: return
    msgs = {
        "THEFT":
            f"Warning! Power theft detected! "
            f"Abnormal current of {current:.1f} amperes. "
            f"Please investigate immediately!",
        "OVERLOAD":
            f"Warning! Power overload! "
            f"Current power is {power:.0f} watts. Check now!",
        "OVERVOLTAGE":
            f"Warning! Overvoltage detected! "
            f"Voltage exceeded 260 volts!",
        "UNDERVOLTAGE":
            f"Warning! Undervoltage detected! "
            f"Voltage dropped below 200 volts!"
    }
    text = msgs.get(atype, f"Warning! {atype} detected!")
    def _send():
        try:
            tts = gTTS(text=text, lang='en', slow=False)
            buf = io.BytesIO()
            tts.write_to_fp(buf)
            buf.seek(0)
            r = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendVoice",
                data={"chat_id": TELEGRAM_CHAT_ID},
                files={"voice": ("alert.ogg", buf, "audio/ogg")},
                timeout=15
            )
            if r.status_code == 200:
                print("[OK] Telegram voice sent!")
            else:
                print(f"[WARN] Voice failed: {r.status_code}")
        except Exception as e:
            print(f"[WARN] Voice error: {e}")
    threading.Thread(target=_send, daemon=True).start()

# ── Energy calculation ───────────────────────────────────
def update_energy(power):
    global energy_total, last_energy_time
    now = time.time()
    energy_total += (power * (now - last_energy_time)) / 3600000.0
    last_energy_time = now

# ── CSV logging ──────────────────────────────────────────
def log_csv(ts, c, v, p, e, status):
    exists = os.path.isfile(LOG_FILE)
    try:
        with open(LOG_FILE, 'a', newline='') as f:
            w = csv.writer(f)
            if not exists:
                w.writerow(['Time','Current','Voltage','Power','Energy','Status'])
            w.writerow([ts, round(c,3), round(v,1), round(p,1), round(e,4), status])
    except: pass

# ── Dashboard HTML ───────────────────────────────────────
DASH = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#0f172a">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<title>AI Energy Meter</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Segoe UI',sans-serif;background:#0f172a;color:#e2e8f0;padding:12px}
h1{text-align:center;color:#60a5fa;font-size:1.1rem;font-weight:800;margin-bottom:2px}
.sub{text-align:center;color:#475569;font-size:.72rem;margin-bottom:10px}
.live{text-align:center;color:#22c55e;font-size:.7rem;margin-bottom:12px;display:flex;align-items:center;justify-content:center;gap:5px}
.dot{width:7px;height:7px;border-radius:50%;background:#22c55e;animation:blink 1s infinite}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.2}}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:9px;margin-bottom:10px}
.card{background:#1e293b;border-radius:11px;padding:13px 9px;border:1px solid #334155;text-align:center}
.lbl{color:#64748b;font-size:.67rem;text-transform:uppercase;letter-spacing:.5px;margin-bottom:4px}
.val{font-size:1.7rem;font-weight:800;font-variant-numeric:tabular-nums;margin-bottom:1px}
.unit{color:#475569;font-size:.63rem}
.blue{color:#3b82f6}.green{color:#22c55e}.yellow{color:#eab308}.purple{color:#a855f7}
.scard{background:#1e293b;border-radius:11px;padding:12px 14px;border:1px solid #334155;margin-bottom:10px}
.badge{display:block;text-align:center;padding:8px;border-radius:8px;font-size:.85rem;font-weight:700;margin-bottom:10px}
.ok{background:#065f46;color:#6ee7b7}
.bad{background:#991b1b;color:#fca5a5;animation:pulse .8s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.6}}
.igrid{display:grid;grid-template-columns:1fr 1fr;gap:6px}
.ibox{background:#0f172a;border-radius:6px;padding:7px 9px}
.ik{color:#475569;font-size:.65rem;margin-bottom:2px}
.iv{color:#93c5fd;font-size:.78rem;font-weight:600}
.chart-card{background:#1e293b;border-radius:11px;padding:12px 14px;border:1px solid #334155;margin-bottom:10px}
.ct{color:#64748b;font-size:.7rem;text-transform:uppercase;letter-spacing:.5px;margin-bottom:8px}
canvas{width:100%;height:80px;display:block}
.log-card{background:#1e293b;border-radius:11px;padding:12px 14px;border:1px solid #334155;margin-bottom:10px}
.lr{display:flex;justify-content:space-between;align-items:center;padding:5px 0;border-bottom:1px solid #0f172a;font-size:.7rem}
.lr:last-child{border:none}
.lt{color:#334155}.lv{color:#93c5fd}
.sNORMAL{color:#22c55e;font-size:.65rem}
.sTHEFT{color:#ef4444;font-weight:700;font-size:.65rem}
.sOVERLOAD{color:#f97316;font-weight:700;font-size:.65rem}
.sOVERVOLTAGE{color:#facc15;font-size:.65rem}
.sUNDERVOLTAGE{color:#a78bfa;font-size:.65rem}
.prog{position:fixed;bottom:0;left:0;right:0;height:3px;background:#1e293b}
.pf{height:100%;background:#3b82f6;transition:width .1s linear}
</style>
</head>
<body>
<h1>⚡ AI Smart Energy Meter</h1>
<p class="sub">Live Dashboard · Auto-refresh every 2s</p>
<div class="live"><div class="dot"></div><span id="lt">Connecting...</span></div>
<div class="grid">
  <div class="card"><div class="lbl">Current</div><div class="val blue" id="cur">--</div><div class="unit">Amperes</div></div>
  <div class="card"><div class="lbl">Voltage</div><div class="val green" id="vol">--</div><div class="unit">Volts</div></div>
  <div class="card"><div class="lbl">Power</div><div class="val yellow" id="pow">--</div><div class="unit">Watts</div></div>
  <div class="card"><div class="lbl">Energy</div><div class="val purple" id="eng">--</div><div class="unit">kWh</div></div>
</div>
<div class="scard">
  <div class="badge ok" id="badge">✓ System Normal</div>
  <div class="igrid">
    <div class="ibox"><div class="ik">AI Status</div><div class="iv" id="ai1">--</div></div>
    <div class="ibox"><div class="ik">Samples</div><div class="iv" id="ai2">--</div></div>
    <div class="ibox"><div class="ik">Base Current</div><div class="iv" id="ai3">--</div></div>
    <div class="ibox"><div class="ik">Theft Events</div><div class="iv" id="ai4">--</div></div>
    <div class="ibox"><div class="ik">Total Readings</div><div class="iv" id="ai5">--</div></div>
    <div class="ibox"><div class="ik">Base Power</div><div class="iv" id="ai6">--</div></div>
  </div>
</div>
<div class="chart-card">
  <div class="ct">📈 Current — last 20 readings</div>
  <canvas id="chart"></canvas>
</div>
<div class="log-card">
  <div class="ct">📋 Event Log</div>
  <div id="log"><div style="color:#334155;font-size:.72rem;text-align:center;padding:8px">Waiting...</div></div>
</div>
<div class="prog"><div class="pf" id="pf" style="width:100%"></div></div>
<script>
var cd=[],logs=[],ps=Date.now(),IV=2000;
function drawChart(al){
  var c=document.getElementById('chart'),ctx=c.getContext('2d');
  c.width=c.offsetWidth||300;c.height=80;ctx.clearRect(0,0,c.width,c.height);
  var pts=cd.slice(-20);if(pts.length<2)return;
  var mx=Math.max.apply(null,pts)*1.3||1,W=c.width,H=c.height;
  function px(i){return(i/(pts.length-1))*W}
  function py(v){return H-(v/mx)*(H*.82)-(H*.08)}
  ctx.fillStyle=al?'rgba(239,68,68,.1)':'rgba(59,130,246,.1)';
  ctx.beginPath();ctx.moveTo(0,H);
  pts.forEach(function(v,i){ctx.lineTo(px(i),py(v))});
  ctx.lineTo(W,H);ctx.closePath();ctx.fill();
  ctx.strokeStyle=al?'#ef4444':'#3b82f6';ctx.lineWidth=2;ctx.lineJoin='round';
  ctx.beginPath();
  pts.forEach(function(v,i){i===0?ctx.moveTo(px(i),py(v)):ctx.lineTo(px(i),py(v))});
  ctx.stroke();
}
function addLog(ts,c,v,p,s){
  logs.unshift({ts:ts,c:c,v:v,p:p,s:s});if(logs.length>20)logs.pop();
  document.getElementById('log').innerHTML=logs.map(function(r){
    return "<div class='lr'><span class='lt'>"+r.ts+"</span><span class='lv'>"+
    (r.c||0).toFixed(2)+"A "+(r.v||0).toFixed(0)+"V "+(r.p||0).toFixed(0)+
    "W</span><span class='s"+r.s+"'>"+r.s+"</span></div>";
  }).join('');
}
function upProg(){
  var p=Math.min(100,(Date.now()-ps)/IV*100);
  document.getElementById('pf').style.width=(100-p)+'%';
  requestAnimationFrame(upProg);
}
upProg();
async function poll(){
  ps=Date.now();
  try{
    var r=await fetch('/status'),d=await r.json();
    document.getElementById('cur').textContent=d.current!=null?d.current.toFixed(2):'--';
    document.getElementById('vol').textContent=d.voltage!=null?d.voltage.toFixed(1):'--';
    document.getElementById('pow').textContent=d.power!=null?d.power.toFixed(0):'--';
    document.getElementById('eng').textContent=d.energy!=null?d.energy.toFixed(4):'--';
    document.getElementById('lt').textContent='Live — '+new Date().toLocaleTimeString();
    var al=(d.alert_type&&d.alert_type!='NORMAL');
    var b=document.getElementById('badge');
    b.textContent=al?('⚠ '+d.alert_type+' DETECTED!'):'✓ System Normal';
    b.className='badge '+(al?'bad':'ok');
    var inf=d.info||{};
    document.getElementById('ai1').textContent=inf.trained?'Z+ML Active':(inf.ready?'Z-Score':'Learning...');
    document.getElementById('ai2').textContent=inf.samples||0;
    document.getElementById('ai3').textContent=(inf.mean_i||0)+' A';
    document.getElementById('ai4').textContent=d.theft_count||0;
    document.getElementById('ai5').textContent=d.total_readings||0;
    document.getElementById('ai6').textContent=(inf.mean_p||0)+' W';
    if(d.current!=null){cd.push(d.current);drawChart(al);}
    if(d.log_entry){var e=d.log_entry;addLog(e.ts,e.c,e.v,e.p,e.s);}
  }catch(e){document.getElementById('lt').textContent='Reconnecting...';}
  setTimeout(poll,IV);
}
poll();
</script>
</body>
</html>"""

# ══════════════════════════════════════════════════════════
#   FLASK ROUTES
# ══════════════════════════════════════════════════════════

@app.route('/')
def dashboard():
    return render_template_string(DASH)


@app.route('/data', methods=['POST'])
def receive_data():
    global total_readings, theft_count, overload_count
    global last_alert_time, last_alert_type

    try:
        data = request.get_json(force=True)
        if not data:
            return jsonify({"status": "error"}), 400

        current = float(data.get('current', 0))
        voltage = float(data.get('voltage', 230))
        power   = float(data.get('power', current * voltage))

        readings_current.append(current)
        readings_voltage.append(voltage)
        readings_power.append(power)
        readings_time.append(datetime.now().strftime('%H:%M:%S'))

        update_energy(power)
        total_readings += 1
        detector.update_baseline()

        print(f"\n[{datetime.now().strftime('%H:%M:%S')}] "
              f"I={current:.3f}A  V={voltage:.1f}V  "
              f"P={power:.1f}W  E={energy_total:.4f}kWh")

        alert      = False
        alert_type = "NORMAL"
        detail     = ""

        if voltage > 260:
            alert = True
            alert_type = "OVERVOLTAGE"
            detail = f"Voltage {voltage:.1f}V exceeds 260V"
        elif 10 < voltage < 200:
            alert = True
            alert_type = "UNDERVOLTAGE"
            detail = f"Voltage {voltage:.1f}V below 200V"
        elif current > 18.0:
            alert = True
            alert_type = "OVERLOAD"
            detail = f"Current {current:.2f}A exceeds 18A"
            overload_count += 1
        else:
            is_anomaly, method, msg = detector.check(current, voltage, power)
            print(f"[AI] {method}: {msg}")
            if is_anomaly:
                alert = True
                alert_type = "THEFT"
                detail = f"{method}: {msg}"
                theft_count += 1

        last_alert_type = alert_type

        now = time.time()
        if alert and (now - last_alert_time) > ALERT_COOLDOWN_SEC:
            last_alert_time = now
            print(f"[ALERT] {alert_type} — {detail}")
            # ✅ No laptop voice on cloud — Telegram only
            send_telegram_text(alert_type, detail)
            send_telegram_voice(alert_type, current, power)

        log_csv(
            datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            current, voltage, power, energy_total,
            alert_type if alert else "NORMAL"
        )

        return jsonify({
            "status": "ok",
            "alert":  alert,
            "type":   alert_type
        }), 200

    except Exception as e:
        print(f"[ERROR] {e}")
        return jsonify({"status": "error"}), 500


@app.route('/status')
def get_status():
    c = readings_current[-1] if readings_current else 0
    v = readings_voltage[-1] if readings_voltage else 0
    p = readings_power[-1]   if readings_power   else 0
    t = readings_time[-1]    if readings_time     else ""
    return jsonify({
        "current":        round(float(c), 3),
        "voltage":        round(float(v), 1),
        "power":          round(float(p), 1),
        "energy":         round(energy_total, 4),
        "alert_type":     last_alert_type,
        "theft_detected": last_alert_type == "THEFT",
        "total_readings": total_readings,
        "theft_count":    theft_count,
        "info":           detector.info(),
        "log_entry":      {
            "ts": t, "c": float(c),
            "v": float(v), "p": float(p),
            "s": last_alert_type
        } if t else None
    })

# ── Health check for Railway ─────────────────────────────
@app.route('/health')
def health():
    return jsonify({"status": "ok"}), 200


# ══════════════════════════════════════════════════════════
#   MAIN — ✅ Port from environment variable for Railway
# ══════════════════════════════════════════════════════════
if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    print("\n" + "=" * 60)
    print("  AI SMART ENERGY METER — CLOUD SERVER")
    print("=" * 60)
    print(f"  Running on port     : {port}")
    print(f"  Telegram            : {'ON' if TELEGRAM_ENABLED else 'OFF'}")
    print(f"  sklearn ML          : {'ON' if SKLEARN_OK else 'OFF (Z-score only)'}")
    print(f"  CSV log             : {LOG_FILE}")
    print("=" * 60)
    print("\n  Waiting for NodeMCU to connect...\n")
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
