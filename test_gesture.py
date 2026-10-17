import os
from ultralytics import YOLO
import cv2

# Define model path relative to project root
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MODELS_DIR = os.path.join(BASE_DIR, "models")
os.makedirs(MODELS_DIR, exist_ok=True)

MODEL_PATH = os.path.join(MODELS_DIR, "best.pt")

# Load the best trained model
model = YOLO(MODEL_PATH)

# Open webcam
cap = cv2.VideoCapture(0)

if not cap.isOpened():
    print("Could not open webcam")
    exit()

print("Webcam started.")
print("Show FIST, OPEN_PALM, or SOS.")
print("Press Q to quit.")

while True:
    ret, frame = cap.read()

    if not ret:
        print("Could not read webcam frame")
        break

    # Predict gesture
    results = model(frame, verbose=False)

    # Get detection result
    result = results[0]

    if result.boxes is not None:
        for box in result.boxes:
            # Extract bounding box coordinates, confidence score, and class ID
            x1, y1, x2, y2 = map(int, box.xyxy.cpu().numpy()[0])
            confidence = float(box.conf.cpu().numpy()[0])
            class_id = int(box.cls.cpu().numpy()[0])
            class_name = result.names.get(class_id, str(class_id))

            # Draw bounding box
            cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 0), 2)

            # Draw class label and confidence score
            text = f"{class_name}: {confidence:.2f}"
            label_y = y1 - 10 if y1 - 10 > 10 else y1 + 20
            cv2.putText(
                frame,
                text,
                (x1, label_y),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.6,
                (0, 255, 0),
                2
            )

    cv2.imshow("Gesture Detection", frame)

    if cv2.waitKey(1) & 0xFF == ord("q"):
        break

cap.release()
cv2.destroyAllWindows()