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

- **`arica2_message.py`** — a Tkinter GUI that builds and sends the same
  20-slot mailbox commands (Download / Upload / Confirm / Parrot) over a
  KISS TCP connection to [Direwolf](https://github.com/wb2osz/direwolf),
  and decodes received AX.25 UI frames for display. Includes a
  user-adjustable **key-up time** control (sent to Direwolf as a KISS
  TXDELAY command right before each message, so Direwolf — the thing that
  actually owns PTT and modem timing — is what enforces it).
- **`direwolf.conf.example`** — a template Direwolf config covering the
  audio device and PTT options that come up in practice (USB audio +
  FTDI RTS keyer, built-in sound card + CAT PTT, CM108 sound fob).

## Requirements

- Python 3 with Tkinter (ships with virtually every desktop Linux distro's
  Python — no pip installs needed for the GUI itself)
- [Direwolf](https://github.com/wb2osz/direwolf) as the software TNC/modem
- A radio capable of 4800 baud G3RUH FM on ARICA-2's downlink/uplink
  frequency, with a way to get flat (non-mic/speaker) audio in/out and a
  PTT line
- Optional but recommended: [Gpredict](http://gpredict.oz9aec.net/) for
  Doppler correction and antenna tracking, and `hamlib`'s `rigctld` for
  CAT control

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
python3 arica2_message.py
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

Downlink frames (telemetry, message content, ACKs) are standard AX.25 UI
frames with normally shifted (`<<1`) callsigns — `arica2_message.py`
decodes and displays them as `SRC>DST:message`.

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
