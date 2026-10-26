"""Learn an object from the camera image and track it.

ColorTracker follows an object by its colour. ObjectTracker is what the server uses: NanoTrack follows
any object from frame to frame, and for objects with colour a histogram check notices when the box
has drifted onto something else, and the colours find the object again after a loss.

Nothing here touches hardware, so it can be tested offline (tools/track_test.py,
tools/compare_trackers.py).
"""
import collections
import dataclasses
import os

import cv2
import numpy as np

H_BINS, S_BINS = 30, 32
MIN_SATURATION = 60  # grey and dark pixels have no reliable hue
MIN_VALUE = 40
MIN_COLOURFUL = 0.5  # share of the learn box that must be colourful for an object to count as coloured
MIN_DISTINCT = 0.25  # and this share of its colours must be rare around it: a warm tint makes everything colourful
WEAK_BINS = 0.3  # histogram bins below this share of the strongest one are dropped
RARE_BINS = 0.01  # colours that make up less of the learn region than this are noise
NOISE_FLOOR = 60  # back-projection values (0..255) below this are ignored
MIN_FILL = 0.25  # share of the box that must match the object, otherwise the track is bad
SIZE_JUMP = 2.5  # the box area may change by at most this factor between two frames
LOST_AFTER = 5  # consecutive bad frames before the object counts as lost

# Is the box still on the learned object? A coloured object is judged by its hue alone, weighted by
# saturation: brightness, exposure and white balance change a lot with the light and with how the object
# is held, hue much less. An object without colour is judged by the directions of its edges, which light
# does not change either. In tests with darker, brighter and warmer copies of the clips, the old
# colour-and-brightness histogram moved more on the right object than on a wrong one.
HUE_BINS = 18
SHAPE_BINS = 8
VERIFY_GOOD, VERIFY_BAD = 0.20, 0.50  # hue distance: fully trusted below, no confidence above
DRIFT_DIST = 0.45  # hue distance above which the box no longer shows the learned object
DRIFT_FRAMES = 3  # frames in a row beyond the limit before the object counts as lost
NANO_MIN_SCORE = 0.3
REDETECT_HUE = 0.40  # a blob must be this close in hue to be the object again
REDETECT_SHAPE = 0.26  # and this close in the directions of its edges: a round disc is not a square box
REDETECT_SIZE = 1.4  # and at most this factor taller or shorter than it was while it was tracked well
REDETECT_MARGIN = 0.1  # among those, the best look must beat the runner-up by this much, else it is ambiguous
REDETECT_FRAMES = 3  # frames in a row the same blob must be found before tracking resumes
# An object without colour: its first second after Learn is not checked, because the learn box is loose,
# so at the end of that second the reference is taken again from the (tight) tracked box. The limit then
# follows how much the object varies by itself during the next second, but stays between floor and ceiling.
SETTLE_FRAMES = 20
BASELINE_FRAMES = 20
SHAPE_FLOOR, SHAPE_CEIL, SHAPE_MARGIN = 0.25, 0.30, 0.08  # a wrong object scores 0.33 or more
PROFILE_VERSION = 2  # of the saved form of an object
MIN_BOX = 8
RETICLE = 0.3  # side of the learn box as a share of the image height
NANO_MODELS = ("nanotrack_backbone_sim.onnx", "nanotrack_head_sim.onnx")


