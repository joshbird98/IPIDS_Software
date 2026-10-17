import cv2
import os
import sys

# --- CONFIGURATION CONSTANTS ---
VIDEO_FILE = "C:\\Users\\lenovo\\OneDrive - University of Surrey\\Documents\\interview_presentation\\rapid_cycling_test_ioniser.mp4"
OUTPUT_DIR = "./output_frames"
OUTPUT_PREFIX = "frame"

# Define timestamps in seconds.
TIMESTAMPS_SEC = [1.0, 4.9, 11.0, 11.9, 13.0, 13.5]


# -------------------------------

def extract_and_crop_frames(video_path, timestamps, out_dir, out_prefix):
    if not os.path.exists(out_dir):
        os.makedirs(out_dir)

    cap = cv2.VideoCapture(video_path)

    if not cap.isOpened():
        print(f"Error: Could not open video file '{video_path}'.")
        sys.exit(1)

    fps = cap.get(cv2.CAP_PROP_FPS)
    total_frames = cap.get(cv2.CAP_PROP_FRAME_COUNT)
    duration = total_frames / fps if fps > 0 else 0

    for i, t_sec in enumerate(timestamps):
        if t_sec > duration and duration > 0:
            print(f"Warning: Timestamp {t_sec}s exceeds video duration ({duration:.2f}s). Skipping.")
            continue

        cap.set(cv2.CAP_PROP_POS_MSEC, t_sec * 1000)

        ret, frame = cap.read()
        if ret:
            # Calculate upper left two-thirds dimensions
            height, width = frame.shape[:2]
            crop_height = int(height * (2 / 3))
            crop_width = int(width * (2 / 3))

            # Crop using NumPy slicing [y_start:y_end, x_start:x_end]
            cropped_frame = frame[0:crop_height, 0:crop_width]

            out_path = os.path.join(out_dir, f"{out_prefix}_{i + 1}_t{t_sec}s.png")
            cv2.imwrite(out_path, cropped_frame)
            print(f"Saved: {out_path}")
        else:
            print(
                f"Error: Could not read frame at timestamp {t_sec}s. Manual verification of video encoding may be required.")

    cap.release()


if __name__ == "__main__":
    extract_and_crop_frames(VIDEO_FILE, TIMESTAMPS_SEC, OUTPUT_DIR, OUTPUT_PREFIX)