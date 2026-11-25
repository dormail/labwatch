# labwatch

Monitors lab equipment and sends warnings when something goes wrong, for example
when a power outage stops a dilution fridge's pulse tube at 3 a.m.

- **Bluefors fridges** (Control Software v2.x HTTP API, tested with 2.6.1 and 2.7.0):
  temperatures, pressures, flow, pulse tube, compressor state, alarms and cooling water.
- **Any networked PC** (e.g. the Plassys evaporator's control computer): ping or TCP check.
- **Notifications** via Discord, Telegram, ntfy (phone push), e-mail and the log.
- **Trustworthy alerts:**
  - Rules **auto-arm**, so a warm fridge doesn't spam you.
  - Alerts are debounced and resolve with hysteresis.
  - Reminders repeat while a problem persists.
  - You can mute alerts during planned work.
  - A **dead man's switch** catches the case where labwatch itself loses power.

## Quick start

```bash
git clone https://github.com/dormail/labwatch && cd labwatch
uv sync                                  # or: python -m venv .venv && .venv/bin/pip install -e .
cp config.example.yaml config.yaml       # git-ignored
cp .env.example .env                     # git-ignored: API keys, tokens, webhook URLs
$EDITOR .env config.yaml

uv run labwatch check                    # poll once: readings + what each rule thinks
uv run labwatch test-notify              # send a test message to every notifier
uv run labwatch test-notify telegram -s critical   # test the loud path
uv run labwatch run                      # monitor (foreground)
```

Other commands:

| command | what it does |
|---|---|
| `labwatch discover Flash [prefix] [-g text]` | list every value the fridge exposes, e.g. `labwatch discover Flash driver.cpa` |
| `labwatch status` | latest readings, rule states, mutes and recent events (from the running monitor) |
| `labwatch mute Flash --for 6h -r "warm-up"` | silence a device (or a rule: `"Flash/T4K above 5 K"`, or everything: `"*"`) |
| `labwatch unmute Flash` | remove the mute |
| `labwatch backup` | bundle config, `.env` and history into one archive (to move machines) |

## How alerts work

Each device has rules on its channels (see `config.example.yaml`):

```yaml
- channel: pulse_tube
  not_equals: 1          # 0 = off, 1 = on, 2 = error
  severity: critical
  message: Pulse tube is not running! Check power, compressor, cooling water.
- channel: T4K
  above: 5               # K
  hysteresis: 0.3
  severity: critical
- channel: T4K
  rate_above: 0.05       # K/min over the window: early warning of a warm-up
  window: 10m
```

Condition types:

| keys | meaning |
|---|---|
| `above` / `below` | threshold (optional `hysteresis`) |
| `rate_above` / `rate_below` | rate of change per minute over `window` |
| `equals` / `not_equals` | enumeration state (lists allowed) |
| `max_age` | data not updated (e.g. the temperature controller stopped) |

Every device also gets a built-in **unreachable** rule (critical) that fires after
`unreachable_after` without a successful poll. That covers the case where a power
outage takes down the fridge's control PC.

Life of an alert:

- **auto-arm** (default on): a rule only becomes active after its condition has been
  healthy for `arm_after` (default 10 min). While Flash is warm, the "T4K above 5 K"
  rule is *disarmed*. Once the fridge is cold it arms itself, and you get an
  "alarms armed" info message.
- **`for`**: the condition must hold this long before alerting. This prevents alerts
  from single glitches.
- **`clear_for`**: the condition must be healthy this long before the alert resolves.
  This prevents flapping.
- **`repeat_interval`**: a reminder is sent while the alert is still firing.
- **Planned warm-ups and maintenance:** `labwatch mute Flash --for 2d`. Muted alerts are
  still evaluated and logged. If an alert is still firing when the mute ends, it is sent then.
- **Routing:** each notifier has a `min_severity` (info < warning < critical). For example:
  - Discord gets everything, including the daily summary.
  - Telegram gets warnings, silently, and critical alerts with sound.
  - ntfy and e-mail get only critical alerts.

The state (rule states, mutes, events, reading history) lives in `labwatch.sqlite`. It
survives restarts and will be the data source for a future dashboard.

## Devices

### Bluefors (Control Software v2.x)

1. In the Bluefors control software, create an **API key with read-only rights**. labwatch
   never writes. A key with write access could operate valves and heaters, so don't
   hand one out.
2. Put the PC's address and the key in `.env` (`FLASH_HOST`, `FLASH_API_KEY`).
3. `labwatch check Flash` should list temperatures, pressures, pulse tube etc.

The API is HTTPS on port 49098 with a self-signed certificate. `verify_tls: false`
works, but it is better to pin the certificate:

```bash
mkdir -p certs
openssl s_client -connect $FLASH_HOST:49098 </dev/null 2>/dev/null | openssl x509 > certs/flash.pem
# config.yaml:   verify_tls: certs/flash.pem
```

Built-in channels: `T50K T4K Tstill TMXC`(mK) `P1..P6 flow pulse_tube compressor cpa_alarm
cpa_alarm_text cpa_warning cpa_warning_text cooling_water_in cooling_water_out cpa_oil
cpa_helium cpa_high_pressure cpa_low_pressure status_text`.

You can add any other value: find its path with `labwatch discover`, then add it under
`channels:`. For more fridges, copy the device block. The rules are shared via a YAML
anchor (`rules: *bluefors_rules`).

### Ping / TCP check (e.g. Plassys MEB 550 S)

```yaml
- name: Plassys
  type: ping
  host: ${PLASSYS_HOST}
  method: tcp        # Windows blocks ICMP ping by default; use an open TCP port instead
  port: 80
  rules:
    - {channel: reachable, name: PC not responding, not_equals: 1, for: 3m, severity: warning, auto_arm: false}
    - {channel: reachable, name: PC down for 15 min, not_equals: 1, for: 15m, severity: critical, auto_arm: false}
```

Reading the Plassys control software itself (load lock state, pump-down times) is on the roadmap,
see [docs/plassys.md](docs/plassys.md).

### Simulator

`type: simulated` with `scenario: power_outage` produces a fake fridge whose pulse tube
"fails" after `outage_after`. Use it to try the whole chain, all the way to your phone,
without touching real hardware.

## Notification channels

### Telegram (recommended for your phone)

You only need a normal Telegram account. There is no developer registration and it's free.
1. In Telegram, message **@BotFather**, send `/newbot`, and choose a name and a username
   ending in `bot`. You get a token like `123456789:AA...`. Put it in `.env` as
   `TELEGRAM_BOT_TOKEN` and keep it secret (`/token` in BotFather replaces it).
2. Open `t.me/<your_bot>` and press **Start**. A bot cannot message you first.
3. Open `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser and copy
   `result[].message.chat.id` to `TELEGRAM_CHAT_ID`.
   - For a group: add the bot, send `/start@<your_bot>`, then read the (negative) id the
     same way. Supergroup ids start with `-100`.
4. `labwatch test-notify telegram` and `labwatch test-notify telegram -s critical`.

Sounds work like this:
- Warnings are sent *silently* (`disable_notification`) and critical alerts ring
  (`silent_below: critical`).
- The bot can't choose a sound, but Telegram lets you set a **custom notification
  sound per chat**: tap a short audio file in a chat, then pick it for that chat under
  notification settings.
- Optionally set `critical_chat_id` to a separate chat that has a loud sound; critical
  alerts then go only there.

Do-Not-Disturb: on Android, allow Telegram to override DND (Settings → Apps → Telegram →
Notifications). On iOS, allow Telegram in your Focus modes. Telegram doesn't support iOS
"Critical Alerts", so a phone on silent may stay silent. Keep a second channel (e.g. ntfy)
for the alerts that must wake you.

**Several people:** colleagues open the bot and press Start. You, the admin (`chat_id`), get
`Access request from Ana … /approve 123456`. After approval everyone gets alerts in their
own chat, with their own settings:

| command | answer |
|---|---|
| `/status`, `/flash`, `/ket`, `/plassys` | overview or details (temperatures, pulse tube, cooling water, pumps, FSE) |
| `/alerts` | active alerts |
| `/mute Flash 4h`, `/mute anomaly`, `/mute Ket circulation 1d`, `/mute all 8h` | mute **for yourself** (others still get them) |
| `/unmute …`, `/unmute all`, `/mutes` | undo / list |
| `/level critical\|warning\|info` | what you get (critical = only what rings) |
| `/muteall Flash 2d warm-up`, `/unmuteall Flash` | mute for **everyone**, announced to everyone |
| `/users`, `/approve <id>`, `/remove <id>` | admins only |

Alarm types for muting:
- `pulse_tube`, `temperature`, `circulation`, `cooling_water`, `pressure`, `fse`
- `anomaly` (unusual values), `connection` (unreachable)
- `labwatch` (started/stopped messages)

Strangers get no lab data until approved. Turn the commands off with `commands: false`.

### Discord (one bot, a channel per machine)

Each machine's alerts go to its own channel (#flash, #ket, #plassys).
**Everyone in a channel** can use slash commands. A mute applies to that channel,
so to the whole group, and the bot says publicly who muted what.

| command | what it does |
|---|---|
| `/status [device]` | overview, or one machine (only you see the answer) |
| `/alerts` | active alerts |
| `/mute [what] [duration]` | mute this channel: `all` (default), `anomaly`, `pulse_tube`, `circulation`, … for `30m`/`4h`/`2d`/`forever` (default 2 h) |
| `/unmute [what]` | unmute (default: everything muted here) |
| `/mutes` | what's muted here and for everyone |
| `/level critical\|warning\|info` | what this channel gets |
| `/muteall what duration [reason]` | mute for everyone (all channels and Telegram), announced everywhere; max 14 days |

**Setup** (once; you need *Manage Server* on your group's Discord server):
1. Open <https://discord.com/developers/applications> → **New Application**, name it e.g. `labwatch`.
2. **Bot** tab → **Reset Token** → copy it into `.env` as `DISCORD_BOT_TOKEN`. It is a password.
   No privileged intents are needed. You can switch off *Public Bot* so only you can add it.
3. **OAuth2 → URL Generator**:
   - Scopes: `bot` and `applications.commands`.
   - Bot permissions: *View Channels*, *Send Messages*, *Embed Links*, plus *Mention Everyone* if you use `mention: "@here"`.
   - Open the generated URL and add the bot to your server.
4. In Discord, turn on User Settings → Advanced → **Developer Mode**. Right-click each machine
   channel → **Copy Channel ID** → `.env` (`DISCORD_FLASH_CHANNEL`, `DISCORD_KET_CHANNEL`, …).
   For private channels, give the bot access in the channel's permissions.
5. Restart labwatch. The slash commands appear within seconds, and `labwatch test-notify discord`
   posts a test into the channels.

For a single channel without commands, a plain webhook still works (`type: discord`, `webhook_url`).

### ntfy (phone push, no account needed)

Install the ntfy app (Android or iOS) and subscribe to a long random topic name. That name
works as a password on the public server. Put it in `.env` as `NTFY_TOPIC`. Critical alerts
are sent with *urgent* priority, which can bypass Do-Not-Disturb on Android.

### E-mail

Standard SMTP (`starttls`, `ssl` or `none`). Critical alerts are flagged high priority.

## Where to run it, and the dead man's switch

A power outage can take down the network and the monitoring machine together with the fridge.
- Run labwatch on an **always-on machine on a UPS**, or a VM in the institute's server room,
  rather than a laptop or the fridge PC.
- Configure `heartbeat.url`. labwatch pings it every minute while healthy. A free
  [healthchecks.io](https://healthchecks.io) check then e-mails or pushes you if the pings stop
  (labwatch crashed, machine off, network down).
- `daily_summary: "09:00"` sends a short daily "all OK" message. If it doesn't arrive,
  something is wrong.

**Linux server (recommended):** `deploy/install.sh` sets up a sandboxed systemd service.

```bash
sudo git clone https://github.com/dormail/labwatch /opt/labwatch
sudo /opt/labwatch/deploy/install.sh [labwatch-backup-<date>.tar.gz]
```

| path | contents |
|---|---|
| `/opt/labwatch` | code + virtualenv (root-owned) |
| `/var/lib/labwatch` | `config.yaml`, `.env`, `labwatch.sqlite`; owned by the `labwatch` user, mode 700 |
| `/usr/local/bin/labwatch` | CLI wrapper, so `labwatch status` and `labwatch mute Flash --for 2h` work for admins |

- It needs Python ≥ 3.10; on Debian/Ubuntu also `python3-venv`.
- The service starts on boot and restarts itself if it crashes. Logs: `journalctl -u labwatch -f`.
- After editing the config: `sudo systemctl restart labwatch`.
- Update: `cd /opt/labwatch && sudo git pull && sudo deploy/install.sh`.

On Windows, run `labwatch run` as a service with [NSSM](https://nssm.cc) or Task Scheduler
("At startup", restart on failure).

## Moving labwatch to another machine

Everything site-specific lives in three files next to `config.yaml`:

| file | contents | without it |
|---|---|---|
| `config.yaml` | devices, rules, notifiers | start over from `config.example.yaml` |
| `.env` | API keys, bot token, webhook URLs | recreate the keys |
| `labwatch.sqlite` | reading history, armed/firing state, mutes, event log | rules re-arm after 10 min; anomaly detection re-learns (~12 h) |

```bash
# old machine (safe while running; consistent copy of the database)
labwatch backup                                  # -> labwatch-backup-<date>.tar.gz (secrets inside!)
scp labwatch-backup-*.tar.gz server:

# Linux server: installs a systemd service, restores the backup, checks the devices, starts it
sudo git clone https://github.com/dormail/labwatch /opt/labwatch
sudo /opt/labwatch/deploy/install.sh ~/labwatch-backup-*.tar.gz
# then stop the old instance:  kill $(cat labwatch.pid)   (or: sudo systemctl stop labwatch)
```

Run **only one instance per Telegram bot**. Two instances would send every alert
twice and compete for the bot's commands.

## Security

This repository is public. **Code goes here; site data doesn't:**
- `.env` (keys, tokens, webhook URLs), `config.yaml` (hosts, thresholds), `*.sqlite` and
  `*key*.txt` are git-ignored.
- Real IPs and secrets live only in your local `.env`.
- Optional pre-commit guard that blocks commits containing secrets or values from `.env`:
  `ln -sf ../../scripts/check-secrets.sh .git/hooks/pre-commit`.
- The Bluefors API only accepts the key as a URL parameter and echoes it in error
  responses. labwatch strips it from errors, silences `httpx` request logging, and redacts
  every secret from its logs.
- Use a read-only Bluefors API key.

## Development

```bash
uv run pytest
```

Layout:

| path | contents |
|---|---|
| `src/labwatch/devices/` | drivers: `bluefors`, `ping`, `simulated` |
| `src/labwatch/rules.py` | conditions and the alert state machine |
| `src/labwatch/engine.py` | polling, dispatch, heartbeat |
| `src/labwatch/notifiers/` | notification channels |
| `src/labwatch/state.py` | SQLite state |
| `src/labwatch/cli.py` | CLI |

To add a device type, subclass `Device` and decorate it with `@register("name")`.
Notifiers work the same way.

## Roadmap

- Plassys MEB 550 S: read the DaLeK control software / PLC (load lock open, left vented).
- More fridges (same driver, new device blocks).
- Web dashboard on top of `labwatch.sqlite` (history plots, active alerts, mute buttons).
