"""
HarmonEyes Theia SDK Example: Stasis eye camera

Streams gaze from a Stasis head-mounted eye camera into the SDK as it arrives,
with add_gaze_samples(), and every 10 recorded seconds prints one line with
cognitive load, fatigue, attention style, mental readiness and eye strain.

In your integration, call to_gaze_sample() and add_gaze_samples() from the
callback that hands you each frame. So you can try this without the camera,
the program replays a Stasis eye-coordinates export (the CSV the camera's
software writes) at the pace it was recorded, or faster.

From the export it reads utc_estimate_ns, eye, gaze_vector_x/y/z, gaze_valid
and pupil_valid. Every other column is ignored. The README's "Stasis eye
camera" section describes each of those fields.

Requirements
------------
  * Your HarmonEyes API key in THEIA_API_KEY_ID and THEIA_API_KEY_SECRET
    (see the README, "Device Lease").

Usage:
  python theia-stasis-streaming.py eye_coordinates.csv [speed]

speed replays faster than recorded: at 10, ten minutes of gaze take one.
"""

import csv
import sys
import time
import uuid
from collections import Counter
from dataclasses import dataclass

import harmoneyes_theia
from harmoneyes_theia import GazeSample

# How often the line is printed, in recorded seconds.
PRINT_EVERY_SECONDS = 10.0

COG_LOAD_LABELS = {0: "Low", 1: "Moderate", 2: "High"}
FATIGUE_LABELS = {0: "Alert", 1: "Somewhat Fatigued", 2: "Fatigued"}


@dataclass
class StasisFrame:
    """One frame of a Stasis export: the fields the SDK uses."""
    utc_estimate_ns: int   # capture time, nanoseconds since the Unix epoch
    right_eye: bool
    gaze_valid: bool
    gaze: tuple            # unit vector: +x right, +y down, +z forward
    pupil_valid: bool      # the camera found the pupil


def to_gaze_sample(frame: StasisFrame) -> GazeSample:
    """The sample add_gaze_samples() takes, for one Stasis frame.

    platform="Stasis" reads the timestamp, the tracked eye's gaze direction as
    the camera reports it (+y down), and that eye's status: 2 when the camera
    found the pupil, 0 when it lost it, which is how blinks are found. The
    other eye stays at its "not tracked" default. A frame with no valid gaze is
    still sent: its timestamp tells the SDK the eye was lost then, rather than
    that the stream paused.
    """
    s = GazeSample()
    s.timestamp_ns = frame.utc_estimate_ns
    status = 2 if frame.pupil_valid else 0
    if frame.right_eye:
        s.right_eye_status = status
        if frame.gaze_valid:
            s.optical_axis_right_x, s.optical_axis_right_y, s.optical_axis_right_z = frame.gaze
    else:
        s.left_eye_status = status
        if frame.gaze_valid:
            s.optical_axis_left_x, s.optical_axis_left_y, s.optical_axis_left_z = frame.gaze
    return s


def read_export(path: str) -> list:
    """The frames of a Stasis eye-coordinates export, in file order."""
    frames = []
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        needed = {"utc_estimate_ns", "eye", "gaze_vector_x", "gaze_vector_y", "gaze_vector_z",
                  "gaze_valid", "pupil_valid"}
        missing = needed - set(reader.fieldnames or [])
        if missing:
            sys.exit(f"{path} has no {', '.join(sorted(missing))} column")
        for row in reader:
            gaze = (row["gaze_vector_x"], row["gaze_vector_y"], row["gaze_vector_z"])
            gaze_valid = row["gaze_valid"] == "true" and all(gaze)
            frames.append(StasisFrame(
                utc_estimate_ns=int(row["utc_estimate_ns"]),
                right_eye=row["eye"] == "right",
                gaze_valid=gaze_valid,
                gaze=tuple(float(v) for v in gaze) if gaze_valid else (0.0, 0.0, 0.0),
                pupil_valid=row["pupil_valid"] == "true",
            ))
    if len(frames) < 2:
        sys.exit(f"{path} has no frames")
    return frames


