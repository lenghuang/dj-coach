import json
import time
import threading
import mido
import numpy as np
import sounddevice as sd
import soundfile as sf
from datetime import datetime

MIDI_CONTROLS = {
    10: ("Trim Left", "Channel 1 Trim"),
    17: ("EQ High Left", "Channel 1 High EQ"),
    18: ("EQ Mid Left", "Channel 1 Mid EQ"),
    27: ("EQ Low Left", "Channel 1 Low EQ"),
    43: ("Filter Left", "Channel 1 Filter"),
    11: ("Trim Right", "Channel 2 Trim"),
    40: ("EQ High Right", "Channel 2 High EQ"),
    41: ("EQ Mid Right", "Channel 2 Mid EQ"),
    42: ("EQ Low Right", "Channel 2 Low EQ"),
    44: ("Filter Right", "Channel 2 Filter"),
    19: ("Crossfader", "Master Crossfader"),
    51: ("Master Level", "Master Volume"),
    33: ("Tempo Fader Left", "Channel 1 Tempo"),
    64: ("Browse", "Browse/Track Selection"),
    100: ("Browse Shift", "Browse with Shift"),
}

MIDI_NOTES = {
    27: ("Hot Cue Pad 1 - Deck 1", "hot_cue"),
    30: ("Hot Cue Pad 2 - Deck 1", "hot_cue"),
    32: ("Hot Cue Pad 3 - Deck 1", "hot_cue"),
    34: ("Hot Cue Pad 4 - Deck 1", "hot_cue"),
    16: ("Beat Sync/Jump - Deck 1", "beat"),
    17: ("Beat Sync/Jump - Deck 2", "beat"),
    11: ("Cue - Deck 1", "cue"),
    12: ("Cue - Deck 2", "cue"),
    54: ("Jog Left Touch", "jog"),
}

# Controls that should be debounced (faders, knobs, encoders).
# Note messages (pads, buttons) are never debounced.
DEBOUNCE_CONTROLS = {19, 51, 10, 11, 17, 18, 27, 40, 41, 42, 43, 44, 33, 64, 100}
DEBOUNCE_MS = 50  # ms of silence before emitting a control change


def format_midi_event(msg, elapsed_time):
    base = f"[{elapsed_time:.2f}s]"

    if msg.type == "control_change":
        control_name = MIDI_CONTROLS.get(msg.control, f"CC {msg.control}")
        value_percent = round((msg.value / 127) * 100)
        return (
            f"{base} 🎚️  {control_name}\n"
            f"         ch={msg.channel} cc={msg.control} val={msg.value} ({value_percent}%)"
        )

    elif msg.type == "note_on":
        note_name = MIDI_NOTES.get(msg.note, f"Note {msg.note}")
        return (
            f"{base} 🔘 {note_name}\n"
            f"         ch={msg.channel} note={msg.note} vel={msg.velocity}"
        )

    elif msg.type == "note_off":
        note_name = MIDI_NOTES.get(msg.note, f"Note {msg.note}")
        return (
            f"{base} 🔘 {note_name} [OFF]\n"
            f"         ch={msg.channel} note={msg.note} vel={msg.velocity}"
        )

    elif msg.type == "pitchwheel":
        return (
            f"{base} 〰️  Pitch Wheel\n"
            f"         ch={msg.channel} pitch={msg.pitch} ({msg.pitch/8192*100:+.1f}%)"
        )

    elif msg.type == "sysex":
        hex_data = " ".join(f"{b:02X}" for b in msg.data)
        return (
            f"{base} 📦 SysEx\n"
            f"         data=[{hex_data}] ({len(msg.data)} bytes)"
        )

    else:
        # Catch-all: dump every attribute mido exposes on the message
        attrs = {k: getattr(msg, k) for k in vars(msg) if not k.startswith("_")}
        return f"{base} ❓ {msg.type}\n         {attrs}"

class MidiDebouncer:
    """
    Collapses rapid bursts of CC messages on the same control number
    into a single event after DEBOUNCE_MS of silence.

    Notes and other message types pass through immediately unchanged.
    """

    def __init__(self, debounce_ms=DEBOUNCE_MS, on_emit=None):
        self._delay = debounce_ms / 1000.0
        self._on_emit = on_emit  # callback(msg, elapsed_time)
        # Per-(channel, control) state: (timer, latest_msg, latest_time)
        self._timers: dict[tuple, threading.Timer] = {}
        self._latest: dict[tuple, tuple] = {}  # key -> (msg, elapsed_time)
        self._lock = threading.Lock()

    def feed(self, msg, elapsed_time):
        """Call with every incoming MIDI message."""
        if msg.type != "control_change" or msg.control not in DEBOUNCE_CONTROLS:
            # Buttons, pads, note on/off — emit immediately
            if self._on_emit:
                self._on_emit(msg, elapsed_time)
            return

        key = (msg.channel, msg.control)

        with self._lock:
            # Always keep the most recent value
            self._latest[key] = (msg, elapsed_time)

            # Cancel any pending timer and start a fresh one
            existing = self._timers.get(key)
            if existing:
                existing.cancel()

            t = threading.Timer(self._delay, self._fire, args=(key,))
            self._timers[key] = t
            t.start()

    def _fire(self, key):
        with self._lock:
            entry = self._latest.pop(key, None)
            self._timers.pop(key, None)
        if entry and self._on_emit:
            self._on_emit(*entry)

    def flush(self):
        """Force-emit all pending events immediately (call on stop)."""
        with self._lock:
            keys = list(self._timers.keys())
        for key in keys:
            with self._lock:
                t = self._timers.pop(key, None)
                entry = self._latest.pop(key, None)
            if t:
                t.cancel()
            if entry and self._on_emit:
                self._on_emit(*entry)


