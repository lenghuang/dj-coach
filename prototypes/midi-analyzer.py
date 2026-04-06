# midi-analyzer.py
# uv run python midi-analyzer.py capture_20260402_011704.json
import json
import sys
from collections import defaultdict

def load_capture(path):
    with open(path) as f:
        return json.load(f)

def build_timeseries(events):
    """
    Group events by (channel, control/note) into per-control time series.
    Returns dict: control_key -> [(time, value_normalized), ...]
    """
    series = defaultdict(list)
    for e in events:
        if e["type"] == "control_change":
            key = (e["channel"], "cc", e["control"], e.get("control_name", "?"))
            series[key].append((e["time"], e["value_normalized"]))
        elif e["type"] == "note_on":
            key = (e["channel"], "note", e["note"], e.get("note_name", "?"))
            series[key].append((e["time"], e["velocity"] / 127))
    return dict(series)

def detect_gestures(timeseries, gap_threshold=0.5, min_delta=0.05):
    """
    Split each control's time series into discrete gestures.
    A gesture ends when there's a gap > gap_threshold seconds,
    or the control stops moving (delta < min_delta across the gesture).

    Returns: list of gesture dicts
    """
    gestures = []
    for (ch, kind, control_id, name), points in timeseries.items():
        if len(points) < 2:
            continue

        # Split into runs separated by time gaps
        runs = []
        current_run = [points[0]]
        for i in range(1, len(points)):
            if points[i][0] - points[i-1][0] > gap_threshold:
                runs.append(current_run)
                current_run = [points[i]]
            else:
                current_run.append(points[i])
        runs.append(current_run)

        for run in runs:
            if len(run) < 2:
                continue
            times = [p[0] for p in run]
            values = [p[1] for p in run]
            start_val = values[0]
            end_val = values[-1]
            delta = end_val - start_val

            if abs(delta) < min_delta:
                continue  # noise, not a real gesture

            gestures.append({
                "control": name,
                "channel": ch,
                "control_id": control_id,
                "kind": kind,
                "t_start": round(times[0], 3),
                "t_end": round(times[-1], 3),
                "duration": round(times[-1] - times[0], 3),
                "val_start": round(start_val, 3),
                "val_end": round(end_val, 3),
                "delta": round(delta, 3),
                "direction": "up" if delta > 0 else "down",
                "n_points": len(run),
            })

    return sorted(gestures, key=lambda g: g["t_start"])

def print_gestures(gestures):
    print(f"\n{'='*60}")
    print(f"GESTURES ({len(gestures)} detected)")
    print(f"{'='*60}\n")
    for g in gestures:
        arrow = "↑" if g["direction"] == "up" else "↓"
        print(
            f"[{g['t_start']:6.2f}s → {g['t_end']:6.2f}s] "
            f"{arrow} {g['control']:<22} "
            f"ch={g['channel']} "
            f"{g['val_start']:.2f} → {g['val_end']:.2f}  "
            f"(Δ{g['delta']:+.2f}, {g['duration']:.2f}s)"
        )

if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "capture.json"
    data = load_capture(path)
    print(f"Loaded: {data['duration_sec']}s, {data['event_count']} events")

    series = build_timeseries(data["events"])
    gestures = detect_gestures(series)
    print_gestures(gestures)