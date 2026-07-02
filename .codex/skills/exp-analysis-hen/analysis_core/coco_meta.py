"""
COCO-80 taxonomy utilities for semantic understanding in analysis.

This module provides:
- Standard COCO category IDs (with gaps) and names (80 classes)
- Mapping between internal contiguous class indices [0..79] and COCO category IDs
- Fold-aware mapping from eval-log `cat_idx` (episode-local index) to class IDs

Notes:
- In this repo, datasets typically use contiguous class indices [0..79] internally.
- COCO "standard IDs" refer to the official category_id values used in COCO annotations,
  which are not contiguous (e.g., skip 12, 26, 29, 30, ...).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CocoCategory:
    internal_id: int  # contiguous [0..79]
    coco_id: int      # official COCO category_id (non-contiguous)
    name: str
    supercategory: str


# Standard COCO 80 categories (instance segmentation), in the conventional order.
# internal_id is the contiguous index in this list.
COCO80_CATEGORIES: list[CocoCategory] = [
    CocoCategory(0, 1, "person", "person"),
    CocoCategory(1, 2, "bicycle", "vehicle"),
    CocoCategory(2, 3, "car", "vehicle"),
    CocoCategory(3, 4, "motorcycle", "vehicle"),
    CocoCategory(4, 5, "airplane", "vehicle"),
    CocoCategory(5, 6, "bus", "vehicle"),
    CocoCategory(6, 7, "train", "vehicle"),
    CocoCategory(7, 8, "truck", "vehicle"),
    CocoCategory(8, 9, "boat", "vehicle"),
    CocoCategory(9, 10, "traffic light", "outdoor"),
    CocoCategory(10, 11, "fire hydrant", "outdoor"),
    CocoCategory(11, 13, "stop sign", "outdoor"),
    CocoCategory(12, 14, "parking meter", "outdoor"),
    CocoCategory(13, 15, "bench", "outdoor"),
    CocoCategory(14, 16, "bird", "animal"),
    CocoCategory(15, 17, "cat", "animal"),
    CocoCategory(16, 18, "dog", "animal"),
    CocoCategory(17, 19, "horse", "animal"),
    CocoCategory(18, 20, "sheep", "animal"),
    CocoCategory(19, 21, "cow", "animal"),
    CocoCategory(20, 22, "elephant", "animal"),
    CocoCategory(21, 23, "bear", "animal"),
    CocoCategory(22, 24, "zebra", "animal"),
    CocoCategory(23, 25, "giraffe", "animal"),
    CocoCategory(24, 27, "backpack", "accessory"),
    CocoCategory(25, 28, "umbrella", "accessory"),
    CocoCategory(26, 31, "handbag", "accessory"),
    CocoCategory(27, 32, "tie", "accessory"),
    CocoCategory(28, 33, "suitcase", "accessory"),
    CocoCategory(29, 34, "frisbee", "sports"),
    CocoCategory(30, 35, "skis", "sports"),
    CocoCategory(31, 36, "snowboard", "sports"),
    CocoCategory(32, 37, "sports ball", "sports"),
    CocoCategory(33, 38, "kite", "sports"),
    CocoCategory(34, 39, "baseball bat", "sports"),
    CocoCategory(35, 40, "baseball glove", "sports"),
    CocoCategory(36, 41, "skateboard", "sports"),
    CocoCategory(37, 42, "surfboard", "sports"),
    CocoCategory(38, 43, "tennis racket", "sports"),
    CocoCategory(39, 44, "bottle", "kitchen"),
    CocoCategory(40, 46, "wine glass", "kitchen"),
    CocoCategory(41, 47, "cup", "kitchen"),
    CocoCategory(42, 48, "fork", "kitchen"),
    CocoCategory(43, 49, "knife", "kitchen"),
    CocoCategory(44, 50, "spoon", "kitchen"),
    CocoCategory(45, 51, "bowl", "kitchen"),
    CocoCategory(46, 52, "banana", "food"),
    CocoCategory(47, 53, "apple", "food"),
    CocoCategory(48, 54, "sandwich", "food"),
    CocoCategory(49, 55, "orange", "food"),
    CocoCategory(50, 56, "broccoli", "food"),
    CocoCategory(51, 57, "carrot", "food"),
    CocoCategory(52, 58, "hot dog", "food"),
    CocoCategory(53, 59, "pizza", "food"),
    CocoCategory(54, 60, "donut", "food"),
    CocoCategory(55, 61, "cake", "food"),
    CocoCategory(56, 62, "chair", "furniture"),
    CocoCategory(57, 63, "couch", "furniture"),
    CocoCategory(58, 64, "potted plant", "furniture"),
    CocoCategory(59, 65, "bed", "furniture"),
    CocoCategory(60, 67, "dining table", "furniture"),
    CocoCategory(61, 70, "toilet", "furniture"),
    CocoCategory(62, 72, "tv", "electronic"),
    CocoCategory(63, 73, "laptop", "electronic"),
    CocoCategory(64, 74, "mouse", "electronic"),
    CocoCategory(65, 75, "remote", "electronic"),
    CocoCategory(66, 76, "keyboard", "electronic"),
    CocoCategory(67, 77, "cell phone", "electronic"),
    CocoCategory(68, 78, "microwave", "appliance"),
    CocoCategory(69, 79, "oven", "appliance"),
    CocoCategory(70, 80, "toaster", "appliance"),
    CocoCategory(71, 81, "sink", "appliance"),
    CocoCategory(72, 82, "refrigerator", "appliance"),
    CocoCategory(73, 84, "book", "indoor"),
    CocoCategory(74, 85, "clock", "indoor"),
    CocoCategory(75, 86, "vase", "indoor"),
    CocoCategory(76, 87, "scissors", "indoor"),
    CocoCategory(77, 88, "teddy bear", "indoor"),
    CocoCategory(78, 89, "hair drier", "indoor"),
    CocoCategory(79, 90, "toothbrush", "indoor"),
]


COCO_INTERNAL_ID_TO_COCO_ID: list[int] = [c.coco_id for c in COCO80_CATEGORIES]
COCO_INTERNAL_ID_TO_NAME: list[str] = [c.name for c in COCO80_CATEGORIES]
COCO_INTERNAL_ID_TO_SUPERCATEGORY: list[str] = [c.supercategory for c in COCO80_CATEGORIES]

COCO_ID_TO_NAME: dict[int, str] = {c.coco_id: c.name for c in COCO80_CATEGORIES}
COCO_NAME_TO_ID: dict[str, int] = {c.name: c.coco_id for c in COCO80_CATEGORIES}
COCO_ID_TO_SUPERCATEGORY: dict[int, str] = {c.coco_id: c.supercategory for c in COCO80_CATEGORIES}


def build_class_ids_for_fold(*, fold: int, split: str, nclass: int = 80, nfolds: int = 4) -> list[int]:
    """
    Mirror `evaluation_util/data/coco.py:DatasetCOCO.build_class_ids`.

    Returns internal contiguous class IDs (0..79).
    - fold == -1: returns all classes (ICL mode).
    - split in {"val","test"} -> val fold classes (20)
    - otherwise -> train classes (60)
    """
    if fold == -1:
        return list(range(nclass))

    split_norm = (split or "").strip().lower()
    is_val = split_norm in {"val", "test", "val2014"}

    nclass_trn = nclass // nfolds
    class_ids_val = [fold + nfolds * v for v in range(nclass_trn)]
    class_ids_trn = [x for x in range(nclass) if x not in class_ids_val]
    return class_ids_val if is_val else class_ids_trn


def cat_idx_to_internal_class_id(*, cat_idx: int, fold: int, split: str = "val") -> int | None:
    """
    Map eval-log `cat_idx` (episode-local) to internal class id in [0..79].

    For COCO folds, eval typically targets the fold's val classes (20 classes), so cat_idx is 0..19.
    """
    try:
        cat_idx_i = int(cat_idx)
    except Exception:
        return None
    if cat_idx_i < 0:
        return None

    class_ids = build_class_ids_for_fold(fold=fold, split=split)
    if cat_idx_i >= len(class_ids):
        return None
    return int(class_ids[cat_idx_i])


def internal_class_id_to_coco_id(internal_id: int) -> int | None:
    try:
        ii = int(internal_id)
    except Exception:
        return None
    if not (0 <= ii < len(COCO_INTERNAL_ID_TO_COCO_ID)):
        return None
    return int(COCO_INTERNAL_ID_TO_COCO_ID[ii])


def internal_class_id_to_name(internal_id: int) -> str | None:
    try:
        ii = int(internal_id)
    except Exception:
        return None
    if not (0 <= ii < len(COCO_INTERNAL_ID_TO_NAME)):
        return None
    return COCO_INTERNAL_ID_TO_NAME[ii]


def internal_class_id_to_supercategory(internal_id: int) -> str | None:
    try:
        ii = int(internal_id)
    except Exception:
        return None
    if not (0 <= ii < len(COCO_INTERNAL_ID_TO_SUPERCATEGORY)):
        return None
    return COCO_INTERNAL_ID_TO_SUPERCATEGORY[ii]


# Optional semantic helpers beyond COCO supercategory.
_SMALL_OBJECT_NAMES: set[str] = {
    "tie",
    "toothbrush",
    "scissors",
    "fork",
    "knife",
    "spoon",
    "remote",
    "mouse",
    "cell phone",
    "sports ball",
    "baseball bat",
    "baseball glove",
    "tennis racket",
    "kite",
    "stop sign",
}


def is_small_object_class_name(name: str | None) -> bool:
    n = (name or "").strip().lower()
    return n in _SMALL_OBJECT_NAMES
