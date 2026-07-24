"""Bench-test the NfcReader service standalone, without the daemon.

Polls the tag/status snapshots once a second and prints them whenever they
change. Type a short text and press enter to test the WRITE handshake.

Usage:
    python examples/arduino_nfc/reader_bench.py            # auto-detect port
    python examples/arduino_nfc/reader_bench.py COM5       # explicit port
"""

import sys
import threading
import time

from reachy_mini.nfc import NfcReader, find_nfc_ports


def main() -> None:
    port = sys.argv[1] if len(sys.argv) > 1 else "auto"
    print(f"Candidate ports: {find_nfc_ports() or 'none'}; using: {port}")

    reader = NfcReader(port=port)
    reader.start()

    def poll() -> None:
        last = ""
        while True:
            tag = reader.get_tag()
            status = reader.get_status()
            line = (
                f"connected={status.connected} module={status.module_detected} "
                f"port={status.port} present={tag.present} uid={tag.uid} "
                f"content={tag.content!r} blank={tag.blank} "
                f"last_line={status.last_line!r}"
            )
            if line != last:
                print(line)
                last = line
            time.sleep(1.0)

    threading.Thread(target=poll, daemon=True).start()

    print("Type text + enter to write it to the next tag; ctrl-c to quit.")
    try:
        for entered in sys.stdin:
            text = entered.strip()
            if not text:
                continue
            print(f"Writing {text!r} (present a tag within ~5 s)...")
            result = reader.write(text)
            print(f"Write result: success={result.success} error={result.error}")
    except KeyboardInterrupt:
        pass
    finally:
        reader.stop()


if __name__ == "__main__":
    main()
