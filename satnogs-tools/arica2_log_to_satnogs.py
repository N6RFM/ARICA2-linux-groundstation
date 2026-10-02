#!/usr/bin/env python3
"""
arica2_log_to_satnogs.py

Forward ARICA-2 downlink frames to SatNOGS DB (via the SiDS protocol),
reading from a decoder's text log file instead of a live Direwolf KISS
connection. Useful when you have a log like:

    [2026-09-30 17:34:32] JS1YSD>N6RFM:your message is renewed. 'de N6RFM'at 3
    [2026-09-30 17:02:58] [unrecognized frame structure] c0 00 60 a5 0f f1 ... c0

Two line formats are handled:

  1. "[TIMESTAMP] SRC>DST:payload" -- a cleanly decoded frame. Since the
     raw AX.25 bytes aren't available in this form, a synthetic
     (but structurally standard) AX.25 UI frame is reconstructed from
     the source, destination, and payload text for submission. This is
     NOT bit-identical to the original over-the-air frame, but should
     decode correctly through SatNOGS' standard AX.25/Kaitai decoders.

  2. "[TIMESTAMP] [unrecognized frame structure] <hex bytes>" -- a raw
     KISS-framed dump for a frame the decoder couldn't parse into text.
     The exact original AX.25 bytes are recovered from this (KISS
     framing and byte-stuffing removed) and submitted as-is -- no
     reconstruction needed, this is the real frame.

TIMEZONE HANDLING
-----------------
Log timestamps are assumed to be in the local system timezone (as
produced by the decoder) and are converted to UTC before submission,
since SatNOGS expects UTC timestamps. This relies on running this tool
on the same machine (or a machine in the same timezone, with the same
system clock settings) that produced the log.

FILTERING
---------
Only frames whose AX.25 source matches --satellite-callsign are
forwarded (default: JS1YSD, ARICA-2's callsign). This naturally
excludes your own uplink and any unrelated traffic.

USAGE
-----
  # Process a complete log file once (e.g. after a pass):
  python3 arica2_log_to_satnogs.py --log-file ARICA-2Log20260930_174019.log \
      --norad-id 98329 --source-callsign N6RFM

  # Keep watching a log file that's still being written (live operation):
  python3 arica2_log_to_satnogs.py --log-file ARICA-2Log20260930_174019.log \
      --norad-id 98329 --source-callsign N6RFM --follow

  # Dry run -- parse and print, don't submit anything:
  python3 arica2_log_to_satnogs.py --log-file ARICA-2Log20260930_174019.log \
      --norad-id 98329 --source-callsign N6RFM --dry-run -v
"""

import argparse
import datetime
import re
import sys
import time

try:
    import requests
except ImportError:
    requests = None

SIDS_URL = "https://db.satnogs.org/api/telemetry/"
SIDS_DEV_URL = "https://db-dev.satnogs.org/api/telemetry/"

LINE_RE_TEXT = re.compile(
    r"^\[(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]\s+"
    r"(?P<src>[A-Za-z0-9\-]+)>(?P<dst>[A-Za-z0-9\-]+):(?P<payload>.*)$"
)
LINE_RE_HEX = re.compile(
    r"^\[(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]\s+"
    r"\[unrecognized frame structure\]\s+(?P<hex>[0-9a-fA-F ]+)$"
)

FEND = 0xC0
FESC = 0xDB
TFEND = 0xDC
TFESC = 0xDD


def kiss_unescape(data: bytes) -> bytes:
    out = bytearray()
    i = 0
    while i < len(data):
        b = data[i]
        if b == FESC and i + 1 < len(data):
            nxt = data[i + 1]
            if nxt == TFEND:
                out.append(FEND)
                i += 2
                continue
            if nxt == TFESC:
                out.append(FESC)
                i += 2
                continue
        out.append(b)
        i += 1
    return bytes(out)


