#!/usr/bin/env python3
"""Reset ESP32 via RTS pin and capture boot logs."""
import serial
import sys
import time

PORT = "/dev/cu.usbmodem1101"
BAUD = 115200
LOG_FILE = "/tmp/jarvis_serial.log"

def main():
    print(f"Opening {PORT}...")
    ser = serial.Serial(PORT, BAUD, timeout=1)

    # Reset ESP32 via RTS toggle (like idf.py monitor does)
    print("Resetting ESP32 via RTS...")
    ser.setDTR(False)
    ser.setRTS(True)
    time.sleep(0.1)
    ser.setRTS(False)
    time.sleep(0.1)
    ser.setDTR(False)

    ser.reset_input_buffer()
    print(f"Capturing boot logs to {LOG_FILE}...\n")

    with open(LOG_FILE, "w") as f:
        f.write(f"=== BOOT CAPTURE {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
        try:
            while True:
                line = ser.readline()
                if line:
                    try:
                        text = line.decode("utf-8", errors="replace").rstrip()
                    except:
                        text = str(line)
                    ts = time.strftime("%H:%M:%S")
                    entry = f"[{ts}] {text}"
                    print(entry)
                    f.write(entry + "\n")
                    f.flush()
        except KeyboardInterrupt:
            print("\nStopped.")
        finally:
            ser.close()

if __name__ == "__main__":
    main()
