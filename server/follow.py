"""Learn an object from its colour and track it in the camera image.

The tracker has no hardware dependencies, so it can be tested offline (tools/track_test.py).
"""
import dataclasses

import cv2
import numpy as np

H_BINS, S_BINS = 30, 32
MIN_SATURATION = 60  # grey and dark pixels have no reliable hue
MIN_VALUE = 40
MIN_COLOURFUL = 0.15  # share of the learn region that must be colourful
WEAK_BINS = 0.3  # histogram bins below this share of the strongest one are dropped
RARE_BINS = 0.01  # colours that make up less of the learn region than this are noise
NOISE_FLOOR = 60  # back-projection values (0..255) below this are ignored
MIN_FILL = 0.25  # share of the box that must match the object, otherwise the track is bad
SIZE_JUMP = 2.5  # the box area may change by at most this factor between two frames
LOST_AFTER = 5  # consecutive bad frames before the object counts as lost


@dataclasses.dataclass
class Track:
    state: str  # "idle", "tracking" or "lost"
    box: tuple | None = None  # x, y, w, h in pixels
    confidence: float = 0.0  # 0..1, how well the box matches the learned colours
    size: float = 0.0  # box height relative to the height at learn time


class ColorTracker:
    """CamShift on the back-projection of a hue/saturation histogram."""

    def __init__(self):
        self.reset()

    def reset(self):
        self._hist = None
        self._ref_height = 1.0
        self._ref_fill = 1.0
        self._box = None
        self._bad = 0
        self.state = "idle"

    @property
    def learned(self):
        return self._hist is not None

    @staticmethod
    def _hsv(frame):
        hsv = cv2.cvtColor(cv2.GaussianBlur(frame, (5, 5), 0), cv2.COLOR_BGR2HSV)
        mask = cv2.inRange(hsv, (0, MIN_SATURATION, MIN_VALUE), (180, 255, 255))
        return hsv, mask

    @staticmethod
    def _histogram(hsv, mask):
        return cv2.calcHist([hsv], [0, 1], mask, [H_BINS, S_BINS], [0, 180, 0, 256])

    def learn(self, frame, box):
        """Remember the colours inside box (x, y, w, h). Raises ValueError for a dull region."""
        x, y, w, h = box
        hsv, mask = self._hsv(frame)
        region = np.zeros(mask.shape, np.uint8)
        region[y:y + h, x:x + w] = 255
        inside = cv2.bitwise_and(mask, region)
        if cv2.countNonZero(inside) < MIN_COLOURFUL * w * h:
            raise ValueError("the object is not colourful enough")
        outside = cv2.bitwise_and(mask, cv2.bitwise_not(region))
        wanted = self._histogram(hsv, inside)
        around = self._histogram(hsv, outside)
        # Per colour: the share of all such pixels that lie inside the box. A colour that is just as
        # common around the object (the background) scores low and cannot tell it apart.
        ratio = wanted / (wanted + around + 1e-4)
        ratio[wanted < wanted.sum() * RARE_BINS] = 0
        ratio[ratio < ratio.max() * WEAK_BINS] = 0
        self._hist = cv2.normalize(ratio, None, 0, 255, cv2.NORM_MINMAX).astype(np.float32)
        self._ref_height = float(h)
        self._ref_fill = max(self._fill(self._backproject(hsv, mask), box), 0.05)
        self._box = box
        self._bad = 0
        self.state = "tracking"
        return self.update(frame)

    def _backproject(self, hsv, mask):
        prob = cv2.calcBackProject([hsv], [0, 1], self._hist, [0, 180, 0, 256], 1)
        prob = cv2.bitwise_and(prob, mask)
        prob[prob < NOISE_FLOOR] = 0  # a weak background response would let CamShift grow
        return prob

    @staticmethod
    def _fill(prob, box):
        x, y, w, h = box
        patch = prob[y:y + h, x:x + w]
        return float(patch.mean()) / 255 if patch.size else 0.0

    def update(self, frame):
        """Track in the next frame and return a Track."""
        if not self.learned:
            return Track("idle")
        hsv, mask = self._hsv(frame)
        prob = self._backproject(hsv, mask)
        box = self._follow(prob) if self.state == "tracking" else None
        if box is None:
            box = self._find(prob)
        if box is None:
            self._bad += 1
            if self._bad >= LOST_AFTER:
                self.state = "lost"
            confidence = 0.0
            shown = self._box if self.state == "tracking" else None
        else:
            confidence = min(self._fill(prob, box) / self._ref_fill, 1.0)
            if confidence < MIN_FILL / 2:
                self._bad += 1
                if self._bad >= LOST_AFTER:
                    self.state = "lost"
                shown = self._box if self.state == "tracking" else None
            else:
                self._bad = 0
                self.state = "tracking"
                self._box = shown = box
        if self.state != "tracking":
            return Track("lost")
        size = shown[3] / self._ref_height
        return Track("tracking", shown, confidence, size)

    def _follow(self, prob):
        """CamShift from the previous box. Returns the new box or None when it looks wrong."""
        height, width = prob.shape
        x, y, w, h = self._box
        window = (max(x, 0), max(y, 0), max(min(w, width - x), 2), max(min(h, height - y), 2))
        criteria = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 10, 1)
        try:
            rect, _ = cv2.CamShift(prob, window, criteria)
        except cv2.error:
            return None
        bx, by, bw, bh = cv2.boundingRect(cv2.boxPoints(rect).astype(np.int32))
        bx, by = max(bx, 0), max(by, 0)
        bw, bh = min(bw, width - bx), min(bh, height - by)
        if bw < 4 or bh < 4:
            return None
        ratio = (bw * bh) / float(w * h)
        if not 1 / SIZE_JUMP <= ratio <= SIZE_JUMP:
            return None
        return bx, by, bw, bh

    def _find(self, prob):
        """Look for the object in the whole frame: the largest blob that is a plausible size."""
        _, binary = cv2.threshold(prob, 80, 255, cv2.THRESH_BINARY)
        binary = cv2.morphologyEx(binary, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        count, _, stats, _ = cv2.connectedComponentsWithStats(binary)
        ref_area = self._ref_height ** 2 * 0.5
        best, best_area = None, 0
        for i in range(1, count):
            x, y, w, h, area = stats[i]
            if ref_area / 8 <= area <= ref_area * 8 and area > best_area:
                best, best_area = (int(x), int(y), int(w), int(h)), area
        return best


def draw(frame, track):
    """Draw the tracking result onto frame."""
    if track.box:
        x, y, w, h = track.box
        cv2.rectangle(frame, (x, y), (x + w, y + h), (0, 255, 0), 2)
    label = track.state if track.state != "tracking" else "%.0f%%" % (track.confidence * 100)
    cv2.putText(frame, label, (6, 16), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)
