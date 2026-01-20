import shlex
import subprocess
from dataclasses import dataclass
from typing import Dict, List, Tuple

from flask import Flask, render_template_string, request


app = Flask(__name__)
app.secret_key = "badnetwork-demo-key"


@dataclass
class NetemConfig:
    uplink: str  # eth0 (good internet)
    downlink: str  # eth1 (test network)
    delay_ms: str
    jitter_ms: str
    loss_pct: str
    duplicate_pct: str
    corrupt_pct: str
    rate_kbit: str


DEFAULTS = NetemConfig(
    uplink="eth0",
    downlink="eth1",
    delay_ms="500",
    jitter_ms="100",
    loss_pct="5",
    duplicate_pct="0.1",
    corrupt_pct="0.1",
    rate_kbit="1000",  # 1 mbit
)


def to_config(form: Dict[str, str]) -> NetemConfig:
    return NetemConfig(
        uplink=form.get("uplink", DEFAULTS.uplink) or DEFAULTS.uplink,
        downlink=form.get("downlink", DEFAULTS.downlink) or DEFAULTS.downlink,
        delay_ms=form.get("delay_ms", DEFAULTS.delay_ms),
        jitter_ms=form.get("jitter_ms", DEFAULTS.jitter_ms),
        loss_pct=form.get("loss_pct", DEFAULTS.loss_pct),
        duplicate_pct=form.get("duplicate_pct", DEFAULTS.duplicate_pct),
        corrupt_pct=form.get("corrupt_pct", DEFAULTS.corrupt_pct),
        rate_kbit=form.get("rate_kbit", DEFAULTS.rate_kbit),
    )


def quote(value: str) -> str:
    return shlex.quote(value.strip()) if value else ""


def build_netem_clause(cfg: NetemConfig) -> str:
    pieces: List[str] = []
    if cfg.delay_ms:
        if cfg.jitter_ms:
            pieces.append(
                f"delay {cfg.delay_ms}ms {cfg.jitter_ms}ms distribution normal"
            )
        else:
            pieces.append(f"delay {cfg.delay_ms}ms")
    if cfg.loss_pct:
        pieces.append(f"loss {cfg.loss_pct}%")
    if cfg.duplicate_pct:
        pieces.append(f"duplicate {cfg.duplicate_pct}%")
    if cfg.corrupt_pct:
        pieces.append(f"corrupt {cfg.corrupt_pct}%")
    if not pieces:
        pieces.append("delay 0ms")
    return " ".join(pieces)


def build_command(cfg: NetemConfig) -> str:
    uplink = quote(cfg.uplink)
    downlink = quote(cfg.downlink)
    rate = cfg.rate_kbit or DEFAULTS.rate_kbit
    netem_clause = build_netem_clause(cfg)

    steps = [
        "# Enable IPv4 forwarding",
        "sudo sysctl -w net.ipv4.ip_forward=1",
        "# NAT traffic from test network to uplink",
        f"sudo iptables -t nat -F POSTROUTING",
        f"sudo iptables -F FORWARD",
        f"sudo iptables -t nat -A POSTROUTING -o {uplink} -j MASQUERADE",
        f"sudo iptables -A FORWARD -i {downlink} -o {uplink} -m state --state RELATED,ESTABLISHED -j ACCEPT",
        f"sudo iptables -A FORWARD -i {uplink} -o {downlink} -m state --state NEW -j ACCEPT",
        "# Reset old tc rules",
        f"sudo tc qdisc del dev {downlink} root 2>/dev/null",
        "# Shape bandwidth and add netem",
        f"sudo tc qdisc add dev {downlink} root handle 1: htb default 10",
        f"sudo tc class add dev {downlink} parent 1: classid 1:10 htb rate {rate}kbit ceil {rate}kbit",
        f"sudo tc qdisc add dev {downlink} parent 1:10 handle 10: netem {netem_clause}",
    ]
    return "\n".join(steps)


def build_reset_command(cfg: NetemConfig) -> str:
    downlink = quote(cfg.downlink)
    return "\n".join(
        [
            f"sudo tc qdisc del dev {downlink} root 2>/dev/null",
            "sudo iptables -t nat -F POSTROUTING",
            "sudo iptables -F FORWARD",
        ]
    )


def run_script(script: str) -> Tuple[int, str]:
    # Assumes the host is Linux with bash.
    result = subprocess.run(
        ["bash", "-lc", script],
        capture_output=True,
        text=True,
    )
    output = (result.stdout or "") + (result.stderr or "")
    return result.returncode, output.strip()


