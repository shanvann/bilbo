# Baby Monitor Vision Prompt

Analyze this baby bassinet image and return ONLY JSON that exactly matches the required schema. Use only high-confidence visual observations. Never guess. If uncertain, use "Unknown" when available. Every observedValue must exactly match one of the listed possibleValues. The image may be normal or night vision. If a visible timestamp overlay exists in the image, especially near the top-left corner, extract it exactly and normalize it to ISO 8601 only when the full date and time are unambiguous. Do not include any text outside JSON.

Schema:
{
  "schemaVersion": "1.1",
  "imageType": "baby_bassinet_check",
  "imageContext": {
    "captureMode": "Normal|NightVision|Unknown",
    "timestampVisible": "Yes|No",
    "timestampText": "string|null",
    "inferredDateTime": "string|null"
  },
  "attributes": [
    {
      "category": "Sleep Safety",
      "attribute": "isBabyPresent",
      "observedValue": "Yes|No",
      "possibleValues": ["Yes", "No"]
    },
    {
      "category": "Sleep Safety",
      "attribute": "Sleep Position",
      "observedValue": "Back|Side|Stomach|Unknown",
      "possibleValues": ["Back", "Side", "Stomach", "Unknown"]
    },
    {
      "category": "Sleep Safety",
      "attribute": "Objects in bassinet",
      "observedValue": "None|Pacifier|Blanket|Pillow|Toys|Mixed|Unknown",
      "possibleValues": ["None", "Pacifier", "Blanket", "Pillow", "Toys", "Mixed", "Unknown"]
    },
    {
      "category": "Sleep Safety",
      "attribute": "Swaddle presence",
      "observedValue": "None|Partial|Full|Unknown",
      "possibleValues": ["None", "Partial", "Full", "Unknown"]
    },
    {
      "category": "Comfort",
      "attribute": "Head covering",
      "observedValue": "Hat|No hat|Unknown",
      "possibleValues": ["Hat", "No hat", "Unknown"]
    },
    {
      "category": "Comfort",
      "attribute": "Lighting",
      "observedValue": "Dark|Dim|Moderate|Bright|Unknown",
      "possibleValues": ["Dark", "Dim", "Moderate", "Bright", "Unknown"]
    },
    {
      "category": "Baby State",
      "attribute": "Awake vs asleep",
      "observedValue": "Asleep|Drowsy|Awake|Unknown",
      "possibleValues": ["Asleep", "Drowsy", "Awake", "Unknown"]
    },
    {
      "category": "Baby State",
      "attribute": "Body posture",
      "observedValue": "Relaxed|Tense|Startle reflex|Unknown",
      "possibleValues": ["Relaxed", "Tense", "Startle reflex", "Unknown"]
    },
    {
      "category": "Baby State",
      "attribute": "isPacifierEngaged",
      "observedValue": "Yes|No|Unknown",
      "possibleValues": ["Yes", "No", "Unknown"]
    },
    {
      "category": "Sleep Safety",
      "attribute": "Baby location in bassinet",
      "observedValue": "Center|Near edge|Pressed against side|Unknown",
      "possibleValues": ["Center", "Near edge", "Pressed against side", "Unknown"]
    },
    {
      "category": "Environment",
      "attribute": "Bassinet condition",
      "observedValue": "Clean|Wrinkled|Loose|Unknown",
      "possibleValues": ["Clean", "Wrinkled", "Loose", "Unknown"]
    },
    {
      "category": "Environment",
      "attribute": "External hazards (inside bassinet)",
      "observedValue": "None|Loose items|Cords nearby|Unknown",
      "possibleValues": ["None", "Loose items", "Cords nearby", "Unknown"]
    }
  ]
}

Rules:
- If no baby is visible:
  - isBabyPresent = "No"
  - Sleep Position = "Unknown"
  - Swaddle presence = "Unknown"
  - Head covering = "Unknown"
  - Awake vs asleep = "Unknown"
  - Body posture = "Unknown"
  - isPacifierEngaged = "Unknown"
  - Baby location in bassinet = "Unknown"
- For captureMode:
  - "NightVision" only when the image clearly appears to use infrared/night-vision style capture
  - otherwise "Normal"
  - use "Unknown" only when unclear
- For timestamp extraction:
  - timestampVisible = "Yes" only if timestamp text is visibly present in the image
  - timestampText must copy the visible text exactly when readable
  - inferredDateTime must be ISO 8601 only when the full date and time are reliably readable
  - otherwise inferredDateTime = null
- For isPacifierEngaged:
  - "Yes" only if pacifier is clearly in the mouth
  - "No" if pacifier is absent or visible but not in the mouth
  - "Unknown" if unclear
- Return attributes in the exact order shown in the schema.
- No extra keys.
- No markdown.
- No commentary.
