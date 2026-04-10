# Plan: YuNet Face Detection Stage + Dashboard Face Correction

## Pipeline architecture

```
Current:  frame -> bassinet crop -> presence -> bassinet crop -> eye-state
                                                                 (224x224, eyes are tiny)

New:      frame -> bassinet crop -> presence -> face detect -> face crop -> eye-state
                                                 | (no face)     (224x224, face fills frame)
                                                 v
                                                 cloud API fallback
```

## File-by-file changes

### 1. `scripts/lib/config.py` -- New constants
- `FACE_DETECT_MODEL` -- path to `pipeline/models/face_detection_yunet_2023mar.onnx`
- `FACE_DETECT_SCORE_THRESHOLD = 0.5`
- `FACE_DETECT_NMS_THRESHOLD = 0.3`
- `FACE_CROP_PADDING = 0.3` -- expand detected bbox by 30% on each side

### 2. `scripts/lib/classifiers.py` -- New classes + helper
- `FaceDetectResult` dataclass: `bbox`, `confidence`, `normalized_bbox`
- `FaceDetector` class: wraps `cv2.FaceDetectorYN`, `detect(bassinet_crop) -> FaceDetectResult | None`
- `crop_face(bassinet_crop, bbox, padding)` helper

### 3. `scripts/lib/local_pipeline.py` -- Wire face detection
- Load FaceDetector as third singleton
- After presence=True: face detect -> crop_face -> eye-state
- No face -> cloud API fallback
- Store faceBbox, faceConfidence in entry

### 4. `scripts/train_classifiers.py` -- Update EyeStateDataset
- At init: run face detection per frame, cache bboxes
- Prefer faceBboxCorrected (dashboard) over auto-detect
- Skip frames with no face bbox

### 5. `dashboard/app.py` -- Backend
- Timeline API: pass faceBbox, faceConfidence, faceBboxCorrected
- `/api/update-entry`: accept faceBbox corrections

### 6. `dashboard/static/app.js` -- Frame viewer
- Face overlay: blue (auto) / green (corrected)
- Drag-to-draw interaction for face bbox correction
- Clear face button
- Fall back to head overlay for old entries

### 7. `dashboard/static/index.html` -- Minor
- Replace head overlay div with face overlay
- Add Clear face button

### 8. `dashboard/static/style.css` -- Styles
- .face-overlay, .face-overlay.corrected, cursor styles

## Timing budget

| Stage            | Time   |
|------------------|--------|
| Load frame       | ~5ms   |
| Bassinet crop    | ~0ms   |
| Presence         | ~60ms  |
| Face detection   | ~10ms  |
| Face crop        | ~0ms   |
| Eye-state        | ~60ms  |
| **Total**        | ~135ms |

## Risk: IR/night-vision
1. Test on 20 frames (day + night) first
2. If poor: lower threshold to 0.3
3. If still poor: Haar cascade fallback

## Implementation sequence
1. Config constants
2. FaceDetector + crop_face
3. Quick test on 20 frames (day + night)
4. Wire into local_pipeline.py
5. Update training pipeline
6. Dashboard backend
7. Dashboard frontend (overlay + drag-to-draw)
8. Retrain, compare metrics
9. Commit
