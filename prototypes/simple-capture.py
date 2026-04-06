import json
import time
import threading
import mido
import numpy as np
import sounddevice as sd
import soundfile as sf
from datetime import datetime
import argparse

MIDI_CONTROLS = {
    # Deck 1 (ch=0)
    (0, 11): ("Trim", "deck1_trim"),
    (0, 15): ("EQ High", "deck1_eq_high"),
    (0, 47): ("EQ Mid", "deck1_eq_mid"),
    (0, 23): ("EQ Low", "deck1_eq_low"),
    (0, 43): ("Filter", "deck1_filter"),
    (0, 33): ("Tempo", "deck1_tempo"),
    (0, 34): ("Tempo Fine", "deck1_tempo_fine"),
    # Deck 2 (ch=1)
    (1, 11): ("Trim", "deck2_trim"),
    (1, 15): ("EQ High", "deck2_eq_high"),
    (1, 47): ("EQ Mid", "deck2_eq_mid"),
    (1, 39): ("EQ Low", "deck2_eq_low"),
    (1, 43): ("Filter", "deck2_filter"),
    (1, 33): ("Tempo", "deck2_tempo"),
    (1, 34): ("Tempo Fine", "deck2_tempo_fine"),
    (1,  7): ("???", "deck2_???"),
    # Master (ch=0 shared)
    (0, 19): ("Crossfader", "master_crossfader"),
    (0, 51): ("Master Level", "master_volume"),
    # Effects/Mixer (ch=6)
    (6, 13): ("???", "fx_???"),
    (6, 23): ("???", "fx_???"),
    (6, 24): ("???", "fx_???"),
    (6, 45): ("???", "fx_???"),
    (6, 55): ("???", "fx_???"),
    (6, 56): ("???", "fx_???"),
    # ch=4, ch=5 — likely effects sends or loop controls
    (4,  2): ("???", "ch4_???"),
    (4, 34): ("???", "ch4_???"),
    (5,  2): ("???", "ch5_???"),
    (5, 34): ("???", "ch5_???"),
}

MIDI_NOTES = {
    (0, 11): ("Cue", "deck1_cue"),
    (1, 11): ("Cue", "deck2_cue"),
    (0, 54): ("Jog Touch", "deck1_jog"),
    (1, 54): ("Jog Touch", "deck2_jog"),
    (0, 77): ("???", "deck1_???"),
    (1, 77): ("???", "deck2_???"),
    (0, 18): ("???", "deck1_???"),
    (0, 19): ("???", "deck1_???"),
    (5, 71): ("???", "ch5_???"),
}

# Controls that should be debounced (faders, knobs, encoders).
# Note messages (pads, buttons) are never debounced.
DEBOUNCE_CONTROLS = {
    (0, 19), (0, 51), (0, 11), (0, 15), (0, 47), (0, 23), (0, 43), (0, 33), (0, 34),
    (1, 11), (1, 15), (1, 47), (1, 39), (1, 43), (1, 33), (1, 34), (1,  7),
    (6, 13), (6, 23), (6, 24), (6, 45), (6, 55), (6, 56),
    (4,  2), (4, 34), (5,  2), (5, 34),
}

DEBOUNCE_MS = 50  # ms of silence before emitting a control change


