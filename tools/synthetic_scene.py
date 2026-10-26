"""Synthetic camera scene shared by the mock server and tools/track_test.py.

An orange ball moves and changes size, a green and a red ball act as distractors, and every
300 frames the orange ball is hidden for a while.
"""
import cv2
import numpy as np

HIDDEN = range(200, 230)  # frame numbers (modulo 300) in which the target is hidden
DIM = range(120, 160)  # frames (modulo 300) with a darker image


def render(index, size=(320, 240)):
    """Return (image, target) where target is (x, y, radius) or None while hidden."""
    width, height = size
    rng = np.random.default_rng(index)
    gradient = np.linspace(70, 40, width, dtype=np.float32)[None, :, None]
    image = np.repeat(gradient, height, 0).repeat(3, 2) * np.array([1.0, 0.8, 0.6], np.float32)
    image += rng.normal(0, 3, image.shape).astype(np.float32)
    image = np.clip(image, 0, 255).astype(np.uint8)

    step = index % 300
    radius = int(height / 8 * (1 + 0.3 * np.sin(index / 37)))
    x = width // 2 + int(width / 4 * np.sin(index / 10))
    y = height // 2 + int(height / 8 * np.sin(index / 23))
    green_x = int(width * 0.75 + width / 6 * np.sin(index / 13))
    cv2.circle(image, (green_x, height // 4), height // 9, (60, 180, 60), -1)  # green distractor
    red_x = int(width * 0.25 + width / 6 * np.sin(index / 17 + 1))
    cv2.circle(image, (red_x, height * 3 // 4), height // 10, (40, 40, 220), -1)  # red distractor
    target = None
    if step not in HIDDEN:
        cv2.circle(image, (x, y), radius, (30, 140, 255), -1)
        target = (x, y, radius)
    if step in DIM:
        image = (image * 0.7).astype(np.uint8)
    return image, target
