import json
import shlex
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Tuple

from flask import Flask, render_template_string, request


app = Flask(__name__)
app.secret_key = "badnetwork-demo-key"
PRESETS_DIR = Path(__file__).parent / "presets"


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
    delay_enabled: bool = True
    jitter_enabled: bool = True
    loss_enabled: bool = True
    duplicate_enabled: bool = True
    corrupt_enabled: bool = True
    rate_enabled: bool = True


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


def parse_bool(form: Dict[str, str], key: str, default: bool = True) -> bool:
    if not form:
        return default
    return key in form


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
        delay_enabled=parse_bool(form, "delay_enabled"),
        jitter_enabled=parse_bool(form, "jitter_enabled"),
        loss_enabled=parse_bool(form, "loss_enabled"),
        duplicate_enabled=parse_bool(form, "duplicate_enabled"),
        corrupt_enabled=parse_bool(form, "corrupt_enabled"),
        rate_enabled=parse_bool(form, "rate_enabled"),
    )


def quote(value: str) -> str:
    return shlex.quote(value.strip()) if value else ""


def ensure_presets_dir() -> None:
    PRESETS_DIR.mkdir(exist_ok=True)


def sanitize_preset_name(name: str) -> str:
    safe = "".join(c for c in name if c.isalnum() or c in ("-", "_", "."))
    return safe.strip(".")


def list_presets() -> List[str]:
    ensure_presets_dir()
    return sorted([p.stem for p in PRESETS_DIR.glob("*.json")])


def save_preset(name: str, cfg: NetemConfig) -> Tuple[int, str]:
    ensure_presets_dir()
    safe_name = sanitize_preset_name(name)
    if not safe_name:
        return 1, "Неверное имя пресета."
    path = PRESETS_DIR / f"{safe_name}.json"
    with path.open("w", encoding="utf-8") as f:
        json.dump(asdict(cfg), f, ensure_ascii=False, indent=2)
    return 0, f"Сохранён пресет: {safe_name}"


def load_preset(name: str) -> NetemConfig:
    ensure_presets_dir()
    safe_name = sanitize_preset_name(name)
    path = PRESETS_DIR / f"{safe_name}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    merged = {**asdict(DEFAULTS), **data}
    return NetemConfig(**merged)


def build_netem_clause(cfg: NetemConfig) -> str:
    pieces: List[str] = []
    if cfg.delay_enabled:
        delay = cfg.delay_ms or DEFAULTS.delay_ms
        if cfg.jitter_enabled:
            jitter = cfg.jitter_ms or DEFAULTS.jitter_ms
            pieces.append(f"delay {delay}ms {jitter}ms distribution normal")
        else:
            pieces.append(f"delay {delay}ms")
    elif cfg.jitter_enabled:
        jitter = cfg.jitter_ms or DEFAULTS.jitter_ms
        pieces.append(f"delay 0ms {jitter}ms distribution normal")

    if cfg.loss_enabled:
        loss = cfg.loss_pct or DEFAULTS.loss_pct
        pieces.append(f"loss {loss}%")
    if cfg.duplicate_enabled:
        duplicate = cfg.duplicate_pct or DEFAULTS.duplicate_pct
        pieces.append(f"duplicate {duplicate}%")
    if cfg.corrupt_enabled:
        corrupt = cfg.corrupt_pct or DEFAULTS.corrupt_pct
        pieces.append(f"corrupt {corrupt}%")
    if not pieces:
        pieces.append("delay 0ms")
    return " ".join(pieces)


def build_command(cfg: NetemConfig) -> str:
    uplink = quote(cfg.uplink)
    downlink = quote(cfg.downlink)
    netem_clause = build_netem_clause(cfg)
    tc_section: List[str] = [
        "# Reset old tc rules",
        f"sudo tc qdisc del dev {downlink} root 2>/dev/null",
        "# Shape bandwidth and add netem",
    ]
    if cfg.rate_enabled:
        rate = cfg.rate_kbit or DEFAULTS.rate_kbit
        tc_section.extend(
            [
                f"sudo tc qdisc add dev {downlink} root handle 1: htb default 10",
                f"sudo tc class add dev {downlink} parent 1: classid 1:10 htb rate {rate}kbit ceil {rate}kbit",
                f"sudo tc qdisc add dev {downlink} parent 1:10 handle 10: netem {netem_clause}",
            ]
        )
    else:
        tc_section.append(f"sudo tc qdisc add dev {downlink} root netem {netem_clause}")

    steps = [
        "# Enable IPv4 forwarding",
        "sudo sysctl -w net.ipv4.ip_forward=1",
        "# NAT traffic from test network to uplink",
        f"sudo iptables -t nat -F POSTROUTING",
        f"sudo iptables -F FORWARD",
        f"sudo iptables -t nat -A POSTROUTING -o {uplink} -j MASQUERADE",
        f"sudo iptables -A FORWARD -i {downlink} -o {uplink} -m state --state RELATED,ESTABLISHED -j ACCEPT",
        f"sudo iptables -A FORWARD -i {uplink} -o {downlink} -m state --state NEW -j ACCEPT",
    ]
    steps.extend(tc_section)
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
    preset_name = request.form.get("preset_name", "")
    selected_preset = request.form.get("preset_select", "")
    presets = list_presets()
    last_output = ""
    last_status = 0

    if request.method == "POST":
        if action == "apply":
            script = build_command(cfg)
            last_status, last_output = run_script(script)
        elif action == "generate":
            # Only refresh preview_script; no execution needed.
            pass
        elif action == "save_preset":
            last_status, last_output = save_preset(preset_name, cfg)
            presets = list_presets()
        elif action == "load_preset":
            try:
                cfg = load_preset(selected_preset)
                last_output = f"Загружен пресет: {selected_preset}"
                last_status = 0
            except FileNotFoundError:
                last_output = "Пресет не найден."
                last_status = 1
            except Exception as exc:
                last_output = f"Ошибка загрузки пресета: {exc}"
                last_status = 1
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
                delay_enabled=False,
                jitter_enabled=False,
                loss_enabled=False,
                duplicate_enabled=False,
                corrupt_enabled=False,
                rate_enabled=False,
            )
            preset_name = ""
            selected_preset = ""

    preview_script = build_command(cfg)

    return render_template_string(
        TEMPLATE,
        cfg=cfg,
        preview_script=preview_script,
        last_output=last_output,
        last_status=last_status,
        presets=presets,
        preset_name=preset_name,
        selected_preset=selected_preset,
    )


TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <title>Bad Network Lab (tc/netem)</title>
  <style>
    body { font-family: Arial, sans-serif; margin: 24px; max-width: 960px; }
    form { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 12px 18px; }
    .field { display: flex; flex-direction: column; gap: 6px; }
    label { font-weight: bold; }
    .checkbox-label { display: inline-flex; align-items: center; gap: 8px; }
    input[type="text"], input[type="number"] { width: 100%; padding: 6px 8px; }
    input[type="checkbox"] { width: auto; height: auto; margin: 0; }
    .wide { grid-column: 1 / -1; }
    textarea { width: 100%; height: 220px; }
    .buttons { display: flex; flex-wrap: wrap; gap: 12px; margin: 12px 0; }
    button { padding: 8px 14px; cursor: pointer; }
    .preset-row { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }
    .preset-row input[type="text"], .preset-row select { flex: 1 1 220px; padding: 6px 8px; }
    .output { background: #f7f7f7; border: 1px solid #ccc; padding: 12px; white-space: pre-wrap; }
    .status-ok { color: green; }
    .status-bad { color: #b30000; }
  </style>
</head>
<body>
  <h1>Bad Network Lab (tc/netem)</h1>
  <p>Заполните параметры, посмотрите итоговый скрипт и выполните его на Ubuntu.</p>
  <form method="post">
    <div class="field">
      <label for="uplink">uplink (eth0):</label>
      <input id="uplink" name="uplink" value="{{ cfg.uplink }}" required />
    </div>

    <div class="field">
      <label for="downlink">downlink (eth1):</label>
      <input id="downlink" name="downlink" value="{{ cfg.downlink }}" required />
    </div>

    <div class="field">
      <label for="delay_ms" class="checkbox-label">
        <input type="checkbox" name="delay_enabled" {% if cfg.delay_enabled %}checked{% endif %} />
        <span>Delay (ms):</span>
      </label>
      <input id="delay_ms" name="delay_ms" type="number" step="1" min="0" value="{{ cfg.delay_ms }}" />
    </div>

    <div class="field">
      <label for="jitter_ms" class="checkbox-label">
        <input type="checkbox" name="jitter_enabled" {% if cfg.jitter_enabled %}checked{% endif %} />
        <span>Jitter (ms):</span>
      </label>
      <input id="jitter_ms" name="jitter_ms" type="number" step="1" min="0" value="{{ cfg.jitter_ms }}" />
    </div>

    <div class="field">
      <label for="loss_pct" class="checkbox-label">
        <input type="checkbox" name="loss_enabled" {% if cfg.loss_enabled %}checked{% endif %} />
        <span>Loss (%):</span>
      </label>
      <input id="loss_pct" name="loss_pct" type="number" step="0.1" min="0" max="100" value="{{ cfg.loss_pct }}" />
    </div>

    <div class="field">
      <label for="duplicate_pct" class="checkbox-label">
        <input type="checkbox" name="duplicate_enabled" {% if cfg.duplicate_enabled %}checked{% endif %} />
        <span>Duplicate (%):</span>
      </label>
      <input id="duplicate_pct" name="duplicate_pct" type="number" step="0.1" min="0" max="100" value="{{ cfg.duplicate_pct }}" />
    </div>

    <div class="field">
      <label for="corrupt_pct" class="checkbox-label">
        <input type="checkbox" name="corrupt_enabled" {% if cfg.corrupt_enabled %}checked{% endif %} />
        <span>Corrupt (%):</span>
      </label>
      <input id="corrupt_pct" name="corrupt_pct" type="number" step="0.1" min="0" max="100" value="{{ cfg.corrupt_pct }}" />
    </div>

    <div class="field">
      <label for="rate_kbit" class="checkbox-label">
        <input type="checkbox" name="rate_enabled" {% if cfg.rate_enabled %}checked{% endif %} />
        <span>Rate (kbit):</span>
      </label>
      <input id="rate_kbit" name="rate_kbit" type="number" step="1" min="1" value="{{ cfg.rate_kbit }}" />
    </div>

    <div class="wide field">
      <label for="preview_script">Итоговый скрипт (tc + iptables):</label>
      <textarea id="preview_script" readonly>{{ preview_script }}</textarea>
    </div>

    <div class="wide field">
      <label>Пресеты</label>
      <div class="preset-row">
        <input type="text" name="preset_name" placeholder="Имя для сохранения" value="{{ preset_name }}" />
        <button type="submit" name="action" value="save_preset">Сохранить пресет</button>
      </div>
      <div class="preset-row">
        <select name="preset_select">
          <option value="">-- выберите пресет --</option>
          {% for p in presets %}
            <option value="{{ p }}" {% if p == selected_preset %}selected{% endif %}>{{ p }}</option>
          {% endfor %}
        </select>
        <button type="submit" name="action" value="load_preset">Загрузить пресет</button>
      </div>
    </div>

    <div class="buttons wide">
      <button type="submit" name="action" value="generate">Сгенерировать</button>
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