def format_midi_event(msg, elapsed_time):
    base = f"[{elapsed_time:.2f}s]"

    if msg.type == "control_change":
        control_name = MIDI_CONTROLS.get(msg.control, (f"CC {msg.control}", "unknown"))[0]
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
        if msg.type != "control_change" or (msg.channel, msg.control) not in DEBOUNCE_CONTROLS:
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

    def learn(self):
        if self.midi_device is None:
            print("Cannot start: no MIDI device found")
            return

        seen = {}  # (channel, type, control_or_note) -> first occurrence

        print("\n🎓 LEARN MODE — touch each control slowly, one at a time")
        print("   New controls will be printed. Known controls will be silent.")
        print("   Ctrl+C to stop and print summary.\n")

        midi_input = mido.open_input(self.midi_device)

        try:
            while True:
                for msg in midi_input.iter_pending():
                    if msg.type == "control_change":
                        key = ("cc", msg.channel, msg.control)
                        if key not in seen:
                            seen[key] = msg.value
                            print(f"  NEW CC  | ch={msg.channel:2d}  cc={msg.control:3d}  val={msg.value:3d}  → add to MIDI_CONTROLS")
                    elif msg.type in ("note_on", "note_off"):
                        key = ("note", msg.channel, msg.note)
                        if key not in seen:
                            seen[key] = msg.velocity
                            print(f"  NEW NOTE| ch={msg.channel:2d}  note={msg.note:3d}  vel={msg.velocity:3d}  → add to MIDI_NOTES")
                    elif msg.type == "pitchwheel":
                        key = ("pitch", msg.channel)
                        if key not in seen:
                            seen[key] = msg.pitch
                            print(f"  NEW PITCH| ch={msg.channel:2d}  pitch={msg.pitch}  → pitchwheel on this channel")
                    elif msg.type == "sysex":
                        key = ("sysex", bytes(msg.data[:4]))  # first 4 bytes as fingerprint
                        if key not in seen:
                            seen[key] = True
                            hex_data = " ".join(f"{b:02X}" for b in msg.data)
                            print(f"  NEW SYSEX| data=[{hex_data}]")
                time.sleep(0.01)

        except KeyboardInterrupt:
            midi_input.close()
            print(f"\n\n{'='*50}")
            print(f"LEARN SESSION SUMMARY — {len(seen)} unique controls found")
            print(f"{'='*50}\n")

            cc_entries = [(ch, ctrl, val) for (t, ch, ctrl), val in seen.items() if t == "cc"]
            note_entries = [(ch, note, vel) for (t, ch, note), vel in seen.items() if t == "note"]

            if cc_entries:
                print("# Paste into MIDI_CONTROLS:")
                for ch, ctrl, val in sorted(cc_entries, key=lambda x: (x[1], x[0])):
                    existing = MIDI_CONTROLS.get((ch, ctrl))
                    if existing:
                        print(f"    ({ch}, {ctrl:3d}): {existing},  # ✓ already mapped")
                    else:
                        print(f"    ({ch}, {ctrl:3d}): (\"???\", \"???\"),  # NEEDS NAME")

            if note_entries:
                print("\n# Paste into MIDI_NOTES:")
                for ch, note, vel in sorted(note_entries, key=lambda x: (x[1], x[0])):
                    existing = MIDI_NOTES.get((ch, note))
                    if existing:
                        print(f"    ({ch}, {note:3d}): {existing},  # ✓ already mapped")
                    else:
                        print(f"    ({ch}, {note:3d}): (\"???\", \"???\"),  # NEEDS NAME")

    def _record_event(self, msg, elapsed_time):
        event = self._serialize_msg(msg, elapsed_time)
        self.midi_events.append(event)
        print(f"\n{format_midi_event(msg, elapsed_time)}")

    def _serialize_msg(self, msg, elapsed_time):
        base = {
            "time": round(elapsed_time, 4),
            "type": msg.type,
            "channel": msg.channel,
        }

        if msg.type == "control_change":
            meta = MIDI_CONTROLS.get((msg.channel, msg.control), (f"CC {msg.control}", "unknown"))
            base.update({
                "control": msg.control,
                "control_name": meta[0],
                "control_category": meta[1],
                "value": msg.value,
                "value_normalized": round(msg.value / 127, 4),
            })

        elif msg.type in ("note_on", "note_off"):
            meta = MIDI_NOTES.get((msg.channel, msg.note), (f"Note {msg.note}", "unknown"))
            base.update({
                "note": msg.note,
                "note_name": meta[0],
                "note_category": meta[1],
                "velocity": msg.velocity,
            })

        elif msg.type == "pitchwheel":
            base.update({
                "pitch": msg.pitch,
                "pitch_normalized": round(msg.pitch / 8192, 4),
            })

        elif msg.type == "sysex":
            base.update({
                "data_hex": [f"{b:02X}" for b in msg.data],
                "data_len": len(msg.data),
            })

        return base

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
                    "schema_version": 1,
                    "duration_sec": round(len(audio_array) / self.sr, 3),
                    "event_count": len(self.midi_events),
                    "events": self.midi_events,
                },
                f,
                indent=2,
            )
        print(f"✓ Saved: {midi_file}")
        print(f"\n{len(audio_array) / self.sr:.1f}s recorded, {len(self.midi_events)} MIDI events")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--learn", action="store_true", help="Learn mode: print raw MIDI without interpretation")
    args = parser.parse_args()

    capture = SimpleCapture()
    capture.list_devices()
    print("\n" + "=" * 50)

    if args.learn:
        input("Press Enter to start learn mode (touch each control one at a time)...")
        capture.learn()
    else:
        input("Press Enter to start recording...")
        capture.start()

if __name__ == "__main__":
    main()