class SimpleCapture:
    def __init__(self):
        self.sr = 44100
        self.chunk_size = 2048
        self.audio_device = self._find_audio_device()
        self.midi_device = self._find_midi_device()
        self.audio_frames = []
        self.midi_events = []
        self.start_time = None
        self.last_meter_update = 0
        self._debouncer = MidiDebouncer(on_emit=self._record_event)

    def _record_event(self, msg, elapsed_time):
        """Receives debounced (or pass-through) events and stores + prints them."""
        event = {"time": elapsed_time, "type": msg.type, "message": str(msg)}
        self.midi_events.append(event)
        print(f"\n{format_midi_event(msg, elapsed_time)}")

    def _find_audio_device(self):
        devices = sd.query_devices()
        for i, device in enumerate(devices):
            if "BlackHole" in device["name"] and device["max_input_channels"] > 0:
                print(f"✓ Found recording bridge: {device['name']} (index {i})")
                return i
        for i, device in enumerate(devices):
            if ("DDJ" in device["name"] or "FLX4" in device["name"]) and device["max_input_channels"] > 0:
                print(f"✓ Found DDJ-FLX4 input: {device['name']} (index {i})")
                return i
        print("⚠️ Recording bridge not found, using system default input")
        return None

    def _find_midi_device(self):
        midi_inputs = mido.get_input_names()
        for name in midi_inputs:
            if "DDJ" in name or "FLX4" in name:
                print(f"✓ Found MIDI device: {name}")
                return name
        if midi_inputs:
            print(f"⚠️ DDJ-FLX4 MIDI not found, using: {midi_inputs[0]}")
            return midi_inputs[0]
        print("✗ No MIDI devices found!")
        return None

    def list_devices(self):
        print("\n=== AUDIO DEVICES ===")
        print(sd.query_devices())
        print("\n=== MIDI DEVICES ===")
        for i, name in enumerate(mido.get_input_names()):
            print(f"{i}: {name}")

    def start(self):
        if self.midi_device is None:
            print("Cannot start: no MIDI device found")
            return

        midi_input = mido.open_input(self.midi_device)
        self.start_time = time.time()

        print(f"\n✓ Recording Audio from: Index {self.audio_device}")
        print(f"✓ Monitoring MIDI from: {self.midi_device}")
        print(f"✓ Debounce window: {DEBOUNCE_MS}ms on CC messages")
        print("--- LOOK FOR THE VOLUME BAR BELOW ---")

        def audio_callback(indata, frames, time_info, status):
            if status:
                print(f"⚠️ {status}")
            self.audio_frames.append(indata.copy())
            if time.time() - self.last_meter_update > 0.1:
                volume_norm = np.linalg.norm(indata) * 10
                bar = "█" * min(int(volume_norm), 30)
                spacer = " " * (30 - len(bar))
                print(f"\rAUDIO LEVEL: [{bar}{spacer}]", end="", flush=True)
                self.last_meter_update = time.time()

        try:
            with sd.InputStream(
                device=self.audio_device,
                channels=2,
                samplerate=self.sr,
                blocksize=self.chunk_size,
                callback=audio_callback,
            ):
                while True:
                    for msg in midi_input.iter_pending():
                        elapsed = time.time() - self.start_time
                        self._debouncer.feed(msg, elapsed)
                    time.sleep(0.01)
        except KeyboardInterrupt:
            print("\n\n✋ Stopped")
            self._debouncer.flush()  # emit any events still waiting in the buffer
            midi_input.close()
            self.save()

    def save(self):
        if not self.audio_frames:
            print("No audio captured")
            return

        audio_array = np.concatenate(self.audio_frames)
        if np.max(np.abs(audio_array)) < 0.0001:
            print("⚠️ WARNING: Recorded audio is SILENT. Check Rekordbox Audio Settings.")

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        audio_file = f"capture_{timestamp}.wav"
        sf.write(audio_file, audio_array, self.sr)
        print(f"\n✓ Saved: {audio_file}")

        midi_file = f"capture_{timestamp}.json"
        with open(midi_file, "w") as f:
            json.dump(
                {
                    "duration_sec": len(audio_array) / self.sr,
                    "num_events": len(self.midi_events),
                    "events": self.midi_events,
                },
                f,
                indent=2,
            )
        print(f"✓ Saved: {midi_file}")
        print(f"\n{len(audio_array) / self.sr:.1f}s recorded, {len(self.midi_events)} MIDI events")


def main():
    capture = SimpleCapture()
    capture.list_devices()
    print("\n" + "=" * 50)
    input("Press Enter to start recording...")
    capture.start()


if __name__ == "__main__":
    main()