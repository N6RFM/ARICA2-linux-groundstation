#!/usr/bin/env python3
"""
arica2_satnogs_forwarder.py

Forward received ARICA-2 (or any satellite's) downlink AX.25 frames,
decoded by Direwolf, to the SatNOGS DB via the SiDS (SatNOGS Ident Data
Store) protocol -- WITHOUT forwarding your own uplink transmissions.

HOW IT WORKS
------------
Direwolf exposes a KISS-over-TCP server (default port 8001). This tool
connects to that port as a client, reads every KISS frame Direwolf
sends, unwraps the KISS framing to get the raw AX.25 frame, and parses
the AX.25 source address. Frames whose source callsign matches one of
your own station's callsigns (e.g. because of TX/RX leakage on a
shared antenna, or Direwolf echoing what it just transmitted) are
skipped; everything else is forwarded to SatNOGS DB as a SiDS
telemetry submission.

This never touches your uplink in any way -- it only listens to
Direwolf's existing KISS output, the same stream any other KISS client
would use to view received frames.

SUBMISSION FORMAT
------------------
This follows the SiDS protocol used by SatNOGS DB's telemetry API
(https://db.satnogs.org/api/telemetry/), a simple form-encoded POST
with fields: noradID, source, locator, latitude, longitude, timestamp,
frame (hex-encoded raw AX.25 bytes). As of this writing, no API token
is required for submission (an optional --api-token flag is provided
in case that changes).

REQUIREMENTS
------------
- Direwolf running with its KISS TCP interface enabled (the default;
  check KISSPORT in direwolf.conf, commonly 8001).
- `requests` (pip install requests --break-system-packages), unless
  running with --dry-run.

USAGE
-----
  python3 arica2_satnogs_forwarder.py \
      --norad-id 98329 \
      --source-callsign N6RFM \
      --satellite-callsign JS1YSD

  # Dry run -- parse and print frames, don't actually upload anything:
  python3 arica2_satnogs_forwarder.py \
      --norad-id 98329 --source-callsign N6RFM --satellite-callsign JS1YSD --dry-run -v

NOTE ON ARICA-2 SPECIFICALLY: SatNOGS DB's catalog entry for ARICA-2
uses NORAD ID 98329 (its "Followed NORAD ID" of 68796 is only used for
TLE/orbit propagation, not telemetry association) and ARICA-2's own
callsign is JS1YSD. Using --satellite-callsign JS1YSD is a stronger
filter than --my-callsign, since it also excludes unrelated
interference, not just your own uplink.
"""

import argparse
import datetime
import socket
import sys
import time

try:
    import requests
except ImportError:
    requests = None

FEND = 0xC0
FESC = 0xDB
TFEND = 0xDC
TFESC = 0xDD

SIDS_URL = "https://db.satnogs.org/api/telemetry/"
SIDS_DEV_URL = "https://db-dev.satnogs.org/api/telemetry/"


def kiss_unescape(data: bytes) -> bytes:
    """Reverse KISS byte-stuffing (FESC TFEND -> FEND, FESC TFESC -> FESC)."""
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


def read_kiss_frames(sock: socket.socket):
    """Generator yielding (kiss_command_byte, ax25_payload) as full KISS
    frames (delimited by FEND bytes) arrive on the socket."""
    buf = bytearray()
    in_frame = False
    while True:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("Direwolf KISS connection closed")
        for b in chunk:
            if b == FEND:
                if in_frame and buf:
                    raw = kiss_unescape(bytes(buf))
                    if raw:
                        yield raw[0], raw[1:]
                buf = bytearray()
                in_frame = True
            elif in_frame:
                buf.append(b)


def parse_ax25_addr(field: bytes):
    """Parse one 7-byte AX.25 address field -> (callsign, ssid, is_last)."""
    call = "".join(chr(b >> 1) for b in field[:6]).strip()
    ssid = (field[6] >> 1) & 0x0F
    is_last = bool(field[6] & 0x01)
    return call, ssid, is_last


def parse_ax25_source(frame: bytes):
    """Extract (source_callsign, source_ssid) from a raw AX.25 frame.
    Destination occupies bytes 0:7, Source occupies bytes 7:14,
    regardless of whether repeater addresses follow. Returns None if
    the frame is too short to contain a valid header.
    """
    if len(frame) < 14:
        return None
    src_call, src_ssid, _ = parse_ax25_addr(frame[7:14])
    return src_call, src_ssid