def extract_ax25_from_kiss_dump(hex_str: str):
    """Given a hex dump of a full KISS frame (including FEND delimiters),
    return the raw AX.25 bytes, or None if it's not a data frame."""
    raw = bytes.fromhex(hex_str.replace(" ", "").strip())
    if raw[:1] == bytes([FEND]):
        raw = raw[1:]
    if raw[-1:] == bytes([FEND]):
        raw = raw[:-1]
    raw = kiss_unescape(raw)
    if not raw:
        return None
    cmd, payload = raw[0], raw[1:]
    if (cmd & 0x0F) != 0x00:
        return None
    return payload


def safe_ascii(data: bytes) -> str:
    """Render bytes as text, replacing non-printable bytes with '.'."""
    return "".join(chr(b) if 32 <= b < 127 else "." for b in data)


def encode_arica2_addr(callsign: str, width: int, include_ssid: bool) -> bytes:
    """Encode a callsign as one of ARICA-2's hybrid address fields:
    `width` raw ASCII bytes (AX.25 <<1 shift, space-padded), optionally
    followed by a standard AX.25 SSID byte. See parse_arica2_frame for
    the full confirmed layout this matches.
    """
    call = callsign.upper()
    ssid = 0
    if "-" in call:
        call, ssid_str = call.split("-", 1)
        try:
            ssid = int(ssid_str)
        except ValueError:
            ssid = 0
    call = call.ljust(width)[:width]
    addr = bytes((ord(c) << 1) & 0xFF for c in call)
    if include_ssid:
        ssid_byte = 0x60 | ((ssid & 0x0F) << 1) | 0x01
        return addr + bytes([ssid_byte])
    return addr


def build_synthetic_ax25(src: str, dst: str, payload_text: str) -> bytes:
    """Reconstruct ARICA-2's actual hybrid frame layout (see the main
    repo README's "Frame format" section) from decoded text fields --
    NOT a generic standard-AX.25 UI frame. A 5-byte destination (no
    SSID byte), a 6-byte source plus a standard AX.25 SSID byte for the
    source only, then Control (0x03) and PID (0xF0), then the info
    text. Not bit-identical to the original over-the-air frame (no
    original FCS), but structurally matches what ARICA-2 actually
    transmits, unlike a generic 7+7 standard-AX.25 frame would.
    """
    dest_addr = encode_arica2_addr(dst, width=5, include_ssid=False)
    src_addr = encode_arica2_addr(src, width=6, include_ssid=True)
    control = bytes([0x03])  # UI frame
    pid = bytes([0xF0])  # no layer 3 protocol
    info = payload_text.encode("utf-8", errors="replace")
    return dest_addr + src_addr + control + pid + info


def parse_arica2_frame(ax25: bytes):
    """Parse ARICA-2's confirmed hybrid frame layout -- NOT standard
    AX.25 addressing (see build_synthetic_ax25 / the main repo README's
    "Frame format" section for the full layout). Returns
    (dest_call, src_call, info_text), or None if too short.
    """
    if len(ax25) < 14:
        return None
    dest_call = "".join(chr(b >> 1) for b in ax25[0:5]).strip(" \x00")
    src_call = "".join(chr(b >> 1) for b in ax25[5:11]).strip(" \x00")
    info_text = safe_ascii(ax25[14:])
    return dest_call, src_call, info_text


def parse_local_to_utc(ts_str: str, tzinfo: datetime.tzinfo) -> str:
    """Parse a naive 'YYYY-MM-DD HH:MM:SS' local timestamp (assumed to be
    in the given tzinfo) and return an ISO8601 UTC string suitable for
    SiDS submission."""
    naive = datetime.datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
    local_aware = naive.replace(tzinfo=tzinfo)
    utc = local_aware.astimezone(datetime.timezone.utc)
    return utc.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def resolve_tzinfo(utc_offset: float, tz_name: str) -> datetime.tzinfo:
    """Resolve the timezone to assume the log's naive timestamps are in,
    from the --tz-name / --utc-offset / system-default options (checked
    in that priority order).
    """
    if tz_name:
        try:
            from zoneinfo import ZoneInfo  # Python 3.9+
        except ImportError:
            print("--tz-name requires Python 3.9+ (zoneinfo module). "
                  "Use --utc-offset instead.", file=sys.stderr)
            sys.exit(1)
        try:
            return ZoneInfo(tz_name)
        except Exception as exc:  # noqa: BLE001
            print(f"[error] unknown timezone name '{tz_name}': {exc}", file=sys.stderr)
            sys.exit(1)
    if utc_offset is not None:
        return datetime.timezone(datetime.timedelta(hours=utc_offset))
    # Fall back to this machine's own local timezone setting.
    return datetime.datetime.now().astimezone().tzinfo


