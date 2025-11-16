#!/usr/bin/env python3
"""
Conveyor sorting system (IPC)
- Metal detector: GPIO input
- Rejector: GPIO output
- Check weigher: RS232 read
- Barcode printer: RS232 write

Behavior:
- First item_name (entered at startup) saved as reference_item.
- For each incoming product (detected by weight pulse), evaluate:
    * metal? -> reject
    * item_name != reference_item -> reject
    * weight outside tolerance -> reject
  If accepted -> print barcode.
  If rejected -> schedule rejector pulse when product reaches rejector (REJECTION_DELAY).
"""

import threading
import time
import re
import queue
import sys
import signal
from collections import deque

# ---- Dependencies ----
# pip install pyserial python-periphery (optional)
try:
    import serial
except Exception as e:
    print("ERROR: pyserial is required. Install with: pip install pyserial")
    raise

# Try GPIO libraries in order (RPi.GPIO for Pi, periphery for generic Linux GPIO)
GPIO_IMPL = None
try:
    import RPi.GPIO as RPI_GPIO
    GPIO_IMPL = "RPi"
except Exception:
    try:
        from periphery import GPIO as PeriphGPIO
        GPIO_IMPL = "periphery"
    except Exception:
        GPIO_IMPL = None

# ---- CONFIGURATION ----
# GPIO pin numbering: if using RPi library it's BCM; for periphery use Linux GPIO numbers.
METAL_GPIO_PIN = 17         # input from metal detector (change to your pin)
REJECTOR_GPIO_PIN = 27      # output to rejector (change as needed)
METAL_ACTIVE_HIGH = True    # if metal detector sets line HIGH when metal present

REJECTOR_PULSE_SECONDS = 0.25   # pulse length to trigger rejector (sec)
REJECTION_DELAY = 2.5           # seconds from detection point to rejector point (tune)

# Checkweigher serial config
WEIGHER_PORT = "/dev/ttyUSB0"
WEIGHER_BAUD = 9600
WEIGHER_TIMEOUT = 0.5  # seconds per read

# Barcode printer serial config
PRINTER_PORT = "/dev/ttyUSB1"
PRINTER_BAUD = 9600
PRINTER_TIMEOUT = 1

# Weight tolerance (grams)
WEIGHT_MIN = 95.0
WEIGHT_MAX = 105.0

# Item identification:
# We assume operator enters item_name at start. Optionally you can connect a scanner to another serial port and fill current_item_name.
REFERENCE_ITEM = None

# Operational queues
# When an item is "detected" (weight pulse), we will read the current metal flag and weight and enqueue a detection event.
detection_queue = deque()   # stores tuples: (timestamp, item_name, weight, metal_flag)

# Logs
accepted = []
rejected = []

# Thread control
running = True

# ---- GPIO helper ----
class GPIOInterface:
    def __init__(self):
        self.impl = GPIO_IMPL
        self.input_pin = METAL_GPIO_PIN
        self.output_pin = REJECTOR_GPIO_PIN
        self._gpio_in = None
        self._gpio_out = None
        if self.impl == "RpI" or self.impl == "RPi":
            # RPi.GPIO usage
            RPI_GPIO.setmode(RPI_GPIO.BCM)
            RPI_GPIO.setup(self.input_pin, RPI_GPIO.IN, pull_up_down=RPI_GPIO.PUD_DOWN)
            RPI_GPIO.setup(self.output_pin, RPI_GPIO.OUT)
            RPI_GPIO.output(self.output_pin, RPI_GPIO.LOW)
            self._gpio_in = RPI_GPIO
        elif self.impl == "periphery":
            # periphery uses linux GPIO numbers - user must set correct numbers
            self._gpio_in = PeriphGPIO(self.input_pin, "in")
            self._gpio_out = PeriphGPIO(self.output_pin, "out")
            self._gpio_out.write(False)
        else:
            print("GPIO: No GPIO library found. Running in SIMULATION mode.")
            # simulation: just store states
            self._state_in = False
            self._state_out = False

    def read_metal(self):
        if self.impl == "RPi":
            val = self._gpio_in.input(self.input_pin)
            return bool(val) if METAL_ACTIVE_HIGH else not bool(val)
        elif self.impl == "periphery":
            val = self._gpio_in.read()
            return bool(val) if METAL_ACTIVE_HIGH else not bool(val)
        else:
            # simulation mode: return stored state
            return bool(self._state_in)

    def set_sim_input(self, state: bool):
        if self.impl is None:
            self._state_in = bool(state)

    def pulse_rejector(self, seconds=REJECTOR_PULSE_SECONDS):
        if self.impl == "RPi":
            RPI_GPIO.output(self.output_pin, RPI_GPIO.HIGH)
            time.sleep(seconds)
            RPI_GPIO.output(self.output_pin, RPI_GPIO.LOW)
        elif self.impl == "periphery":
            self._gpio_out.write(True)
            time.sleep(seconds)
            self._gpio_out.write(False)
        else:
            # simulation: print
            print(f"[SIM] Rejector pulse {seconds}s")
            self._state_out = True
            time.sleep(seconds)
            self._state_out = False

