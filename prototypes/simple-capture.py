import json
import time
import mido
import numpy as np
import sounddevice as sd
import soundfile as sf
from datetime import datetime

# Official Pioneer DDJ-FLX4 MIDI Control Mapping
MIDI_CONTROLS = {
    # MIXER Section (Channels 0-6 = B0-B6)
    # Deck 1 (Channel 0 = B0)
    10: ("Trim Left", "Channel 1 Trim"),
    17: ("EQ High Left", "Channel 1 High EQ"),
    18: ("EQ Mid Left", "Channel 1 Mid EQ"),
    27: ("EQ Low Left", "Channel 1 Low EQ"),
    43: ("Filter Left", "Channel 1 Filter"),
    # Deck 2 (Channel 1 = B1)
    11: ("Trim Right", "Channel 2 Trim"),
    40: ("EQ High Right", "Channel 2 High EQ"),
    41: ("EQ Mid Right", "Channel 2 Mid EQ"),
    42: ("EQ Low Right", "Channel 2 Low EQ"),
    44: ("Filter Right", "Channel 2 Filter"),
    # Master (Channel 6 = B6)
    19: ("Crossfader", "Master Crossfader"),
    51: ("Master Level", "Master Volume"),
    # TEMPO (Channel 0-1)
    # These are in spec as rotate controls
    33: ("Tempo Fader Left", "Channel 1 Tempo"),
    # BROWSE (Channel 6 = B6)
    64: ("Browse", "Browse/Track Selection"),
    100: ("Browse Shift", "Browse with Shift"),
}

# Note numbers for buttons/pads (from PERFORMANCE PADS section)
MIDI_NOTES = {
    # HOT CUE MODE - Deck 1
    27: ("Hot Cue Pad 1 - Deck 1", "hot_cue"),
    30: ("Hot Cue Pad 2 - Deck 1", "hot_cue"),
    32: ("Hot Cue Pad 3 - Deck 1", "hot_cue"),
    34: ("Hot Cue Pad 4 - Deck 1", "hot_cue"),
    # HOT CUE MODE - Deck 2
    # (Same notes, different MIDI channel 1)
    # BEAT JUMP/BEAT SYNC
    16: ("Beat Sync/Jump - Deck 1", "beat"),
    17: ("Beat Sync/Jump - Deck 2", "beat"),
    # CUE buttons
    11: ("Cue - Deck 1", "cue"),
    12: ("Cue - Deck 2", "cue"),
    # PLAY/PAUSE buttons
    # NOTE 11 & 14 (with shift)
    # JOG DIAL (also sends notes)
    54: ("Jog Left Touch", "jog"),
    # 103: ("Jog Left Scratch", "jog"),
}


def format_midi_event(msg, elapsed_time):
    """Pretty print a MIDI message"""
    if msg.type == "control_change":
        control_name = MIDI_CONTROLS.get(msg.control, f"CC {msg.control}")
        value_percent = round((msg.value / 127) * 100)
        return f"[{elapsed_time:.2f}s] 🎚️  {control_name}: {msg.value}/127 ({value_percent}%)"

    elif msg.type == "note_on":
        note_name = MIDI_NOTES.get(msg.note, f"Note {msg.note}")
        return (
            f"[{elapsed_time:.2f}s] 🔘 {note_name} (Ch {msg.channel}): ON (velocity {msg.velocity})"
        )

    elif msg.type == "note_off":
        note_name = MIDI_NOTES.get(msg.note, f"Note {msg.note}")
        return f"[{elapsed_time:.2f}s] 🔘 {note_name} (Ch {msg.channel}): OFF"

    else:
        return f"[{elapsed_time:.2f}s] {msg.type}: {msg}"


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

    def _find_audio_device(self):
        """Find the virtual bridge (BlackHole) for recording"""
        devices = sd.query_devices()

        # 1. Look for BlackHole first (this is our bridge from Rekordbox)
        for i, device in enumerate(devices):
            if "BlackHole" in device["name"] and device["max_input_channels"] > 0:
                print(f"✓ Found recording bridge: {device['name']} (index {i})")
                return i

        # 2. Fallback to FLX4 (though this usually only works if PC Master Out is on)
        for i, device in enumerate(devices):
            if ("DDJ" in device["name"] or "FLX4" in device["name"]) and device["max_input_channels"] > 0:
                print(f"✓ Found DDJ-FLX4 input: {device['name']} (index {i})")
                return i

        print("⚠️ Recording bridge not found, using system default input")
        return None # sounddevice will use default if None

    def _find_midi_device(self):
        """Find DDJ-FLX4 MIDI device"""
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
        """Show available audio and MIDI devices"""
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
        print("--- LOOK FOR THE VOLUME BAR BELOW ---")

        def audio_callback(indata, frames, time_info, status):
            if status:
                print(f"⚠️ {status}")

            # Store audio
            self.audio_frames.append(indata.copy())

            # --- VOLUME METER LOGIC ---
            # Update meter every 0.1 seconds to avoid flickering
            if time.time() - self.last_meter_update > 0.1:
                volume_norm = np.linalg.norm(indata) * 10
                magnitude = int(volume_norm)
                # Visual Bar: [||||      ]
                bar = "█" * min(magnitude, 30)
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
                        event = {"time": elapsed, "type": msg.type, "message": str(msg)}
                        self.midi_events.append(event)
                        # Use \n to move past the volume meter line
                        print(f"\n{format_midi_event(msg, elapsed)}")
                    time.sleep(0.01)
        except KeyboardInterrupt:
            print("\n\n✋ Stopped")
            midi_input.close()
            self.save()

    def save(self):
        """Save audio + MIDI to files"""
        if not self.audio_frames:
            print("No audio captured")
            return

        # ... (Same as your previous save function) ...
        # Add a check: if max volume in audio_array is 0, warn the user
        audio_array = np.concatenate(self.audio_frames)
        if np.max(np.abs(audio_array)) < 0.0001:
            print("⚠️ WARNING: Recorded audio is SILENT. Check Rekordbox Audio Settings.")

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        audio_array = np.concatenate(self.audio_frames)

        # Save audio
        audio_file = f"capture_{timestamp}.wav"
        sf.write(audio_file, audio_array, self.sr)
        print(f"\n✓ Saved: {audio_file}")

        # Save MIDI
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
