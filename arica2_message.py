#!/usr/bin/env python3
"""
ARICA-2 Message tool - native Linux port.

Reverse-engineered from the original ARICA-2_Message.exe (a small Mono/.NET
GUI app by JI1IZR) by disassembling its IL with `monodis` and reading the
Transmit_Button_Click / ClientListen methods directly.

Behavior is a byte-for-byte match of the original:
  - Connects over TCP to a KISS TNC port (Direwolf: 8001, hs_soundmodem: 8100)
  - Builds the same fixed 22-byte KISS frame for Download/Upload/Confirm/Parrot
  - Decodes incoming standard AX.25 UI frames the same way for display
    ("SRC>DST:message")

Requires only Python 3 + Tkinter (stdlib) - no Wine, no extra packages.
"""

import socket
import threading
import queue
import time
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox

DEFAULT_HOST = "127.0.0.1"
DIREWOLF_PORT = 8001
HS_SOUNDMODEM_PORT = 8100


def build_frame(cmd_type: str, callsign: str, message: str = "", msg_id: int = 1) -> bytes:
    """Build the 22-byte KISS uplink frame exactly as the original app does."""
    buf = bytearray(22)
    buf[0] = 0xC0
    buf[1] = 0x00
    buf[2] = 0x42
    buf[3] = 0xF8
    buf[4] = 0xBD
    buf[5] = 0x40
    buf[6] = 0x00

    # callsign: up to 6 raw ASCII chars, written starting at offset 7
    cs = callsign.encode("ascii", errors="ignore")[:6]
    for i, b in enumerate(cs):
        buf[7 + i] = b

    if cmd_type == "Download":
        # msg_id is 1..20, as shown in the original combo box
        v10 = msg_id & 0xFF
        v11 = v10 // 2
        v12 = (v10 - v11 * 2) * 128
        buf[5] = (v11 + 0x40) & 0xFF
        buf[6] = v12 & 0xFF

    elif cmd_type == "Upload":
        buf[5] = 0x50
        msg = message.encode("ascii", errors="ignore")[:8]
        for i, b in enumerate(msg):
            buf[13 + i] = b

    elif cmd_type == "Confirm":
        buf[5] = 0x60

    elif cmd_type == "Parrot":
        buf[5] = 0x70
        msg = message.encode("ascii", errors="ignore")[:8]
        for i, b in enumerate(msg):
            buf[13 + i] = b

    buf[21] = 0xC0
    return bytes(buf)


def build_txdelay_frame(seconds: float) -> bytes:
    """
    KISS command frame to set TXDELAY on port 0, in 10ms units.
    Sent as its own KISS frame right before the data frame, so the user
    can choose the radio key-up time per transmission from the GUI while
    Direwolf (the actual PTT/modem owner) is the one that enforces it.
    """
    units = int(round(seconds * 100))
    units = max(0, min(255, units))
    return bytes([0xC0, 0x01, units, 0xC0])