def build_sids_payload(norad_id: int, source_callsign: str, frame: bytes,
                        latitude: str, longitude: str, timestamp_utc: str) -> dict:
    return {
        "noradID": norad_id,
        "source": source_callsign,
        "locator": "longLat",
        "latitude": latitude,
        "longitude": longitude,
        "timestamp": timestamp_utc,
        "frame": frame.hex().upper(),
    }


def parse_log_line(line: str):
    """Parse one log line. Returns (timestamp_str, src_call, ax25_bytes,
    is_reconstructed, decoded_text) or None if the line doesn't match
    either format."""
    line = line.rstrip("\n")
    m = LINE_RE_TEXT.match(line)
    if m:
        frame = build_synthetic_ax25(m.group("src"), m.group("dst"), m.group("payload"))
        decoded = f"{m.group('src').upper()}>{m.group('dst').upper()}:{m.group('payload')}"
        return m.group("ts"), m.group("src").upper(), frame, True, decoded

    m = LINE_RE_HEX.match(line)
    if m:
        try:
            ax25 = extract_ax25_from_kiss_dump(m.group("hex"))
        except ValueError:
            return None
        if ax25 is None:
            return None
        parsed_frame = parse_arica2_frame(ax25)
        if parsed_frame is None:
            return None
        dest_call, src_call, info_text = parsed_frame
        decoded = f"{src_call}>{dest_call}:{info_text}"
        return m.group("ts"), src_call.upper(), ax25, False, decoded

    return None