gpio = GPIOInterface()

# ---- Serial / Device readers ----

# Weigher reader thread: reads weight values from serial and posts "item events" when weight crosses threshold.
# Strategy: watch weight stream; when stable baseline then an item weight spike appears above a small threshold -> treat as item present.
class WeigherReader(threading.Thread):
    def __init__(self, port, baud, timeout=0.5):
        super().__init__(daemon=True)
        self.port = port
        self.baud = baud
        self.timeout = timeout
        self.ser = None
        self.last_weight = 0.0
        self.item_present = False
        self.item_entry_time = None

    def connect(self):
        try:
            self.ser = serial.Serial(self.port, self.baud, timeout=self.timeout)
            print(f"[Weigher] Connected to {self.port} @ {self.baud}")
        except Exception as e:
            print(f"[Weigher] ERROR opening {self.port}: {e}")
            self.ser = None

    def parse_weight_line(self, line: str):
        """
        Parse a line from the checkweigher to extract weight in grams.
        Many weighers output something like:
          "WT: 100.5 g"
          "100.5"
          "S  100.5 kg"
        Adjust this parser to match your device.
        """
        if not line:
            return None
        # find the first float number
        m = re.search(r"(-?\d+\.\d+|-?\d+)", line)
        if m:
            try:
                return float(m.group(0))
            except:
                return None
        return None

    def run(self):
        self.connect()
        # simple running median/baseline to detect item pulses
        baseline = 0.0
        baseline_samples = []
        while running:
            weight = None
            if self.ser and self.ser.in_waiting:
                try:
                    raw = self.ser.readline().decode(errors="ignore").strip()
                except Exception:
                    raw = ""
                if raw:
                    w = self.parse_weight_line(raw)
                    if w is not None:
                        weight = w
            else:
                # if no serial or nothing to read, sleep shortly
                time.sleep(0.01)

            if weight is None:
                # no new weight reading; continue
                continue

            self.last_weight = weight
            # maintain baseline when no item present
            if not self.item_present:
                baseline_samples.append(weight)
                if len(baseline_samples) > 50:
                    baseline_samples.pop(0)
                baseline = sum(baseline_samples)/len(baseline_samples)
            # detection threshold
            THRESH = 10.0  # grams above baseline to see something (tune)
            if not self.item_present and (weight - baseline) > THRESH:
                # item entered detection point
                self.item_present = True
                self.item_entry_time = time.time()
                # read metal flag and get current item_name (from operator input or optional scanner)
                current_metal = gpio.read_metal()
                # For item_name: use global REFERENCE_ITEM or interactive/manual current item
                # We'll ask user to input current item name via console if you wish (non-blocking not simple).
                # Instead, we use the global variable CURRENT_ITEM_NAME if set externally; else set to None.
                item_name = CURRENT_ITEM_NAME.get() if CURRENT_ITEM_NAME else None
                detection_queue.append((self.item_entry_time, item_name, weight, current_metal))
                print(f"[Detect] item at {self.item_entry_time:.3f} weight={weight:.2f} metal={current_metal} name={item_name}")
            elif self.item_present and (weight - baseline) < (THRESH * 0.5):
                # item left detection area -> reset
                self.item_present = False
                self.item_entry_time = None

# Simple thread-safe holder for current item name (if you have a scanner feeding it)
class CurrentItemName:
    def __init__(self):
        self._lock = threading.Lock()
        self._name = None
    def set(self, n):
        with self._lock:
            self._name = n
    def get(self):
        with self._lock:
            return self._name

CURRENT_ITEM_NAME = CurrentItemName()

