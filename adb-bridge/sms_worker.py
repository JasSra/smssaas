#!/usr/bin/env python3
"""
ADB-direct SMS worker — NO custom Android APK required.

Uses ADB content providers and shell commands to:
  • Read inbound SMS from content://sms/inbox → POST /worker/inbound
  • Poll /worker/poll for outbound jobs → adb shell cmd sms send
  • Send /worker/heartbeat every 60s
  • Send /worker/diagnostic (signal, battery)

Run with: python3 sms_worker.py <device_serial> [--api http://192.168.4.25:8300]
Designed to run from any machine that has ADB connected to the phone.
"""
import subprocess, sys, os, time, json, re, argparse, logging
from datetime import datetime, timezone
from typing import Optional
import urllib.request, urllib.error

logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s %(message)s', datefmt='%H:%M:%S')
log = logging.getLogger('sms-worker')

# ── Config ────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(description='ADB-direct SMS worker')
    p.add_argument('device', help='ADB device serial (from adb devices)')
    p.add_argument('--api', default=os.environ.get('API_BASE', 'http://192.168.4.25:8300'),
                   help='SmsSaaS API base URL')
    p.add_argument('--key', default=os.environ.get('ADMIN_KEY', ''),
                   help='SmsSaaS admin key (for registration); worker endpoints need device secret')
    p.add_argument('--device-secret', default=os.environ.get('DEVICE_SECRET', 'smssaas-worker-secret'),
                   help='X-Device-Secret header for worker endpoints')
    p.add_argument('--poll-sms', type=int, default=3,    help='Inbound SMS poll interval (seconds)')
    p.add_argument('--poll-outbound', type=int, default=3, help='Outbound poll interval (seconds)')
    p.add_argument('--heartbeat', type=int, default=30,  help='Heartbeat interval (seconds)')
    return p.parse_args()

# ── ADB helpers ───────────────────────────────────────────────────────────────

def adb(serial: str, *args, timeout=10) -> str:
    cmd = ['adb', '-s', serial, *args]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip()
    except subprocess.TimeoutExpired:
        return ''
    except FileNotFoundError:
        log.error('adb not found — install Android platform tools')
        sys.exit(1)

def adb_shell(serial: str, cmd: str, timeout=10) -> str:
    return adb(serial, 'shell', cmd, timeout=timeout)

def device_online(serial: str) -> bool:
    """Check if device is still connected and authorized."""
    out = subprocess.run(['adb', 'devices'], capture_output=True, text=True).stdout
    for line in out.splitlines():
        if line.startswith(serial) and 'device' in line:
            return True
    return False

# ── Phone info ────────────────────────────────────────────────────────────────

def get_phone_number(serial: str) -> str:
    """Best-effort phone number extraction."""
    # Method 1: iphonesubinfo service call (MSISDN, slot 0)
    for slot in [15, 16, 1]:
        out = adb_shell(serial, f'service call iphonesubinfo {slot}')
        nums = re.findall(r"'([^']+)'", out)
        candidate = ''.join(n.replace('.', '') for n in nums).strip()
        if candidate and candidate not in ('', '0'):
            # Normalize to E.164 if it looks like a number
            digits = re.sub(r'\D', '', candidate)
            if len(digits) >= 8:
                if not digits.startswith('+'):
                    if digits.startswith('61'):
                        return f'+{digits}'
                    elif digits.startswith('0'):
                        return f'+61{digits[1:]}'
                return f'+{digits}'
    # Method 2: telephony manager
    for prop in ['ril.msisdn1', 'ril.msisdn0', 'ril.phone.number']:
        n = adb_shell(serial, f'getprop {prop}').strip()
        if n and len(n) > 4:
            digits = re.sub(r'\D', '', n)
            return f'+{digits}' if digits else 'unknown'
    return 'unknown'

def get_diagnostics(serial: str) -> dict:
    """Collect signal, battery, carrier info."""
    carrier     = adb_shell(serial, 'getprop gsm.sim.operator.alpha').strip() or ''
    country_raw = adb_shell(serial, 'getprop gsm.sim.operator.iso-country').strip().upper() or 'AU'
    model       = adb_shell(serial, 'getprop ro.product.model').strip() or 'Android'

    # Battery: parse dumpsys battery
    bat_out     = adb_shell(serial, 'dumpsys battery', timeout=5)
    bat_pct     = None
    m = re.search(r'level:\s*(\d+)', bat_out)
    if m: bat_pct = int(m.group(1))

    # Signal: parse dumpsys telephony.registry for signal strength
    sig_out  = adb_shell(serial, r'dumpsys telephony.registry 2>/dev/null | grep -i "signal\|rssi\|dbm" | head -5', timeout=5)
    sig_dbm  = None
    m = re.search(r'-?(\d{2,3})\s*dBm', sig_out, re.IGNORECASE)
    if m: sig_dbm = -int(m.group(1))

    return {
        'carrier':      carrier,
        'country_code': country_raw,
        'model':        model,
        'battery_pct':  bat_pct,
        'signal_dbm':   sig_dbm,
        'network_type': 'LTE',  # simplified; could parse from dumpsys
        'data_state':   'connected',
    }