@app.route("/", methods=["GET", "POST"])
def index():
    cfg = to_config(request.form if request.method == "POST" else {})
    action = request.form.get("action")
    last_output = ""
    last_status = 0

    if request.method == "POST":
        if action == "apply":
            script = build_command(cfg)
            last_status, last_output = run_script(script)
        elif action == "reset":
            script = build_reset_command(cfg)
            last_status, last_output = run_script(script)
        elif action == "clear":
            cfg = NetemConfig(
                uplink=cfg.uplink,
                downlink=cfg.downlink,
                delay_ms="",
                jitter_ms="",
                loss_pct="",
                duplicate_pct="",
                corrupt_pct="",
                rate_kbit="",
            )

    preview_script = build_command(
        NetemConfig(
            uplink=cfg.uplink,
            downlink=cfg.downlink,
            delay_ms=cfg.delay_ms or DEFAULTS.delay_ms,
            jitter_ms=cfg.jitter_ms or DEFAULTS.jitter_ms,
            loss_pct=cfg.loss_pct or DEFAULTS.loss_pct,
            duplicate_pct=cfg.duplicate_pct or DEFAULTS.duplicate_pct,
            corrupt_pct=cfg.corrupt_pct or DEFAULTS.corrupt_pct,
            rate_kbit=cfg.rate_kbit or DEFAULTS.rate_kbit,
        )
    )

    return render_template_string(
        TEMPLATE,
        cfg=cfg,
        preview_script=preview_script,
        last_output=last_output,
        last_status=last_status,
    )


TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <title>Bad Network Lab (tc/netem)</title>
  <style>
    body { font-family: Arial, sans-serif; margin: 24px; max-width: 960px; }
    form { display: grid; grid-template-columns: repeat(2, minmax(220px, 1fr)); gap: 12px 18px; align-items: center; }
    label { font-weight: bold; }
    input { width: 100%; padding: 6px 8px; }
    .wide { grid-column: 1 / -1; }
    textarea { width: 100%; height: 220px; }
    .buttons { display: flex; gap: 12px; margin: 12px 0; }
    button { padding: 8px 14px; cursor: pointer; }
    .output { background: #f7f7f7; border: 1px solid #ccc; padding: 12px; white-space: pre-wrap; }
    .status-ok { color: green; }
    .status-bad { color: #b30000; }
  </style>
</head>
<body>
  <h1>Bad Network Lab (tc/netem)</h1>
  <p>Заполните параметры, посмотрите итоговый скрипт и выполните его на Ubuntu.</p>
  <form method="post">
    <label for="uplink">uplink (eth0):</label>
    <input id="uplink" name="uplink" value="{{ cfg.uplink }}" required />

    <label for="downlink">downlink (eth1):</label>
    <input id="downlink" name="downlink" value="{{ cfg.downlink }}" required />

    <label for="delay_ms">Delay (ms):</label>
    <input id="delay_ms" name="delay_ms" type="number" step="1" min="0" value="{{ cfg.delay_ms }}" />

    <label for="jitter_ms">Jitter (ms):</label>
    <input id="jitter_ms" name="jitter_ms" type="number" step="1" min="0" value="{{ cfg.jitter_ms }}" />

    <label for="loss_pct">Loss (%):</label>
    <input id="loss_pct" name="loss_pct" type="number" step="0.1" min="0" max="100" value="{{ cfg.loss_pct }}" />

    <label for="duplicate_pct">Duplicate (%):</label>
    <input id="duplicate_pct" name="duplicate_pct" type="number" step="0.1" min="0" max="100" value="{{ cfg.duplicate_pct }}" />

    <label for="corrupt_pct">Corrupt (%):</label>
    <input id="corrupt_pct" name="corrupt_pct" type="number" step="0.1" min="0" max="100" value="{{ cfg.corrupt_pct }}" />

    <label for="rate_kbit">Rate (kbit):</label>
    <input id="rate_kbit" name="rate_kbit" type="number" step="1" min="1" value="{{ cfg.rate_kbit }}" />

    <div class="wide">
      <label for="preview_script">Итоговый скрипт (tc + iptables):</label><br />
      <textarea id="preview_script" readonly>{{ preview_script }}</textarea>
    </div>

    <div class="buttons wide">
      <button type="submit" name="action" value="apply">Выполнить</button>
      <button type="submit" name="action" value="reset">Сбросить правила</button>
      <button type="submit" name="action" value="clear">Очистить параметры</button>
    </div>
  </form>

  {% if last_output %}
    <h3>Результат (код {{ last_status }})</h3>
    <div class="output {{ 'status-ok' if last_status == 0 else 'status-bad' }}">{{ last_output }}</div>
  {% endif %}
</body>
</html>
"""


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