# Optional scanner reader thread (if you connect a barcode scanner to RS232 and it sends item names)
class ScannerReader(threading.Thread):
    def __init__(self, port, baud, timeout=0.5):
        super().__init__(daemon=True)
        self.port = port
        self.baud = baud
        self.timeout = timeout
        self.ser = None

    def connect(self):
        try:
            self.ser = serial.Serial(self.port, self.baud, timeout=self.timeout)
            print(f"[Scanner] Connected to {self.port} @ {self.baud}")
        except Exception as e:
            print(f"[Scanner] Could not open scanner port {self.port}: {e}")
            self.ser = None

    def run(self):
        self.connect()
        while running:
            if self.ser and self.ser.in_waiting:
                raw = self.ser.readline().decode(errors="ignore").strip()
                if raw:
                    # set current item name for the next detection(s)
                    CURRENT_ITEM_NAME.set(raw)
                    print(f"[Scanner] current item set -> {raw}")
            else:
                time.sleep(0.05)

# Printer helper
class BarcodePrinter:
    def __init__(self, port, baud, timeout=1):
        self.port = port
        self.baud = baud
        self.timeout = timeout
        self.ser = None
        self.connect()

    def connect(self):
        try:
            self.ser = serial.Serial(self.port, self.baud, timeout=self.timeout)
            print(f"[Printer] Connected to {self.port} @ {self.baud}")
        except Exception as e:
            print(f"[Printer] ERROR opening {self.port}: {e}")
            self.ser = None

    def print_label(self, item_name, weight):
        if not self.ser:
            print("[Printer] No serial port available. Skipping print.")
            return
        # Very simple printer text. Adjust for your printer's language (ZPL, EPL, TSPL, ESC/POS).
        # Example: send plain text and a newline. Most label printers accept vendor-specific commands.
        text = f"ITEM:{item_name} WT:{weight:.2f}g\n\n"
        try:
            self.ser.write(text.encode())
            self.ser.flush()
            print(f"[Printer] Printed label for {item_name}, weight {weight:.2f}g")
        except Exception as e:
            print(f"[Printer] Write error: {e}")

# ---- Main control thread ----
def rejector_worker(delay, pulse_seconds):
    """Background worker that sleeps 'delay' seconds then pulses rejector."""
    print(f"[RejectorWorker] Scheduling reject in {delay:.3f}s")
    time.sleep(delay)
    print("[RejectorWorker] Activating rejector now")
    gpio.pulse_rejector(pulse_seconds)

def process_detection_event(event):
    ts, item_name, weight, metal_flag = event
    # If item_name is None use REFERENCE_ITEM as a fallback for comparison (operator-entered)
    name = item_name if item_name else REFERENCE_ITEM
    reason = None
    if metal_flag:
        reason = "Metal detected"
    elif name != REFERENCE_ITEM:
        reason = f"Different item (got {name})"
    elif not (WEIGHT_MIN <= weight <= WEIGHT_MAX):
        reason = f"Weight out of bounds ({weight:.2f}g)"

    if reason:
        rejected.append((time.time(), name, weight, reason))
        print(f"[REJECT] {name} @ {weight:.2f}g -> {reason}")
        # schedule rejector activation after REJECTION_DELAY
        threading.Thread(target=rejector_worker, args=(REJECTION_DELAY, REJECTOR_PULSE_SECONDS), daemon=True).start()
    else:
        accepted.append((time.time(), name, weight))
        print(f"[ACCEPT] {name} @ {weight:.2f}g -> printing label")
        # print label
        printer.print_label(name, weight)

# ---- Graceful shutdown ----
def signal_handler(sig, frame):
    global running
    print("Shutting down...")
    running = False

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# ---- Start components ----
weigher = WeigherReader(WEIGHER_PORT, WEIGHER_BAUD, WEIGHER_TIMEOUT)
weigher.start()

# If you have a physical scanner on another serial port, create and start ScannerReader with correct port
# scanner = ScannerReader("/dev/ttyUSB2", 9600)
# scanner.start()

printer = BarcodePrinter(PRINTER_PORT, PRINTER_BAUD, PRINTER_TIMEOUT)

# ---- Startup flow: set reference item manually ----
def ask_reference_item():
    global REFERENCE_ITEM
    print("\n--- SET REFERENCE ITEM ---")
    print("Place the FIRST item on the line at detection point, then enter its name (e.g., item_A).")
    REFERENCE_ITEM = input("Enter reference item name: ").strip()
    if not REFERENCE_ITEM:
        print("No reference entered. Exiting.")
        sys.exit(1)
    print(f"Reference item set -> '{REFERENCE_ITEM}'\n")

ask_reference_item()

print("System running. Press Ctrl+C to stop.")
# ---- Main loop: consume detection_queue ----
while running:
    try:
        if detection_queue:
            event = detection_queue.popleft()
            process_detection_event(event)
        else:
            time.sleep(0.05)
    except Exception as e:
        print("Main loop error:", e)

# cleanup gpio if needed
print("Final accepted:", len(accepted), "rejected:", len(rejected))
