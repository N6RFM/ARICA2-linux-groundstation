# ARICA2-linux-groundstation

A native Linux toolkit for working [ARICA-2](https://db.satnogs.org/satellite/IFKU-9201-2588-0809-6295)'s
amateur "message box" mission — no Windows, no Wine.

The mission runs a scheduled window (announced on the
[ARICA-2 site](https://sites.google.com/phys.aoyama.ac.jp/sakamotolab/research/current_space/ARICA-2_en/amateur)
and JAMSAT-BB). The satellite holds 20 message slots, each storing up to
8 characters for 48 hours. Uplink is only possible in a ~15-second window
right after the satellite's CW beacon ends.

[JI1IZR's original tool](https://www.jamsat.or.jp/?page_id=2777) for building
these commands is a small Windows/.NET GUI app. This repo is a from-scratch
**native Linux reimplementation**, reverse-engineered by disassembling that
app's IL bytecode with `monodis` and reading its `Transmit_Button_Click` /
`ClientListen` logic directly — the frame layout below is a byte-for-byte
match, not a guess.

## What's here

- **`arica2_message_v1.2.0.py`** — a Tkinter GUI that builds and sends the same
  20-slot mailbox commands (Download / Upload / Confirm / Parrot) over a
  KISS TCP connection to [Direwolf](https://github.com/wb2osz/direwolf),
  and decodes received AX.25 UI frames for display. Includes an optional,
  unchecked-by-default **TXDELAY override**: when enabled, Direwolf
  asserts PTT, waits this long, and only then starts sending the actual
  frame — giving the radio time to key up cleanly and the receiver's
  squelch/AGC/PLL time to settle before real data arrives, instead of
  clipping the start of the transmission. This is sent to Direwolf as a
  KISS TXDELAY command right before each message (in 10ms units, e.g.
  30 = 300ms). If you always want the same TX delay, leave the
  override unchecked and just set `TXDELAY` once in `direwolf.conf`
  instead — the override exists for adjusting it live, per transmission,
  without restarting Direwolf. **Note:** there's no value in the
  spinbox that means "leave Direwolf's own setting alone" — sending
  the command at all (0 included) explicitly overrides it, so 0 would
  mean *zero* delay, not "unchanged."
- **`direwolf.conf.example`** — a template Direwolf config covering the
  audio device and PTT options that come up in practice (USB audio +
  FTDI RTS keyer, built-in sound card + CAT PTT, CM108 sound fob).
- **`kiss_frame_dump.py`** — a standalone diagnostic tool that connects
  to Direwolf's KISS port and prints the complete raw hex of every frame
  it sees, for capturing exact bytes when working out frame structure
  (see the note on downlink decoding below).

See [CHANGELOG.md](CHANGELOG.md) for version history.

## Requirements

```
sudo apt install python3 python3-tk direwolf grig libhamlib-utils pavucontrol
```

- **`python3-tk`** — Tkinter isn't bundled with the `python3` package on
  Debian/Ubuntu (unlike Windows/Mac); the GUI needs this explicitly or it
  fails with `ModuleNotFoundError: No module named 'tkinter'`.
- **`direwolf`** — the software TNC/modem that actually transmits/receives
  over the radio.
- **`grig`** and **`libhamlib-utils`** (provides `rigctld`) — CAT control;
  optional if you're not using CAT for PTT or Doppler.
- **`pavucontrol`** — for checking/fixing audio routing and capture source;
  optional but useful for diagnosing audio issues.

Also needed, not installed via `apt`:

- A radio capable of 4800 baud G3RUH FM on ARICA-2's downlink/uplink
  frequency, with a way to get flat (non-mic/speaker) audio in/out and a
  PTT line.
- Optional but recommended: [Gpredict](http://gpredict.oz9aec.net/) for
  Doppler correction and antenna tracking.

## Quick start

```bash
sudo apt install direwolf              # plus grig, libhamlib-utils, pavucontrol as needed

# find your audio device
arecord -l

cp direwolf.conf.example direwolf.conf
# edit direwolf.conf: set MYCALL, ADEVICE, and uncomment the PTT line
# that matches your hardware

direwolf -c direwolf.conf -t 0
```

In another terminal:

```bash
python3 arica2_message_v1.2.0.py
```

In the app: select **Direwolf**, confirm host/port are `127.0.0.1:8001`,
click **Connect**, fill in the command type/callsign/slot/message, and
**Transmit**.

## Frame format (reverse-engineered)

Every uplink command is a fixed 22-byte KISS frame. This is *not* standard
AX.25 addressing — it's ARICA-2's own compact command layout, carried over
Direwolf's normal AX.25 physical-layer framing (flags, bit-stuffing, FCS,
G3RUH scrambling), which is why the original software describes it as a
"special format" requiring dedicated tooling.

| Byte(s) | Meaning |
|---|---|
| 0 | `0xC0` — KISS FEND |
| 1 | `0x00` — KISS data frame, port 0 |
| 2–4 | `0x42 0xF8 0xBD` — fixed constant |
| 5–6 | Command type (see below) |
| 7–12 | Callsign, up to 6 raw ASCII chars, zero-padded |
| 13–20 | Message, up to 8 raw ASCII chars (Upload/Parrot only), zero-padded |
| 21 | `0xC0` — KISS FEND |

Command type encoding for bytes 5–6:

| Command | Byte 5 | Byte 6 |
|---|---|---|
| Download (slot `id`, 1–20) | `0x40 + (id // 2)` | `(id % 2) * 128` |
| Upload | `0x50` | `0x00` |
| Confirm | `0x60` | `0x00` |
| Parrot | `0x70` | `0x00` |

**Downlink frames use a hybrid AX.25 addressing format**, confirmed
against real captured frames (see CHANGELOG) — not the standard symmetric
7-byte-dest + 7-byte-src layout originally assumed:

```
raw[2:7]    destination: 5 raw bytes, standard AX.25 <<1 shift, NO SSID/
            extension octet - compact. Confirmed as the requesting
            operator's own callsign (e.g. N6RFM).
raw[7:13]   source: 6 bytes, standard AX.25 <<1 shift. Confirmed as the
            satellite's own callsign, JS1YSD.
raw[13]     source SSID octet - a properly formed standard AX.25 SSID
            byte (extension bit set, reserved bits 1,1); the destination
            side has no equivalent byte at all.
raw[14]     0x03  Control (fixed AX.25 UI-frame value)
raw[15]     0xF0  PID (fixed AX.25 "no layer 3" value)
raw[16:-1]  info text, plain ASCII - the actual content, e.g.
            "saved '' at box: 3" or a relayed "CALLSIGN:MESSAGE" entry
```

This is why Direwolf itself flags every one of these frames
`(Not AX.25)`: the destination field breaks the standard AX.25 address
format (no SSID/extension octet), even though the source field right
next to it is fully standard AX.25. `arica2_message_v1.2.0.py` decodes
this as `SRC>DST:info`, e.g. `JS1YSD>N6RFM:saved '' at box: 3`.

**Not yet confirmed:** whether the 5-byte destination field width holds
for callsigns other than 5 characters — it fit perfectly here with no
padding needed, but a shorter or longer callsign hasn't been tested.
`kiss_frame_dump.py` is included for capturing frames from other
callsign lengths to settle this.

**Only Download encodes a slot ID.** Upload, Confirm, and Parrot each use a
fixed byte 5 value with byte 6 left at `0x00` — none of them has any code
path that folds a slot number into the frame. This means there's no way to
target a specific slot on Upload; the satellite itself must be choosing
which of the 20 slots to store an Upload into, and any "message renewed at
[slot ID]"-style response is the satellite reporting that assignment back
to you after the fact, not something you can request going in. This is why
`arica2_message_v1.2.0.py` greys out the Message ID field for every command type
except Download — there's no slot field for it to populate.

## Known limitations

- No KISS byte-escaping (`0xDB`) handling on receive, matching the
  original app's behavior — only matters if a payload happens to contain
  a literal `0xC0`/`0xDB` byte, unlikely for these short ASCII messages.
- Callsign/message length limits (6/8 chars) are enforced in software here;
  the original app relied on textbox `MaxLength` at the UI layer, which
  isn't visible from the disassembled logic.

## Authors

- N6RFM
- Claude (Anthropic) — reverse engineering, Linux port, and documentation

## License

MIT — see [LICENSE](LICENSE). This is an independent reimplementation, not
affiliated with JI1IZR, JAMSAT, or the ARICA-2 project.
