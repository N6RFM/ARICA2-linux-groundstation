# Changelog

## Unreleased

- **Fixed:** `kiss_frame_dump.py` was documented in the README since 1.1.0
  but had been missed from the actual pushed repo. No functional change
  to the tool itself, just adding the file that should have been there
  all along.

## 1.2.0

- **Confirmed:** downlink addressing is a hybrid format, not the
  symmetric 7+7-byte AX.25 layout originally assumed, nor an unidentified
  opaque header as shown in 1.1.0. Destination is a compact 5 raw bytes
  with no SSID octet (confirmed as the requesting operator's own
  callsign); source is a standard 6-char + SSID field (confirmed as the
  satellite's own callsign, `JS1YSD`). This also explains why Direwolf
  flags every frame `(Not AX.25)` — the destination side breaks standard
  AX.25 address formatting even though the source side is fully
  standard.
- **Changed:** `decode_ax25_ui()` now returns proper `SRC>DST:info`
  formatted output (e.g. `JS1YSD>N6RFM:saved '' at box: 3`) instead of
  showing the header as opaque hex.
- **Renamed:** `arica2_message_v1.1.0.py` → `arica2_message_v1.2.0.py`.
- **Still unconfirmed:** whether the 5-byte destination field width holds
  for callsigns other than 5 characters.

## 1.1.0

- **Fixed:** downlink decoding was producing garbled output (e.g.
  `1YSDp>N6RFMJ:ved '' at box: 3`) because `decode_ax25_ui()` assumed
  standard 7-byte AX.25 addressing (6 chars + SSID) for both source and
  destination. Real captured frames don't match that structure.
- **Changed:** `decode_ax25_ui()` now anchors on the fixed Control
  (`0x03`) / PID (`0xF0`) bytes instead, and displays the preceding
  12-byte header as raw hex rather than mis-decoding it as a callsign.
  This is provisional — the header's actual meaning (fixed marker vs.
  sender-dependent) isn't confirmed yet; see the function's docstring.
- **Added:** `kiss_frame_dump.py` — a standalone tool that connects to
  Direwolf's KISS port and prints the complete raw hex of every frame,
  for capturing exact bytes when working out frame structure.
- **Added:** TXDELAY override checkbox in the message tool, unchecked by
  default, so `direwolf.conf`'s own static `TXDELAY` value is left alone
  unless explicitly overridden (sending any value, including 0, would
  otherwise force zero delay rather than leaving Direwolf's setting
  untouched).

## 1.0.0

- Initial native Linux port of JI1IZR's ARICA-2_Message.exe, reverse
  engineered from the disassembled IL. Uplink command frame construction
  (Download/Upload/Confirm/Parrot) and Direwolf/hs_soundmodem KISS TCP
  connectivity.
