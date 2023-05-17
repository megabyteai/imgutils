"""
Overview:
    Detect targets from the given anime image.

    For example, you can detect the heads with :func:`imgutils.head.detect_heads` and visualize it
    with :func:`imgutils.visual.detection_visualize` like this

    .. image:: head_detect.dat.svg
        :align: center
"""
from .head import detect_heads
from .person import detect_person
from .visual import detection_visualize