@dataclasses.dataclass
class Track:
    state: str  # "idle", "tracking" or "lost"
    box: tuple | None = None  # x, y, w, h in pixels
    confidence: float = 0.0  # 0..1, how well the box matches the learned colours
    size: float = 0.0  # box height relative to the height at learn time
    reason: str = ""  # why the object is lost: "drift", "searching", "ambiguous", "track-only" or "lost"


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
        if (ratio * wanted).sum() / wanted.sum() < MIN_DISTINCT:
            raise ValueError("its colours are common around it")
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

    def probability(self, frame):
        """Back-projection of the learned colours onto frame (0..255 per pixel)."""
        hsv, mask = self._hsv(frame)
        return self._backproject(hsv, mask)

    def blobs(self, prob):
        """Boxes (x, y, w, h) of the regions that match the learned colours and are a plausible size."""
        _, binary = cv2.threshold(prob, 80, 255, cv2.THRESH_BINARY)
        binary = cv2.morphologyEx(binary, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        count, _, stats, _ = cv2.connectedComponentsWithStats(binary)
        ref_area = self._ref_height ** 2 * 0.5
        found = []
        for i in range(1, count):
            x, y, w, h, area = stats[i]
            if ref_area / 8 <= area <= ref_area * 8:
                found.append((int(area), (int(x), int(y), int(w), int(h))))
        return [box for _, box in sorted(found, reverse=True)]

    def _find(self, prob):
        """Look for the object in the whole frame: the largest blob that is a plausible size."""
        found = self.blobs(prob)
        return found[0] if found else None

    def object_box(self, frame, box):
        """The box of the largest learned-colour region inside box, which fits the object tighter."""
        x, y, w, h = box
        prob = self.probability(frame)
        inside = np.zeros_like(prob)
        inside[y:y + h, x:x + w] = prob[y:y + h, x:x + w]
        _, binary = cv2.threshold(inside, 80, 255, cv2.THRESH_BINARY)
        count, _, stats, _ = cv2.connectedComponentsWithStats(binary)
        if count < 2:
            return box
        i = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
        return tuple(int(v) for v in stats[i, :4])

    def restore(self, hist, ref_fill, ref_height):
        """Use a saved colour model; the tracker starts out lost and waits to find the object."""
        self._hist, self._ref_fill, self._ref_height = hist, ref_fill, float(ref_height)
        self._box, self._bad, self.state = None, 0, "lost"

    def resume(self, box):
        self._box, self._bad, self.state = box, 0, "tracking"


def _patch(frame, box):
    height, width = frame.shape[:2]
    x, y, w, h = box
    x, y = max(x, 0), max(y, 0)
    patch = frame[y:min(y + h, height), x:min(x + w, width)]
    return patch if patch.size else None


def _look_hist(frame, box):
    """Coarse hue/saturation/brightness histogram of the box: how alike two candidates look, not for lighting."""
    patch = _patch(frame, box)
    if patch is None:
        return None
    hist = cv2.calcHist([cv2.cvtColor(patch, cv2.COLOR_BGR2HSV)], [0, 1, 2], None, [8, 4, 8], [0, 180, 0, 256, 0, 256])
    return cv2.normalize(hist, None, 1, 0, cv2.NORM_L1).flatten().astype(np.float32)


def _hue_hist(frame, box):
    """Hue of the pixels in the box, each weighted by its saturation."""
    patch = _patch(frame, box)
    if patch is None:
        return None
    hsv = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV)
    weight = hsv[..., 1].astype(np.float32) / 255 * (hsv[..., 2] > MIN_VALUE)
    hist = np.bincount((hsv[..., 0] // (180 // HUE_BINS)).ravel(), weights=weight.ravel(), minlength=HUE_BINS)
    return (hist / (hist.sum() + 1e-6)).astype(np.float32)


def _shape_hist(frame, box):
    """Directions of the edges in the box, weighted by their strength."""
    patch = _patch(frame, box)
    if patch is None:
        return None
    gray = cv2.resize(cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY), (24, 24), interpolation=cv2.INTER_AREA).astype(np.float32)
    gx, gy = cv2.Sobel(gray, cv2.CV_32F, 1, 0), cv2.Sobel(gray, cv2.CV_32F, 0, 1)
    direction = (np.arctan2(gy, gx) % np.pi / np.pi * SHAPE_BINS).astype(int).clip(0, SHAPE_BINS - 1)
    hist = np.bincount(direction.ravel(), weights=cv2.magnitude(gx, gy).ravel(), minlength=SHAPE_BINS)
    return (hist / (hist.sum() + 1e-6)).astype(np.float32)


def _distance(a, b):
    return 1.0 if a is None or b is None else float(cv2.compareHist(a, b, cv2.HISTCMP_BHATTACHARYYA))


def _overlap(a, b):
    ix = max(0, min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1]))
    return ix * iy / float(a[2] * a[3] + b[2] * b[3] - ix * iy)


