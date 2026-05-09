#!/usr/bin/env python3
import numpy as np
import sounddevice as sd
import soundfile as sf
from datetime import datetime
import time

class SimpleAudioRecorder:
    def __init__(self):
        self.sr = 44100
        self.chunk_size = 2048
        self.audio_device = self._find_audio_device()
        self.audio_frames = []
        self.start_time = None
        self.last_meter_update = 0

    def _find_audio_device(self):
        devices = sd.query_devices()
        # Find BlackHole 2ch (input device)
        for i, device in enumerate(devices):
            if "BlackHole 2ch" in device["name"] and device["max_input_channels"] >= 2:
                print(f"✓ Found input device: {device['name']} (index {i})")
                return i
        print("✗ BlackHole 2ch input not found!")
        print("   Make sure your DJ Coach outputs to: Len DJ Coach (Mac)")
        print("   which routes to both speakers AND BlackHole")
        return None

    def list_devices(self):
        print("\n=== AUDIO DEVICES ===")
        print(sd.query_devices())

    def start(self):
        self.start_time = time.time()
        print(f"\n✓ Recording from: Index {self.audio_device}")
        print("--- VOLUME LEVEL BELOW ---\n")

        def audio_callback(indata, frames, time_info, status):
            if status:
                print(f"⚠️ {status}")
            self.audio_frames.append(indata.copy())
            if time.time() - self.last_meter_update > 0.1:
                volume_norm = np.linalg.norm(indata) * 10
                bar = "█" * min(int(volume_norm), 30)
                spacer = " " * (30 - len(bar))
                print(f"\rAUDIO: [{bar}{spacer}]", end="", flush=True)
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
                    time.sleep(0.01)
        except KeyboardInterrupt:
            print("\n\n✋ Stopped")
            self.save()

    def save(self):
        if not self.audio_frames:
            print("No audio captured")
            return

        audio_array = np.concatenate(self.audio_frames)
        if np.max(np.abs(audio_array)) < 0.0001:
            print("⚠️ WARNING: Recorded audio is SILENT")

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        audio_file = f"capture_{timestamp}.wav"
        sf.write(audio_file, audio_array, self.sr)

        duration = len(audio_array) / self.sr
        print(f"✓ Saved: {audio_file}")
        print(f"  Duration: {duration:.1f}s")


if __name__ == "__main__":
    recorder = SimpleAudioRecorder()
    recorder.list_devices()
    print("\n" + "=" * 50)
    input("Press Enter to start recording (Ctrl+C to stop)...")
    recorder.start()