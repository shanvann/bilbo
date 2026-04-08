# Baby Bassinet State Detection Pipeline

Local ML pipeline for detecting baby sleep/wake state from an RTSP camera feed. Replaces cloud vision API calls with a 3-model cascade + temporal logic running entirely on-device.

## Architecture

```
RTSP Frame
    │
    ▼
┌──────────────────┐
│ 1. Baby Detector │  YOLOv8n on full frame → baby bounding box or ABSENT
│    (full frame)  │
└────────┬─────────┘
         │ baby crop (padded)
         ▼
┌──────────────────┐
│ 2. Face Detector │  YOLOv8n on baby crop → face bounding box or NOT VISIBLE
│   (baby crop)    │
└────────┬─────────┘
         │ face crop (padded)
         ▼
┌──────────────────┐
│ 3. Eye Classifier│  MobileNetV3-Small → eyes_open | eyes_closed | eyes_unclear
│   (face crop)    │
└────────┬─────────┘
         │
         ▼
┌──────────────────┐
│  Motion Scorer   │  OpenCV frame differencing on baby crop → motion score [0,1]
└────────┬─────────┘
         │
         ▼
┌──────────────────┐     ┌────────────────┐
│ Temporal Engine  │────▶│  Final State   │
│ (15s window)     │     │                │
│                  │     │  • awake       │
│  Aggregates:     │     │  • asleep      │
│  - eye states    │     │  • unknown     │
│  - motion scores │     │  • not_present │
│  - baby presence │     └────────────────┘
└──────────────────┘
```

## Final State Rules

| State | Condition |
|-------|-----------|
| `not_present` | Baby absent in ≥80% of window frames |
| `awake` | Eyes open in ≥40% of frames, OR high motion with unclear eyes |
| `asleep` | Eyes closed ≥60%, eyes open = 0%, motion below threshold (strict) |
| `unknown` | Everything else: face not visible, mixed signals, unclear eyes with low motion |

**Design priority:** Minimize awake→asleep errors (false sleep detection). The `asleep` state has zero tolerance for any `eyes_open` observations in the window.

## Setup

```bash
cd ~/.openclaw/workspace/skills/baby-monitor/pipeline
pip install -r requirements.txt
```

## Configuration

All thresholds are in `config.yaml`. Key sections:

- `baby_detector` / `face_detector` — YOLO confidence, IoU, box size filters
- `eye_classifier` — MobileNetV3 confidence threshold, class weights, augmentation
- `motion` — blur kernel, diff threshold, contour area
- `temporal` — window duration, state transition thresholds
- `training` — epochs, batch size, time-block split parameters
- `evaluation` — cost matrix (awake→asleep = 10x penalty)

## Training

### Label Formats

**Baby/Face detector** — JSON file mapping filenames to YOLO-format boxes:
```json
{
  "frame_001.jpg": [[0, 0.5, 0.5, 0.3, 0.4]],
  "frame_002.jpg": []
}
```
Each box: `[class_id, x_center, y_center, width, height]` (normalized 0-1).

**Eye classifier** — Either:
- JSON mapping filenames to labels: `{"crop_001.jpg": "eyes_open", ...}`
- Pre-organized directory: `data/{train,val,test}/{eyes_open,eyes_closed,eyes_unclear}/`

### Train Commands

```bash
# Train baby detector (YOLOv8n fine-tune)
python train.py baby --data data/baby_labels.json --frame-dir data/frames/ --output output

# Train face detector (YOLOv8n fine-tune)
python train.py face --data data/face_labels.json --frame-dir data/baby_crops/ --output output

# Train eye classifier (MobileNetV3-Small)
python train.py eyes --data data/eye_crops/ --labels data/eye_labels.json --output output

# Or with pre-split dataset:
python train.py eyes --data data/eye_dataset/ --output output
```

All splits use **time-block splitting** (default 30-min blocks) to prevent data leakage from neighboring frames.

## Inference

```bash
# Live RTSP stream
python infer.py rtsp --config config.yaml

# Single frame
python infer.py frame path/to/frame.jpg --annotate

# Batch of frames
python infer.py batch data/frames/ --glob "*.jpg" --annotate
```

Output: JSONL log at `output/detections.jsonl` with per-frame results and rolling state.

## Evaluation

```bash
# From existing predictions
python evaluate.py --predictions output/detections.jsonl --ground-truth data/ground_truth.jsonl

# Run inference + evaluate
python evaluate.py --frames data/frames/ --ground-truth data/ground_truth.jsonl

# JSON output
python evaluate.py --predictions output/detections.jsonl --ground-truth data/ground_truth.jsonl --json
```

### Ground Truth Format

JSONL with timestamp and state:
```json
{"timestamp": 1712345678, "state": "awake"}
{"timestamp": 1712345683, "state": "asleep"}
{"timestamp": 1712345700, "state": "not_present"}
```

### Evaluation Output

- Confusion matrix (rows=true, cols=predicted)
- Per-state precision, recall, F1
- Critical error analysis: awake→asleep miss rate
- Cost-weighted score (awake→asleep = 10x penalty)

## Night Vision Support

The pipeline auto-detects night vision / IR frames (low saturation heuristic) and applies CLAHE enhancement before detection. Configurable via `capture.night_vision` in `config.yaml`. The eye classifier training includes random grayscale augmentation to handle IR frames.

## File Structure

```
pipeline/
├── config.yaml              # All thresholds (edit this)
├── requirements.txt
├── train.py                 # Training CLI
├── infer.py                 # Inference CLI
├── evaluate.py              # Evaluation CLI
├── README.md
├── pipeline/
│   ├── __init__.py
│   ├── config.py            # YAML config loader
│   ├── capture.py           # RTSP capture + night vision
│   ├── baby_detector.py     # Stage 1: baby detection
│   ├── face_detector.py     # Stage 2: face detection
│   ├── eye_classifier.py    # Stage 3: eye classification
│   ├── motion.py            # Motion scoring
│   ├── temporal.py          # Temporal rule engine
│   ├── inference.py         # End-to-end pipeline
│   ├── dataset.py           # Dataset prep + splitting
│   └── evaluate.py          # Evaluation metrics
├── models/                  # Trained model weights (gitignored)
└── output/                  # Inference/eval output (gitignored)
```