class ObjectTracker:
    """Tracks any learned object and notices when the box has left it.

    tier is "colour" for an object with colour: it is judged by hue, and found again by its colours
    after a loss. An object without colour (a plain grey one) gets tier "track-only": it is judged by
    the shape of its edges, which is less reliable, and it cannot be found again, so it has to be
    learned again after a loss. Without the NanoTrack models (models_dir) only objects with colour work.
    """

    def __init__(self, models_dir=None):
        self.models_dir = models_dir
        self._nano = None
        self.colour = ColorTracker()
        self.reset()

    def reset(self):
        self.colour.reset()
        self.state = "idle"
        self.tier = None
        self.reason = ""
        self._box = None
        self._reference = None  # hue histogram (colour) or edge-direction histogram (track-only) of the object
        self._shape = None  # edge-direction histogram at Learn, to tell look-alikes of the same colour apart
        self._look = None  # colour/brightness histogram, only to choose between candidates
        self._baseline = []  # distances during the second after the settling (track-only)
        self._limit = SHAPE_FLOOR
        self._frames = 0  # tracked frames since Learn
        self._ref_height = None
        self._heights = collections.deque(maxlen=45)  # box heights of the last frames that matched well
        self._height_rel = None  # reference height as a share of the frame height, for saved objects
        self._bad = self._drift = self._seen = 0
        self._candidate = None

    @property
    def learned(self):
        return self.state != "idle"

    def _nano_tracker(self):
        if self._nano is None and self.models_dir:
            paths = [os.path.join(self.models_dir, name) for name in NANO_MODELS]
            if all(os.path.exists(p) for p in paths):
                params = cv2.TrackerNano_Params()
                params.backbone, params.neckhead = paths
                self._nano = cv2.TrackerNano_create(params)
        return self._nano

    def _start(self, frame, box):
        nano = self._nano_tracker()
        if nano is not None:
            nano.init(frame, box)
        else:
            self.colour.resume(box)
        self._box = box
        self._bad = self._drift = self._seen = 0
        self._candidate = None
        self.reason = ""
        self.state = "tracking"

    def learn(self, frame, box):
        """Learn the object inside box (x, y, w, h) and start tracking it. Raises ValueError if it cannot."""
        height, width = frame.shape[:2]
        x, y = max(box[0], 0), max(box[1], 0)
        box = (x, y, min(box[2], width - x), min(box[3], height - y))
        if box[2] < MIN_BOX or box[3] < MIN_BOX:
            raise ValueError("the box is too small")
        try:
            self.colour.learn(frame, box)
            coloured = True
        except ValueError:
            coloured = False
        if not coloured and self._nano_tracker() is None:
            raise ValueError("the object has no colour and the NanoTrack models are not installed")
        self.tier = "colour" if coloured else "track-only"
        if coloured:
            box = self.colour.object_box(frame, box)
            self.colour._ref_height = float(box[3])
        self._ref_height = float(box[3])
        self._height_rel = self._ref_height / height
        self._reference = _hue_hist(frame, box) if coloured else _shape_hist(frame, box)
        self._look = _look_hist(frame, box)
        self._shape = _shape_hist(frame, box)
        self._baseline, self._limit, self._frames = [], SHAPE_FLOOR, 0
        self._heights.clear()
        self._start(frame, box)
        return Track("tracking", box, 1.0, 1.0)

    def export(self):
        """The saved form of a learned object with colour (see load)."""
        if self.tier != "colour":
            raise ValueError("only objects with colour can be saved")
        return {"version": PROFILE_VERSION, "hist": self.colour._hist, "ref_fill": self.colour._ref_fill,
                "hue": self._reference, "look": self._look, "shape": self._shape, "height_rel": self._height_rel}

    def load(self, profile):
        """Use a saved object; the tracker starts out lost and finds the object by its colours."""
        if int(profile.get("version", 0)) != PROFILE_VERSION:
            raise ValueError("this object was saved by an older version: learn it again")
        self.reset()
        self.tier = "colour"
        self._reference, self._look, self._shape = profile["hue"], profile["look"], profile["shape"]
        self._height_rel = float(profile["height_rel"])
        self.colour.restore(profile["hist"], float(profile["ref_fill"]), 1.0)
        self._lose("searching")

    def update(self, frame):
        """Track in the next frame and return a Track."""
        if self.state == "idle":
            return Track("idle")
        if self._ref_height is None:  # a loaded object: the size follows from the frame size
            self._ref_height = self._height_rel * frame.shape[0]
            self.colour._ref_height = self._ref_height
        return self._track(frame) if self.state == "tracking" else self._search(frame)

    def _lose(self, reason):
        self.state, self.reason = "lost", reason
        self._candidate, self._seen = None, 0
        return Track("lost", reason=reason)

    def _track(self, frame):
        nano = self._nano
        if nano is not None:
            ok, found = nano.update(frame)
            score = float(nano.getTrackingScore())
            box = tuple(int(v) for v in found) if ok else None
        else:
            track = self.colour.update(frame)
            box, score = (track.box, track.confidence) if track.state == "tracking" else (None, 0.0)
        if box is None or score < NANO_MIN_SCORE or box[2] < MIN_BOX or box[3] < MIN_BOX:
            self._bad += 1
            if self._bad >= LOST_AFTER:
                return self._lose("lost")
            return Track("tracking", self._box, 0.0, self._box[3] / self._ref_height)
        self._bad = 0
        self._box = box
        if self.tier == "colour":
            distance = _distance(self._reference, _hue_hist(frame, box))
            drifting = distance > DRIFT_DIST
            match = min(1.0, max(0.0, (VERIFY_BAD - distance) / (VERIFY_BAD - VERIFY_GOOD)))
        else:
            drifting, match = self._check_plain(frame, box)
        self._drift = self._drift + 1 if drifting else 0
        if self._drift >= DRIFT_FRAMES:
            return self._lose("drift")
        if match > 0.8:
            self._heights.append(box[3])
        return Track("tracking", box, min(score, 1.0) * match, box[3] / self._ref_height)

    def _check_plain(self, frame, box):
        """The drift check for an object without colour. Returns (drifting, how well it still matches 0..1)."""
        self._frames += 1
        if self._frames < SETTLE_FRAMES:
            return False, 1.0
        if self._frames == SETTLE_FRAMES:
            self._reference = _shape_hist(frame, box)
            return False, 1.0
        distance = _distance(self._reference, _shape_hist(frame, box))
        if len(self._baseline) < BASELINE_FRAMES:
            self._baseline.append(distance)
            if len(self._baseline) == BASELINE_FRAMES:
                self._limit = min(SHAPE_CEIL, max(SHAPE_FLOOR, max(self._baseline) + SHAPE_MARGIN))
        excess = distance / self._limit
        return excess > 1.0, min(1.0, max(0.0, (1.25 - excess) / 0.25))

    def _search(self, frame):
        """Lost: for an object with colour, look for it in the whole frame."""
        if self.tier != "colour":
            return Track("lost", reason="track-only")
        # Candidates must match in hue, in the shape of their edges and in size. Among them the one that
        # looks most like the object wins: that comparison is relative, so a change of light, which moves
        # all of them alike, does not matter.
        scored = []
        expected = float(np.median(self._heights)) if len(self._heights) >= 5 else self._ref_height
        for blob in self.colour.blobs(self.colour.probability(frame)):
            if (1 / REDETECT_SIZE <= blob[3] / expected <= REDETECT_SIZE
                    and _distance(self._reference, _hue_hist(frame, blob)) < REDETECT_HUE
                    and _distance(self._shape, _shape_hist(frame, blob)) < REDETECT_SHAPE):
                scored.append((_distance(self._look, _look_hist(frame, blob)), blob))
        scored.sort()
        if not scored:
            self.reason, self._candidate, self._seen = "searching", None, 0
        elif len(scored) > 1 and scored[1][0] - scored[0][0] < REDETECT_MARGIN:
            self.reason, self._candidate, self._seen = "ambiguous", None, 0
        else:
            box = scored[0][1]
            same = self._candidate is not None and _overlap(box, self._candidate) > 0.3
            self._seen = self._seen + 1 if same else 1
            self._candidate = box
            if self._seen >= REDETECT_FRAMES:
                self._start(frame, box)
                return Track("tracking", box, 1.0, box[3] / self._ref_height)
            self.reason = "searching"
        return Track("lost", reason=self.reason)


def reticle_box(width, height):
    """The box that Learn captures: in the middle, a fixed share of the image height."""
    side = int(height * RETICLE)
    return (width - side) // 2, (height - side) // 2, side, side


def draw_reticle(frame):
    """Draw corner marks where the object has to be held for Learn."""
    x, y, w, h = reticle_box(frame.shape[1], frame.shape[0])
    n = w // 4
    for cx, cy, dx, dy in ((x, y, 1, 1), (x + w, y, -1, 1), (x, y + h, 1, -1), (x + w, y + h, -1, -1)):
        cv2.line(frame, (cx, cy), (cx + dx * n, cy), (255, 255, 255), 2)
        cv2.line(frame, (cx, cy), (cx, cy + dy * n), (255, 255, 255), 2)


def draw(frame, track):
    """Draw the tracked box onto frame: green while it is trusted, orange while the match is weak."""
    if track.box:
        x, y, w, h = track.box
        weak = track.confidence < 0.35
        cv2.rectangle(frame, (x, y), (x + w, y + h), (0, 165, 255) if weak else (0, 255, 0), 2)
