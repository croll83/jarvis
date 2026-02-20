#!/usr/bin/env python3
"""Serial monitor for JARVIS AtomS3R — reads ESP32 logs via pyserial."""
import serial
import sys
import time

PORT = "/dev/cu.usbmodem1101"
BAUD = 115200
LOG_FILE = "/tmp/jarvis_serial.log"

def main():
    print(f"Opening {PORT} at {BAUD} baud...")
    print(f"Logging to {LOG_FILE}")
    print("Press Ctrl+C to stop\n")

    try:
        ser = serial.Serial(PORT, BAUD, timeout=1)
    except serial.SerialException as e:
        print(f"ERROR: Cannot open {PORT}: {e}")
        sys.exit(1)

    # Clear any stale data
    ser.reset_input_buffer()

    with open(LOG_FILE, "w") as f:
        f.write(f"=== JARVIS Serial Monitor started at {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
        try:
            while True:
                line = ser.readline()
                if line:
                    try:
                        text = line.decode("utf-8", errors="replace").rstrip()
                    except:
                        text = str(line)
                    timestamp = time.strftime("%H:%M:%S")
                    entry = f"[{timestamp}] {text}"
                    print(entry)
                    f.write(entry + "\n")
                    f.flush()
        except KeyboardInterrupt:
            print("\n\nMonitor stopped.")
        finally:
            ser.close()

if __name__ == "__main__":
    main()
