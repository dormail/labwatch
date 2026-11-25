# Plassys MEB 550 S: monitoring notes

## Phase 1 (done): is the control PC alive?

The control PC runs Windows with Plassys' **DaLeK** software. Windows blocks inbound ICMP
ping by default ("File and Printer Sharing (Echo Request - ICMPv4-In)" is disabled). Our PC
has TCP port 80 open (`Server: Microsoft-IIS/10.0`), so labwatch checks it with a TCP connect:

```yaml
- name: Plassys
  type: ping
  host: ${PLASSYS_HOST}
  method: tcp
  port: 80
  rules:
    - {channel: reachable, name: PC not responding, not_equals: 1, for: 3m, severity: warning, auto_arm: false}
    - {channel: reachable, name: PC down for 15 min, not_equals: 1, for: 15m, severity: critical, auto_arm: false}
```

This proves the PC (and IIS) are up. It does **not** prove DaLeK is running.

## Phase 2 (todo): load lock open / left vented

What is public about DaLeK ([plassys.com/categories/Software](https://plassys.com/categories/Software)):
- Plassys' own software, runs on Windows Pro.
- "Jscript language for machine interface and user interface".
- "Compatible with … LabView, Matlab, .Net" and RS232/RS485/GPIB.
- Records analog/digital I/O "with no time limit", as data "compatible with Origin, Excel,
  Mathematica", which suggests text/CSV files.
- Screenshots show a main window "CSTop" with per-chamber state, e.g.
  "LL. Chamber State: Fine Pumping", and Bayard-Alpert pressure readouts.

No PLC, OPC UA, Modbus or database is documented publicly. TCP ports 102 (S7), 502 (Modbus)
and 4840 (OPC UA) are closed from the lab network.

On-site checklist, least invasive first:

1. **DaLeK data recording**
   - CSTop → *About* for the version.
   - In *File* / *Views*, look for data recording or trends and note where files are written
     (folder, format, how often).
   - If the load-lock state and pressures are logged, labwatch can read the newest file.
     That needs either a read-only SMB share (port 445 opened by IT for the monitoring host
     only) or a small scheduled task on the PC that pushes values out.
2. **What IIS serves**
   - `inetmgr` or PowerShell `Get-Website` (sites, bindings, physical paths).
   - `C:\inetpub\wwwroot`.
   - `C:\inetpub\logs\LogFiles`.
   - `netstat -abno | findstr :80`.
   - If DaLeK exposes an HTTP endpoint, that is the cleanest way to read it.
3. **Hardware**
   - Is there a PLC / remote I/O in the rack (Siemens, Beckhoff, Wago, RS-485 modules,
     a GPIB card)?
   - Does the gauge controller have a spare Ethernet port?
   - Read only, and never share a serial line DaLeK is using.
4. **Ask Plassys support**
   - What their "LabView/Matlab/.Net compatibility" means in practice: API, DLL or files?
   - Whether the IIS site belongs to DaLeK.
   - Whether there is a sanctioned read-only way to get load-lock state and pressures.

Once a data source exists, a `plassys` device type can map it to channels like `loadlock_state`
and `loadlock_pressure`. A rule such as "load lock vented for more than 2 h" is then just a
`for: 2h` on a state rule.
