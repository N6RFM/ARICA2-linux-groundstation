# satnogs-tools

Tools for forwarding received ARICA-2 downlink frames to SatNOGS DB,
without forwarding your own uplink transmissions.

- `arica2_satnogs_forwarder.py` -- for a live Direwolf KISS TCP feed
- `arica2_log_to_satnogs.py` -- for a decoder's text log file (handles
  both cleanly decoded lines and raw hex dump fallback lines, and
  converts local timestamps to UTC)

Key facts for ARICA-2: submit with NORAD ID `98329` (SatNOGS DB's
catalog ID -- not the real/followed NORAD ID 68796, which is only used
for TLE propagation) and filter on satellite callsign `JS1YSD`.

Run either tool with `--help` for full usage, or `--dry-run -v` to see
what would be forwarded without submitting anything.