def iter_log_lines(path: str, follow: bool):
    with open(path, "r", errors="replace") as f:
        while True:
            line = f.readline()
            if line:
                yield line
                continue
            if not follow:
                return
            time.sleep(1.0)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--log-file", required=True, help="Path to the decoder's text log file")
    parser.add_argument(
        "--follow", action="store_true",
        help="Keep watching the log file for new lines (like 'tail -f'), for use "
             "while the decoder is still actively writing to it. Without this, the "
             "file is processed once and the tool exits.",
    )
    parser.add_argument(
        "--norad-id", type=int, default=98329,
        help="Satellite NORAD ID as registered in SatNOGS DB (default: 98329 -- "
             "SatNOGS DB's catalog ID for ARICA-2; this differs from ARICA-2's real/"
             "followed NORAD ID 68796, which is only used for TLE propagation).",
    )
    parser.add_argument(
        "--satellite-callsign", default="JS1YSD",
        help="Only forward frames whose AX.25 source matches this callsign "
             "(default: JS1YSD, ARICA-2).",
    )
    parser.add_argument(
        "--source-callsign", required=True,
        help="Your ground station's callsign, reported to SatNOGS as the receiving "
             "station (the 'source' field in the SiDS submission).",
    )
    parser.add_argument("--latitude", default="0.0N", help="Station latitude, WGS84 (default: 0.0N)")
    parser.add_argument("--longitude", default="0.0E", help="Station longitude, WGS84 (default: 0.0E)")
    tz_group = parser.add_mutually_exclusive_group()
    tz_group.add_argument(
        "--utc-offset", type=float, default=None,
        help="Fixed hours offset from UTC that the log's local timestamps are in "
             "(e.g. -8 for PST, -7 for PDT, 9 for JST). Does not account for DST "
             "changes within the log automatically -- set this to whatever offset "
             "was actually in effect, or use --tz-name instead for automatic DST "
             "handling. If neither is given, this machine's own system timezone "
             "setting is used.",
    )
    tz_group.add_argument(
        "--tz-name", default=None,
        help="IANA timezone name the log's local timestamps are in (e.g. "
             "'America/Los_Angeles', 'Asia/Tokyo'). Handles DST transitions "
             "automatically. Requires Python 3.9+.",
    )
    parser.add_argument(
        "--api-token", default=None,
        help="Optional SatNOGS DB API token, sent as an Authorization header. Not "
             "required for telemetry submission as of this writing.",
    )
    parser.add_argument("--dev", action="store_true", help="Submit to the SatNOGS DB dev server instead of production")
    parser.add_argument("--dry-run", action="store_true", help="Parse and print frames but do not upload")
    parser.add_argument("-v", "--verbose", action="store_true", help="Print filtered/skipped/unparsed lines too")
    args = parser.parse_args()

    if not args.dry_run and requests is None:
        print("The 'requests' package is required unless using --dry-run.", file=sys.stderr)
        print("Install it with: pip install requests --break-system-packages", file=sys.stderr)
        sys.exit(1)

    sat_call = args.satellite_callsign.strip().upper()
    url = SIDS_DEV_URL if args.dev else SIDS_URL
    headers = {"Authorization": f"Token {args.api_token}"} if args.api_token else {}
    tzinfo = resolve_tzinfo(args.utc_offset, args.tz_name)

    if args.tz_name:
        tz_desc = f"{args.tz_name} (explicit, handles DST)"
    elif args.utc_offset is not None:
        tz_desc = f"UTC{args.utc_offset:+g} (explicit fixed offset)"
    else:
        tz_desc = f"{tzinfo} (this machine's system timezone -- consider --tz-name or --utc-offset to be explicit)"

    print("arica2_log_to_satnogs starting.")
    print(f"  log file: {args.log_file}{' (following)' if args.follow else ''}")
    print(f"  NORAD ID: {args.norad_id}")
    print(f"  Reporting as source: {args.source_callsign}")
    print(f"  Only forwarding frames from satellite callsign: {sat_call}")
    print(f"  Log timestamps assumed to be in: {tz_desc}")
    print(f"  SatNOGS DB endpoint: {url}" + (" (DRY RUN, not submitting)" if args.dry_run else ""))
    print("Press Ctrl+C to stop.\n" if args.follow else "")

    try:
        for line in iter_log_lines(args.log_file, args.follow):
            parsed = parse_log_line(line)
            now = time.strftime("%H:%M:%S")
            if parsed is None:
                if args.verbose and line.strip():
                    print(f"[{now}] [skip] unparsed line: {line.strip()[:80]}")
                continue

            ts_str, src_call, ax25, reconstructed, decoded_text = parsed

            if src_call != sat_call:
                if args.verbose:
                    print(f"[{now}] [skip] {src_call} (not {sat_call})")
                continue

            try:
                ts_utc = parse_local_to_utc(ts_str, tzinfo)
            except ValueError as exc:
                print(f"[{now}] [error] could not parse timestamp '{ts_str}': {exc}", file=sys.stderr)
                continue

            tag = "(reconstructed frame)" if reconstructed else "(exact frame)"
            print(f"[{now}] downlink frame from {src_call} at {ts_str} local "
                  f"-> {ts_utc} UTC {tag}, {len(ax25)} bytes")
            print(f"           hex:  {ax25.hex().upper()}")
            print(f"           text: {decoded_text}")

            if args.dry_run:
                continue

            payload = build_sids_payload(
                args.norad_id, args.source_callsign, ax25, args.latitude, args.longitude, ts_utc
            )
            try:
                r = requests.post(url, data=payload, headers=headers, timeout=15)
                r.raise_for_status()
                print("           -> submitted to SatNOGS DB")
            except Exception as exc:  # noqa: BLE001
                print(f"           -> [error] submission failed: {exc}", file=sys.stderr)

    except KeyboardInterrupt:
        print("\nStopped.")
    except FileNotFoundError:
        print(f"[error] log file not found: {args.log_file}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