def decode_ax25_ui(raw: bytes):
    """
    Decode a standard AX.25 UI frame as received in a raw KISS byte stream,
    the same way the original DispRcvMessage/ClientListen code does:
        raw[0]     = KISS FEND
        raw[1]     = KISS cmd
        raw[2:8]   = destination callsign, shifted ASCII (<<1), 6 chars
        raw[9:15]  = source callsign, shifted ASCII (<<1), 6 chars
        raw[16]    = control
        raw[17]    = pid
        raw[18:-1] = info (raw ASCII)
        raw[-1]    = KISS FEND
    Returns a "SRC>DST:info" string, or None if the frame is too short.
    """
    if len(raw) < 19:
        return None

    def shifted_call(offset):
        chars = []
        for i in range(6):
            chars.append(chr(raw[offset + i] // 2))
        return "".join(chars).strip()

    src = shifted_call(9)
    dst = shifted_call(2)
    info = bytes(raw[18:len(raw) - 1])
    try:
        info_text = info.decode("ascii", errors="replace")
    except Exception:
        info_text = repr(info)

    return f"{src}>{dst}:{info_text}"


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("ARICA-2 Message (Linux)")
        self.resizable(False, False)

        self.client_sock = None
        self.listen_thread = None
        self.stop_listen = threading.Event()
        self.rx_queue = queue.Queue()

        self._build_ui()
        self.after(100, self._poll_queue)

    def _build_ui(self):
        pad = {"padx": 6, "pady": 4}

        # --- TNC connection group ---
        tnc_frame = ttk.LabelFrame(self, text="TNC")
        tnc_frame.grid(row=0, column=0, columnspan=2, sticky="ew", **pad)

        self.tnc_choice = tk.StringVar(value="direwolf")
        ttk.Radiobutton(
            tnc_frame, text="Direwolf (8001)", variable=self.tnc_choice,
            value="direwolf", command=self._tnc_choice_changed
        ).grid(row=0, column=0, sticky="w", padx=4)
        ttk.Radiobutton(
            tnc_frame, text="hs_soundmodem (8100)", variable=self.tnc_choice,
            value="hs_soundmodem", command=self._tnc_choice_changed
        ).grid(row=0, column=1, sticky="w", padx=4)

        ttk.Label(tnc_frame, text="Host:").grid(row=1, column=0, sticky="e")
        self.host_var = tk.StringVar(value=DEFAULT_HOST)
        ttk.Entry(tnc_frame, textvariable=self.host_var, width=14).grid(row=1, column=1, sticky="w")

        self.port_var = tk.IntVar(value=DIREWOLF_PORT)
        ttk.Label(tnc_frame, text="Port:").grid(row=1, column=2, sticky="e")
        ttk.Entry(tnc_frame, textvariable=self.port_var, width=6).grid(row=1, column=3, sticky="w")

        self.connect_btn = ttk.Button(tnc_frame, text="Connect", command=self._toggle_connect)
        self.connect_btn.grid(row=1, column=4, padx=8)

        self.warning_var = tk.StringVar(value="**********")
        ttk.Label(self, textvariable=self.warning_var, foreground="red").grid(
            row=1, column=0, columnspan=2, **pad
        )

        # --- Message composition group ---
        msg_frame = ttk.LabelFrame(self, text="Message")
        msg_frame.grid(row=2, column=0, columnspan=2, sticky="ew", **pad)

        ttk.Label(msg_frame, text="Command type:").grid(row=0, column=0, sticky="e")
        self.type_var = tk.StringVar(value="Download")
        type_combo = ttk.Combobox(
            msg_frame, textvariable=self.type_var, state="readonly",
            values=["Download", "Upload", "Confirm", "Parrot"], width=12
        )
        type_combo.grid(row=0, column=1, sticky="w")
        type_combo.bind("<<ComboboxSelected>>", lambda e: self._update_field_state())

        ttk.Label(msg_frame, text="Message ID (1-20):").grid(row=1, column=0, sticky="e")
        self.msgid_var = tk.StringVar(value="1")
        self.msgid_combo = ttk.Combobox(
            msg_frame, textvariable=self.msgid_var, state="readonly",
            values=[str(i) for i in range(1, 21)], width=6
        )
        self.msgid_combo.grid(row=1, column=1, sticky="w")

        ttk.Label(msg_frame, text="Callsign (max 6):").grid(row=2, column=0, sticky="e")
        self.callsign_var = tk.StringVar(value="URCALL")
        ttk.Entry(msg_frame, textvariable=self.callsign_var, width=10).grid(row=2, column=1, sticky="w")

        ttk.Label(msg_frame, text="Message (max 8):").grid(row=3, column=0, sticky="e")
        self.message_var = tk.StringVar(value="")
        self.message_entry = ttk.Entry(msg_frame, textvariable=self.message_var, width=10)
        self.message_entry.grid(row=3, column=1, sticky="w")

        ttk.Label(msg_frame, text="Key-up time (sec):").grid(row=4, column=0, sticky="e")
        self.txdelay_var = tk.DoubleVar(value=0.3)
        ttk.Spinbox(
            msg_frame, textvariable=self.txdelay_var,
            from_=0.0, to=2.55, increment=0.05, width=8
        ).grid(row=4, column=1, sticky="w")

        self.transmit_btn = ttk.Button(msg_frame, text="Transmit", command=self._transmit)
        self.transmit_btn.grid(row=5, column=0, columnspan=2, pady=6)

        self._update_field_state()

        # --- Received messages ---
        rx_frame = ttk.LabelFrame(self, text="Received")
        rx_frame.grid(row=3, column=0, columnspan=2, sticky="ew", **pad)

        self.rx_text = scrolledtext.ScrolledText(rx_frame, width=60, height=12, state="normal")
        self.rx_text.pack(padx=4, pady=4)

        ttk.Button(rx_frame, text="Save log", command=self._save_log).pack(pady=(0, 6))

    def _tnc_choice_changed(self):
        if self.tnc_choice.get() == "direwolf":
            self.port_var.set(DIREWOLF_PORT)
        else:
            self.port_var.set(HS_SOUNDMODEM_PORT)

    def _update_field_state(self):
        t = self.type_var.get()
        self.msgid_combo.configure(state="readonly" if t == "Download" else "disabled")
        self.message_entry.configure(state="normal" if t in ("Upload", "Parrot") else "disabled")

    def _toggle_connect(self):
        if self.connect_btn["text"] == "Connect":
            host = self.host_var.get().strip()
            port = self.port_var.get()
            self.warning_var.set("**********")
            try:
                self.client_sock = socket.create_connection((host, port), timeout=5)
            except Exception:
                self.warning_var.set("Start TNC!")
                return
            self.connect_btn.configure(text="Disconnect")
            self.stop_listen.clear()
            self.listen_thread = threading.Thread(target=self._client_listen, daemon=True)
            self.listen_thread.start()
        else:
            self.stop_listen.set()
            try:
                if self.client_sock:
                    self.client_sock.close()
            except Exception:
                pass
            self.client_sock = None
            self.connect_btn.configure(text="Connect")
            self.warning_var.set("**********")

    def _client_listen(self):
        sock = self.client_sock
        buf = b""
        while not self.stop_listen.is_set():
            try:
                sock.settimeout(1.0)
                data = sock.recv(1000)
            except socket.timeout:
                continue
            except Exception:
                break
            if not data:
                break
            buf += data
            # split on KISS FEND (0xC0) boundaries: frames look like C0 ... C0
            while True:
                start = buf.find(b"\xc0")
                if start == -1:
                    break
                end = buf.find(b"\xc0", start + 1)
                if end == -1:
                    break
                frame = buf[start:end + 1]
                buf = buf[end + 1:]
                text = decode_ax25_ui(frame)
                if text:
                    self.rx_queue.put(text)

    def _poll_queue(self):
        try:
            while True:
                text = self.rx_queue.get_nowait()
                ts = time.strftime("%Y-%m-%d %H:%M:%S")
                self.rx_text.insert("end", f"\n[{ts}] {text}")
                self.rx_text.see("end")
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    def _transmit(self):
        if not self.client_sock or self.connect_btn["text"] != "Disconnect":
            self.warning_var.set("Out of connection!")
            return

        cmd_type = self.type_var.get()
        callsign = self.callsign_var.get()
        message = self.message_var.get()
        try:
            msg_id = int(self.msgid_var.get())
        except ValueError:
            msg_id = 1

        try:
            keyup_seconds = float(self.txdelay_var.get())
        except (tk.TclError, ValueError):
            keyup_seconds = 0.3

        delay_frame = build_txdelay_frame(keyup_seconds)
        frame = build_frame(cmd_type, callsign, message, msg_id)
        try:
            # Set TXDELAY first, over the same connected socket, then send
            # the message frame - Direwolf applies the new value to the
            # very next transmission on this port.
            self.client_sock.sendall(delay_frame)
            self.client_sock.sendall(frame)
        except Exception as exc:
            messagebox.showerror("Transmit failed", str(exc))

    def _save_log(self):
        fname = time.strftime("ARICA-2Log%Y%m%d_%H%M%S.log")
        with open(fname, "w") as f:
            f.write(self.rx_text.get("1.0", "end"))
        messagebox.showinfo("Saved", f"Log saved to {fname}")


if __name__ == "__main__":
    App().mainloop()