# ── HTTP helpers ──────────────────────────────────────────────────────────────

def post(url: str, data: dict, headers: dict = {}, timeout=5) -> Optional[dict]:
    body = json.dumps(data).encode()
    h = {'Content-Type': 'application/json', **headers}
    req = urllib.request.Request(url, data=body, headers=h, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        log.warning(f'POST {url} → HTTP {e.code}: {e.read().decode()[:200]}')
        return None
    except Exception as e:
        log.warning(f'POST {url} → {e}')
        return None

def get_json(url: str, headers: dict = {}, timeout=5) -> Optional[dict]:
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception as e:
        log.warning(f'GET {url} → {e}')
        return None

# ── SMS content provider ──────────────────────────────────────────────────────

def parse_sms_rows(raw: str) -> list[dict]:
    """
    Parse `adb shell content query --uri content://sms` output.
    Each row looks like:
      Row: 0 _id=123, address=+61412345678, body=Hello, date=1234567890123, type=1, read=0
    """
    rows = []
    for line in raw.splitlines():
        m = re.match(r'Row:\s*\d+\s+(.*)', line)
        if not m: continue
        kv_str = m.group(1)
        row: dict = {}
        # parse key=value pairs (values may contain commas in body text)
        # Use a regex that handles the body containing commas
        pairs = re.findall(r'(\w+)=([^,=]*)(?=,\s*\w+=|$)', kv_str)
        for k, v in pairs:
            row[k.strip()] = v.strip()
        # Body may be truncated by the simple regex — re-extract
        bm = re.search(r'\bbody=(.+?)(?:,\s*\w+=|$)', kv_str)
        if bm:
            row['body'] = bm.group(1).strip()
        if 'address' in row or '_id' in row:
            rows.append(row)
    return rows

def read_inbox_since(serial: str, since_id: int) -> list[dict]:
    """Read inbound SMS with _id > since_id (--limit not supported on all Android versions)."""
    where = f'"_id > {since_id}"'
    raw = adb_shell(serial,
        f"content query --uri content://sms/inbox "
        f"--projection _id:address:body:date:read "
        f"--where {where} "
        f'--sort "_id ASC"',
        timeout=10,
    )
    return parse_sms_rows(raw)

def get_max_sms_id(serial: str) -> int:
    """Get the current max _id in inbox to use as a watermark (no --limit flag needed)."""
    raw = adb_shell(serial,
        'content query --uri content://sms/inbox --projection _id --sort "_id DESC"',
        timeout=10,
    )
    rows = parse_sms_rows(raw)
    if rows:
        # Sort descending by _id, take the first (max)
        ids = [int(r['_id']) for r in rows if '_id' in r]
        return max(ids) if ids else 0
    return 0

# ── Outbound send ─────────────────────────────────────────────────────────────

def send_sms(serial: str, to: str, body: str) -> bool:
    """
    Send SMS via ADB using the isms service call.

    Transaction code varies by Android version + OEM:
      isms 7 — Pixel / AOSP Android 10-13 (sendTextForSubscriber with attribution tag)
      isms 6 — Android 14 / Unisoc / Optus AU (sendTextForSubscriber without attrib tag)
      isms 5 — Android 9 and earlier

    We try 6 first (works on the current Optus/Opel S55 fleet), fall back to 7.
    Empty parcel return ('00000000') = sent. 'fffffffc' = security exception (wrong code).
    """
    safe_body = body.replace('"', '\\"').replace("'", "\\'").replace('`', '\\`').replace('$', '\\$')
    safe_to   = to.replace('"', '')

    # isms 6: subId, callerPkg, destAddr, scAddr, text, sentIntent, deliveryIntent, persistMessage, callingUser
    cmd6 = (
        f'service call isms 6 '
        f'i32 1 '                          # subId (1 = first SIM)
        f's16 "com.android.shell" '        # callingPkg
        f's16 "{safe_to}" '               # destAddr (E.164)
        f's16 "null" '                    # scAddr (null = default SC)
        f's16 "{safe_body}" '             # text
        f'i32 0 '                          # sentIntent (null)
        f'i32 0 '                          # deliveryIntent (null)
        f'i32 1 '                          # persistMessage
        f'i32 -2'                          # callingUser (-2 = CURRENT_USER)
    )
    result = adb_shell(serial, cmd6, timeout=20)
    log.debug(f'isms 6 send → {result[:80]!r}')
    # Empty parcel = success; "fffffffc" = SecurityException (wrong txn code)
    if 'fffffffc' not in result and ('00000000' in result or result.strip() == ''):
        return True

    # Fall back to isms 7 (older Android versions)
    cmd7 = (
        f'service call isms 7 '
        f'i32 1 s16 "com.android.shell" s16 "null" '
        f's16 "{safe_to}" s16 "null" s16 "{safe_body}" '
        f'i32 0 i32 0 i32 1 i32 -2'
    )
    result = adb_shell(serial, cmd7, timeout=20)
    log.debug(f'isms 7 send → {result[:80]!r}')
    if 'fffffffc' not in result and ('not fully consumed' in result or result.strip() == ''):
        return True

    if 'ffffffe8' in result:
        log.error(f'SMS send failed: SIM not ready for {to}')
        return False
    log.warning(f'SMS send failed for {to}: result={result[:120]!r}')
    return False

# ── Main worker loops ─────────────────────────────────────────────────────────

def run(serial: str, api: str, device_secret: str, admin_key: str, cfg: argparse.Namespace):
    log.info(f'=== ADB-direct SMS worker | device={serial} | api={api} ===')
    headers_worker = {'X-Device-Secret': device_secret}
    headers_admin  = {'X-Admin-Key': admin_key}

    # ── Register phone ──────────────────────────────────────────────────────
    log.info('Reading phone info...')
    diag = get_diagnostics(serial)
    phone_number = get_phone_number(serial)
    log.info(f'Phone: {phone_number} | carrier={diag["carrier"]} | country={diag["country_code"]} | battery={diag["battery_pct"]}%')

    reg = post(f'{api}/admin/phones/register',
               {'device_id': serial, 'phone_number': phone_number,
                'country_code': diag['country_code'], 'carrier': diag['carrier'],
                'model': diag['model']},
               headers=headers_admin)
    if reg:
        log.info(f'Registered: {reg}')
    else:
        log.warning('Registration failed — will retry. Worker continuing anyway.')

    # ── Watermark: start from current max SMS _id ──────────────────────────
    last_sms_id = get_max_sms_id(serial)
    log.info(f'SMS watermark set to _id={last_sms_id} (will forward new messages from here)')

    last_heartbeat  = 0.0
    last_inbound_t  = 0.0
    last_outbound_t = 0.0
    last_diag_t     = 0.0

    while True:
        now = time.time()

        if not device_online(serial):
            log.error(f'Device {serial} disconnected! Waiting to reconnect...')
            time.sleep(10)
            continue

        # ── Heartbeat ───────────────────────────────────────────────────────
        if now - last_heartbeat >= cfg.heartbeat:
            r = post(f'{api}/worker/heartbeat', {'device_id': serial}, headers=headers_worker)
            if r:
                log.debug('Heartbeat OK')
            last_heartbeat = now

        # ── Diagnostics (every 5 min) ───────────────────────────────────────
        if now - last_diag_t >= 300:
            d = get_diagnostics(serial)
            post(f'{api}/worker/diagnostic', {
                'device_id':   serial,
                'battery_pct': d['battery_pct'],
                'signal_dbm':  d['signal_dbm'],
                'carrier':     d['carrier'],
                'network_type': d['network_type'],
                'data_state':  d['data_state'],
            }, headers=headers_worker)
            last_diag_t = now

        # ── Inbound SMS check ───────────────────────────────────────────────
        if now - last_inbound_t >= cfg.poll_sms:
            new_msgs = read_inbox_since(serial, last_sms_id)
            for msg in new_msgs:
                sms_id  = int(msg.get('_id', 0))
                addr    = msg.get('address', 'unknown')
                body    = msg.get('body', '')
                date_ms = int(msg.get('date', '0') or 0)
                received_dt = datetime.fromtimestamp(date_ms / 1000, tz=timezone.utc).isoformat()

                log.info(f'[INBOUND] from={addr} body={body[:60]!r}')
                r = post(f'{api}/worker/inbound', {
                    'device_id':   serial,
                    'from_number': addr,
                    'body':        body,
                    'received_at': received_dt,
                    'media_json':  None,
                }, headers=headers_worker)
                if r:
                    log.info(f'  → forwarded to API (id={sms_id})')
                else:
                    log.warning(f'  → API forward failed for _id={sms_id}')
                last_sms_id = max(last_sms_id, sms_id)
            last_inbound_t = now

        # ── Outbound jobs ───────────────────────────────────────────────────
        if now - last_outbound_t >= cfg.poll_outbound:
            data = get_json(f'{api}/worker/poll?device_id={serial}&limit=5', headers=headers_worker)
            if data and data.get('jobs'):
                for job in data['jobs']:
                    job_id = job['id']
                    to     = job['to_number']
                    body   = job['body']
                    log.info(f'[OUTBOUND] job={job_id} to={to} body={body[:60]!r}')
                    ok = send_sms(serial, to, body)
                    result = 'SENT' if ok else 'FAILED'
                    post(f'{api}/worker/receipt', {
                        'message_id': job_id,
                        'device_id':  serial,
                        'result':     result,
                        'error_code': None,
                    }, headers=headers_worker)
                    log.info(f'  → {result}')
            last_outbound_t = now

        time.sleep(0.5)

# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == '__main__':
    args = parse_args()
    if not device_online(args.device):
        log.error(f'Device {args.device} not found in `adb devices`. Is it connected?')
        log.error('Run `adb devices` to see available devices.')
        sys.exit(1)
    try:
        run(args.device, args.api, args.device_secret, args.key, args)
    except KeyboardInterrupt:
        log.info('Worker stopped.')