def build_sids_payload(norad_id: int, source_callsign: str, frame: bytes,
                        latitude: str, longitude: str) -> dict:
    ts = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds")
    ts = ts.replace("+00:00", "Z")
    return {
        "noradID": norad_id,
        "source": source_callsign,
        "locator": "longLat",
        "latitude": latitude,
        "longitude": longitude,
        "timestamp": ts,
        "frame": frame.hex().upper(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--norad-id", type=int, default=98329,
        help="Satellite NORAD ID to submit telemetry for, as registered in SatNOGS DB "
             "(default: 98329 -- SatNOGS DB's catalog ID for ARICA-2; note this differs "
             "from ARICA-2's real/followed NORAD ID 68796, which is only used for TLE "
             "propagation, not telemetry association. Double-check against the "
             "satellite's SatNOGS DB page before relying on any default.)",
    )
    parser.add_argument(
        "--satellite-callsign", default=None,
        help="If given, ONLY forward frames whose AX.25 source matches this callsign "
             "(e.g. JS1YSD for ARICA-2) -- a stronger, positive filter than "
             "--my-callsign. Recommended when you know the satellite's own callsign, "
             "since it also guards against unrelated interference, not just your own "
             "uplink.",
    )
    parser.add_argument(
        "--my-callsign", action="append", default=[],
        help="Your own callsign(s) used for uplink -- frames with this as the AX.25 "
             "source are treated as your own TX (e.g. heard via antenna/duplexer "
             "leakage) and are NOT forwarded. Repeatable for multiple SSIDs. Ignored "
             "if --satellite-callsign is given.",
    )
    parser.add_argument(
        "--source-callsign", required=True,
        help="Your ground station's callsign, reported to SatNOGS as the receiving "
             "station (the 'source' field in the SiDS submission).",
    )
    parser.add_argument("--latitude", default="0.0N", help="Station latitude, WGS84 (default: 0.0N)")
    parser.add_argument("--longitude", default="0.0E", help="Station longitude, WGS84 (default: 0.0E)")
    parser.add_argument("--kiss-host", default="localhost", help="Direwolf KISS TCP host (default: localhost)")
    parser.add_argument("--kiss-port", type=int, default=8001, help="Direwolf KISS TCP port (default: 8001)")
    parser.add_argument(
        "--api-token", default=None,
        help="Optional SatNOGS DB API token, sent as an Authorization header. Not "
             "required for telemetry submission as of this writing.",
    )
    parser.add_argument("--dev", action="store_true", help="Submit to the SatNOGS DB dev server instead of production")
    parser.add_argument("--dry-run", action="store_true", help="Parse and print frames but do not upload")
    parser.add_argument("-v", "--verbose", action="store_true", help="Print filtered/skipped frames too")
    args = parser.parse_args()

    if not args.dry_run and requests is None:
        print("The 'requests' package is required unless using --dry-run.", file=sys.stderr)
        print("Install it with: pip install requests --break-system-packages", file=sys.stderr)
        sys.exit(1)

    my_calls = {c.strip().upper() for c in args.my_callsign}
    sat_call = args.satellite_callsign.strip().upper() if args.satellite_callsign else None
    url = SIDS_DEV_URL if args.dev else SIDS_URL
    headers = {"Authorization": f"Token {args.api_token}"} if args.api_token else {}

    print("arica2_satnogs_forwarder starting.")
    print(f"  Direwolf KISS: {args.kiss_host}:{args.kiss_port}")
    print(f"  NORAD ID: {args.norad_id}")
    print(f"  Reporting as source: {args.source_callsign}")
    if sat_call:
        print(f"  Only forwarding frames from satellite callsign: {sat_call}")
    elif my_calls:
        print(f"  Excluding own callsign(s): {', '.join(sorted(my_calls))}")
    else:
        print("  [warning] No --satellite-callsign or --my-callsign given -- your own "
              "uplink frames (if heard by your receiver) will NOT be filtered out!")
    print(f"  SatNOGS DB endpoint: {url}" + (" (DRY RUN, not submitting)" if args.dry_run else ""))
    print("Press Ctrl+C to stop.\n")

    while True:
        try:
            sock = socket.create_connection((args.kiss_host, args.kiss_port))
            print("[info] connected to Direwolf KISS interface")
            for cmd, ax25 in read_kiss_frames(sock):
                if (cmd & 0x0F) != 0x00:
                    continue  # not a data frame

                ts = time.strftime("%H:%M:%S")
                parsed = parse_ax25_source(ax25)
                if parsed is None:
                    if args.verbose:
                        print(f"[{ts}] [skip] frame too short to parse AX.25 header "
                              f"({len(ax25)} bytes)")
                    continue

                src_call, src_ssid = parsed
                src_label = f"{src_call}-{src_ssid}" if src_ssid else src_call

                if sat_call is not None:
                    if src_call.upper() != sat_call:
                        if args.verbose:
                            print(f"[{ts}] [skip] {src_label} (not {sat_call}) -- not forwarding")
                        continue
                elif src_call.upper() in my_calls:
                    if args.verbose:
                        print(f"[{ts}] [skip] {src_label} (own uplink) -- not forwarding")
                    continue

                print(f"[{ts}] downlink frame from {src_label}, {len(ax25)} bytes")

                if args.dry_run:
                    continue

                payload = build_sids_payload(
                    args.norad_id, args.source_callsign, ax25, args.latitude, args.longitude
                )
                try:
                    r = requests.post(url, data=payload, headers=headers, timeout=15)
                    r.raise_for_status()
                    print("           -> submitted to SatNOGS DB")
                except Exception as exc:  # noqa: BLE001
                    print(f"           -> [error] submission failed: {exc}", file=sys.stderr)

        except (ConnectionError, OSError) as exc:
            print(f"[warning] Direwolf KISS connection error: {exc} -- retrying in 5s", file=sys.stderr)
            time.sleep(5)
        except KeyboardInterrupt:
            print("\nStopped.")
            break


if __name__ == "__main__":
    main()
