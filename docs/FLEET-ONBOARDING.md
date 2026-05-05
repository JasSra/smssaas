# Adding a phone to the SmsSaaS fleet

Each phone in the pool is a real Android device with a real SIM. Tenants of inboxr-cloud get assigned to phones in their country (currently AU only). This doc is the step-by-step for bringing on a new device.

## Hardware shopping list

Per phone:

| Item | Spec | AUD cost |
|---|---|---|
| Phone | Pixel 6a, 7a, 8a, or Opel S55 (current fleet member) — must be Android 14+ | $250-400 (used 6a is fine) |
| SIM | Optus / Telstra / Vodafone prepaid, "unlimited SMS + calls AU" | $30/mo |
| USB-C cable | Quality cable, can carry 5V continuously without dropouts | $10 |
| USB hub (only if Proxmox host's USB ports are full) | Powered hub, USB 3.0 | $40 |

Total upfront per phone: ~$300. Recurring: ~$30/mo.

Network constraint: each phone can sustain ~3-5 SMS/sec without carrier rate-limiting. Plan for one phone per ~50 active tenants.

## One-time prep on the phone

1. Insert SIM, complete carrier activation. Note the phone number (ours: send `BAL` to the carrier shortcode to confirm).
2. Settings → About phone → tap "Build number" 7 times → enables Developer options.
3. Developer options → enable **USB debugging** and **Stay awake while charging**.
4. Settings → Battery → disable any "adaptive battery" / "battery optimization" for SmsSaaS (after install) so the foreground service doesn't get killed.
5. Plug into the host machine. On the phone, accept "Allow USB debugging from this computer? Always allow" — *important*: tick "always".

## Connect to the gateway VM

The phone plugs into a host that runs the SmsSaaS adb-bridge container.

### Option A: USB direct on docker-host or gateway VM

```bash
# On the host (e.g. gateway VM 10.10.0.1 or docker-host 10.10.0.30)
lsusb | grep -iE "google|pixel|opel|unisoc"  # confirm enumeration
sudo docker exec smssaas-adb-bridge-1 adb devices
# Should list: <serial>  device   (or "unauthorized" until you tap allow)
```

### Option B: USB on Proxmox host, passthrough to a VM

Find the device's vendor:product:

```bash
ssh core0 'lsusb' | grep -iE "google|pixel|opel|unisoc"
# e.g.: Bus 003 Device 015: ID 18d1:4ee8 Google Inc.

# Pass through to VM 100 (gateway VM)
ssh core0 'qm set 100 -usb1 host=18d1:4ee8 -hotplug usb'
# (Use -usb0, -usb1, ..., -usb4 for up to 5 phones per VM)
```

Then verify from inside the VM:

```bash
ssh -J root@192.168.4.21 dev@10.10.0.1 'sudo docker exec smssaas-adb-bridge-1 adb devices'
```

### Option C: Wireless ADB (Android 11+)

For phones not physically near a host. On the phone: Developer options → Wireless debugging → Pair device with code.

```bash
# On gateway VM, container will pick up wifi devices via env var
ssh -J root@192.168.4.21 dev@10.10.0.1 \
  "sudo docker exec smssaas-adb-bridge-1 adb pair 192.168.4.55:38127 123456"
ssh -J root@192.168.4.21 dev@10.10.0.1 \
  "sudo docker exec smssaas-adb-bridge-1 adb connect 192.168.4.55:42135"
```

Add the IP:port to `WIFI_DEVICES` env in `/opt/smssaas/docker-compose.yml` so the bridge auto-reconnects after restarts.

## Install the SmsSaaS APK

The APK is built once (see `/opt/smssaas/android-app/` build pipeline) and reused for every phone.

```bash
# Push the prebuilt APK to the phone (DEVICE = the serial from `adb devices`)
DEVICE=<serial>
ssh -J root@192.168.4.21 dev@10.10.0.1 bash << REMOTE
sudo docker exec smssaas-adb-bridge-1 adb -s $DEVICE install -t /tmp/smssaas.apk

# Grant runtime permissions
for perm in SEND_SMS RECEIVE_SMS READ_SMS READ_PHONE_STATE READ_CALL_LOG \
            ANSWER_PHONE_CALLS RECORD_AUDIO; do
  sudo docker exec smssaas-adb-bridge-1 adb -s $DEVICE shell pm grant \
    com.smssaas.app android.permission.\$perm
done

# (Optional, for voice) grant system permission for both-sides call audio capture
sudo docker exec smssaas-adb-bridge-1 adb -s $DEVICE shell pm grant \
  com.smssaas.app android.permission.CAPTURE_AUDIO_OUTPUT

# Set up the reverse tunnel so the APK can reach the SmsSaaS API at localhost:8300
sudo docker exec smssaas-adb-bridge-1 adb -s $DEVICE reverse tcp:8300 tcp:8300

# Launch with the right config
sudo docker exec smssaas-adb-bridge-1 adb -s $DEVICE shell am start \
  -n com.smssaas.app/.MainActivity \
  --es base_url 'http://localhost:8300' \
  --es device_id $DEVICE \
  --es device_secret 'smssaas-worker-secret'
REMOTE
```

The phone auto-registers with the SmsSaaS API within ~5s. Verify:

```bash
curl -s http://10.10.0.1:8300/admin/phones \
  -H "X-Admin-Key: $ADMIN_KEY" | jq '.phones[] | select(.device_id=="<serial>")'
```

You should see `"status": "online"` and the phone's number.

## Known phone-number-detection problem on Android 14

The SmsSaaS worker tries to read the SIM's MSISDN via `service call iphonesubinfo`, which Android 14 returns empty for. Two workarounds:

1. **Manual update**: after the phone registers with `phone_number = 'unknown'`, set it via:
   ```bash
   sudo docker exec smssaas-api-1 python3 -c "
   import sqlite3
   c = sqlite3.connect('/data/smssaas.db')
   c.execute(\"UPDATE phones SET phone_number=? WHERE device_id=?\", ('+61413253383', '<serial>'))
   c.commit()"
   ```
   The overwrite-with-`unknown` protection (already in admin_router.py) keeps it stable across worker restarts.

2. **Better**: have the user message a known shortcode (e.g. `BAL` to Optus) — the carrier's reply lands in the inbox with the SIM's number visible in the body. Parse that on first boot.

## Configuring outbound SMS sender ID

By default, recipients see the SIM's phone number. For alphanumeric sender IDs (e.g. "Inboxr"):

- **Option 1**: Add a Twilio / MessageMedia / ClickSend account, route outbound through them when the tenant flag `outbound_via=aggregator` is set. Inbound stays on the real SIM. ~$0.04/SMS in AU.
- **Option 2**: Apply for a dedicated alpha sender ID with each AU carrier (Optus, Telstra, Vodafone). ~$200/mo each, requires ABN + use case approval. 6-8 week setup.

## Maintenance

- **Reboot weekly**: phones running 24/7 occasionally lose carrier registration. Cron a soft reboot:
  ```bash
  sudo docker exec smssaas-adb-bridge-1 adb -s <serial> shell reboot
  # APK auto-restarts on boot via BootReceiver.
  ```
- **Battery health**: keep phones plugged in 24/7. They'll show 100% but actual capacity degrades after ~2 years. Plan replacements at year 2.5.
- **SIM topups**: Optus prepaid auto-recharges if you set up AutoRecharge. Otherwise check balance monthly.
- **Carrier outages**: if `signal_dbm` shows null for >10 min on a phone, check that carrier's status page. Move that phone's tenants to other phones temporarily.

## Capacity planning

- Each phone can do ~3-5 SMS/sec sustained, ~50 SMS/min before the carrier may rate-limit.
- One Pixel can hold 4G/5G data + up to 2 SIMs (eSIM + physical) — useful for redundancy.
- USB hub limits: a single Proxmox host can passthrough ~10-15 USB devices before USB host controller saturation.

## When a phone dies

1. Mark it offline in the DB:
   ```bash
   sudo docker exec smssaas-api-1 python3 -c "
   import sqlite3
   c = sqlite3.connect('/data/smssaas.db')
   c.execute(\"UPDATE phones SET status='offline' WHERE device_id=?\", ('<serial>',))
   c.commit()"
   ```
2. Migrate any inboxes assigned to it. There's a TODO to auto-reassign on offline >5min — until then it's a manual SQL update of `sms_inboxes.device_id`.
3. Replace the SIM into a new phone. Reset device_id to the new serial. The phone number stays the same → tenants don't notice.