def results_line(sdk, recorded_s: float, cog_load: str, fatigue: str) -> str:
    """Every result on one line. Cognitive load and fatigue are the newest the
    SDK handed over; attention style, mental readiness and eye strain are
    running states, read on the spot."""
    attention = sdk.get_attention_style()
    readiness = sdk.get_mental_readiness(recorded_s)
    strain = sdk.get_eye_strain()
    if strain is None:
        strain_text = "not yet"
    elif strain["in_break"]:
        strain_text = "on a break"
    else:
        strain_text = (f"{strain['score']:.0f}/100 "
                       f"({strain['on_seconds_since_break'] / 60:.1f} min since a break)")
    return (f"[{recorded_s:4.0f} s] Cognitive load: {cog_load} | Fatigue: {fatigue} | "
            f"Attention: {attention['label'] if attention else 'not yet'} | "
            f"Mental readiness: {readiness['label'] if readiness else 'not yet'} | "
            f"Eye strain: {strain_text}")


def main():
    if len(sys.argv) not in (2, 3):
        sys.exit(f"usage: {sys.argv[0]} eye_coordinates.csv [speed]")
    recording = read_export(sys.argv[1])
    speed = float(sys.argv[2]) if len(sys.argv) == 3 else 1.0
    if not speed > 0:
        sys.exit("speed must be above 0")

    try:
        # Each machine registers under a hash of its OS machine ID. Units
        # flashed from one disk image can share that ID, and containers usually
        # have none; give each its own with lease_device_identifier="<serial>".
        sdk = harmoneyes_theia.TheiaSDK(platform="Stasis")
    except harmoneyes_theia.LeaseVerificationError as e:
        sys.exit(
            f"[TheiaSDK] Lease rejected: {e}\n"
            "Set THEIA_API_KEY_ID and THEIA_API_KEY_SECRET to your HarmonEyes API key "
            "(see the README, \"Device Lease\")."
        )

    sdk.start_new_session(str(uuid.uuid4()))

    first_ns = recording[0].utc_estimate_ns
    total_s = (recording[-1].utc_estimate_ns - first_ns) / 1e9
    print(f"Streaming {total_s:.0f} s of gaze ({len(recording)} frames) at {speed:g}x")

    # get_cog_load_levels() and get_fatigue_level() hand over each result once,
    # so keep the newest for the printed line.
    cog_load_now, fatigue_now = "warming up", "not yet"
    cog_load_counts = Counter()
    start = time.monotonic()
    next_print_s = PRINT_EVERY_SECONDS
    try:
        # In place of the camera's frame callback: send each frame when it is due.
        for frame in recording:
            recorded_s = (frame.utc_estimate_ns - first_ns) / 1e9
            delay = start + recorded_s / speed - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            sdk.add_gaze_samples([to_gaze_sample(frame)])

            levels, _, _ = sdk.get_cog_load_levels()
            if levels:
                prediction = next(iter(levels.values()))["prediction"]
                cog_load_now = COG_LOAD_LABELS.get(prediction, str(prediction))
                cog_load_counts[cog_load_now] += 1
            fatigue, _ = sdk.get_fatigue_level()
            if fatigue:
                level = next(iter(fatigue.values()))
                fatigue_now = FATIGUE_LABELS.get(level, str(level))

            if recorded_s >= next_print_s:
                print(results_line(sdk, recorded_s, cog_load_now, fatigue_now), flush=True)
                next_print_s += PRINT_EVERY_SECONDS
    except KeyboardInterrupt:
        print("\n[TheiaSDK] Interrupted")
    finally:
        print(results_line(sdk, (recording[-1].utc_estimate_ns - first_ns) / 1e9,
                           cog_load_now, fatigue_now))
        strain = sdk.get_eye_strain()
        sdk.stop_processing()

    print("Cognitive load over the session: " +
          ", ".join(f"{label} {cog_load_counts[label]}" for label in COG_LOAD_LABELS.values()))
    if strain:
        print(f"Eye strain: {strain['on_seconds'] / 60:.1f} min of screen time, "
              f"{strain['break_count']} breaks, longest stretch "
              f"{strain['longest_on_seconds'] / 60:.1f} min")


if __name__ == "__main__":
    main()
