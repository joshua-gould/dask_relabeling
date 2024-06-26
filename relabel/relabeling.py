import datetime
import os
import pathlib
import shutil
import zipfile
from typing import List, Union, Callable

import dask.array as da
import numpy as np

from . import chunkops


def segment_overlapped_input(img: da.Array, seg_fn: Callable,
                             spatial_dims: int = 2,
                             returns_classes: bool = False,
                             segmentation_fn_kwargs: Union[dict, None] = None,
                             ) -> da.Array:
    if segmentation_fn_kwargs is None:
        segmentation_fn_kwargs = {}

    if returns_classes:
        labeled_chunks = [(2,)]
    else:
        labeled_chunks = []

    labeled_chunks += list(img.chunks)[-spatial_dims:]
    arrs = [img]
    _segmentation_fn_kwargs = segmentation_fn_kwargs
    if segmentation_fn_kwargs is not None:
        _segmentation_fn_kwargs = dict()
        for key in segmentation_fn_kwargs.keys():
            if isinstance(segmentation_fn_kwargs[key], da.Array):
                arrs.append(segmentation_fn_kwargs[key])
            else:
                _segmentation_fn_kwargs[key] = segmentation_fn_kwargs[key]
    labeled = da.map_blocks(
        seg_fn,
        *arrs,
        **_segmentation_fn_kwargs,
        chunks=tuple(labeled_chunks),
        drop_axis=tuple(range(img.ndim - spatial_dims)),
        dtype=np.int32,
        meta=np.empty((0, 0), dtype=np.int32)
    )

    return labeled


def remove_overlapped_labels(labels: da.Array, overlaps: List[int],
                             threshold: float = 0.5,
                             spatial_dims: int = 2,
                             ) -> da.Array:
    classes = None
    if labels.ndim > spatial_dims:
        labels_chunks = labels.chunks
        classes = labels[1:]
        labels = labels[0]

    removed = da.map_blocks(
        chunkops.remove_overlapped_objects,
        labels,
        overlaps=overlaps,
        threshold=threshold,
        spatial_dims=spatial_dims,
        dtype=np.int32,
        meta=np.empty((0,), dtype=np.int64)
    )

    if classes is not None:
        classes = da.where(removed, classes, 0)

        removed = da.concatenate((removed[None, ...], classes), axis=0)
        removed = removed.rechunk(labels_chunks)

    return removed


def merge_overlapped_tiles(labels: da.Array, overlaps: List[int],
                           spatial_dims: int = 2) -> da.Array:
    merged_depth = tuple([0] * (labels.ndim - spatial_dims)
                         + [(overlap, overlap) for overlap in overlaps])

    # Merge the overlapped objects from adjacent chunks for all chunk tiles.
    merged = da.map_overlap(
        chunkops.merge_tiles,
        labels,
        overlaps=overlaps,
        spatial_dims=spatial_dims,
        depth=merged_depth,
        boundary=None,
        trim=False,
        dtype=np.int64,
        meta=np.empty((0, 0), dtype=np.int64)
    )

    merged = da.overlap.trim_overlap(merged, merged_depth, boundary=None)

    return merged


def annotate_labeled_tiles(labels: da.Array, overlaps: List[int],
                           object_classes: Union[dict, None] = None,
                           spatial_dims: int = 2) -> Union[da.Array,
                                                           pathlib.Path]:
    if object_classes is None:
        object_classes = {
            0: "cell"
        }

    labels_annotations = da.map_blocks(
        chunkops.annotate_object_fetures,
        labels,
        overlaps=overlaps,
        object_classes=object_classes,
        spatial_dims=spatial_dims,
        drop_axis=tuple(range(labels.ndim - spatial_dims)),
        chunks=(1, 1),
        dtype=object,
        meta=np.empty((0, 0), dtype=object)
    )

    return labels_annotations


def zip_annotated_labeled_tiles(labels: da.Array,
                                out_dir: Union[str, pathlib.Path, None] = None
                                ) -> pathlib.Path:
    if out_dir is None:
        out_dir = "./annotations_output-"
        out_dir += datetime.datetime.now().strftime("%Y%m%d-%H%M%S")

    if isinstance(out_dir, str):
        out_dir = pathlib.Path(out_dir)

    safe_to_remove = False
    if not os.path.isdir(out_dir):
        os.makedirs(out_dir, exist_ok=True)
        safe_to_remove = True

    geojson_filenames = da.map_blocks(
        chunkops.dump_annotaions,
        labels,
        out_dir=out_dir,
        chunks=(1, 1),
        dtype=object,
        meta=np.empty((0, 0), dtype=object)
    )

    geojson_filenames = geojson_filenames.compute()

    out_zip_filename = pathlib.Path(str(out_dir) + ".zip")
    with zipfile.ZipFile(out_zip_filename, "w", zipfile.ZIP_DEFLATED,
                         compresslevel=9) as out_zip:
        for chunk_filename in geojson_filenames.flatten().tolist():
            if chunk_filename:
                out_zip.write(chunk_filename,
                              arcname=chunk_filename.relative_to(out_dir))

    if safe_to_remove and os.path.isdir(out_dir):
        shutil.rmtree(out_dir)

    return out_zip_filename


def prepare_input(img: da.Array, overlaps: List[int], spatial_dims: int = 2
                  ) -> da.Array:
    # Prepare input for overlap.
    padding = [(0, 0)] * (img.ndim - spatial_dims)
    padding += [(0, (cs - dim) % cs)
                for dim, cs in zip(img.shape[-spatial_dims:],
                                   img.chunksize[-spatial_dims:])]

    if any(map(any, padding)):
        img_padded = da.pad(
            img,
            padding
        )

        img_rechunked = da.rechunk(img_padded, img.chunksize)

    else:
        img_rechunked = img

    img_overlapped = da.overlap.overlap(
        img_rechunked,
        depth=tuple([(0, 0)] * (img.ndim - spatial_dims)
                    + [(overlap, overlap) for overlap in overlaps]),
        boundary=None,
    )

    return img_overlapped


def image2labels(img: da.Array, seg_fn: Callable,
                 overlaps: Union[int, List[int]] = 50,
                 threshold: float = 0.05,
                 spatial_dims: int = 2,
                 returns_classes: bool = False,
                 segmentation_fn_kwargs: Union[dict, None] = None) -> da.Array:
    if isinstance(overlaps, int):
        overlaps = [overlaps] * spatial_dims

    img_overlapped = prepare_input(img, overlaps=overlaps,
                                   spatial_dims=spatial_dims)
    if segmentation_fn_kwargs is not None:
        for key in segmentation_fn_kwargs:
            if isinstance(segmentation_fn_kwargs[key], da.Array):
                segmentation_fn_kwargs[key] = prepare_input(
                    segmentation_fn_kwargs[key],
                    overlaps=overlaps,
                    spatial_dims=spatial_dims
                )

    labels = segment_overlapped_input(
        img_overlapped,
        seg_fn=seg_fn,
        spatial_dims=spatial_dims,
        returns_classes=returns_classes,
        segmentation_fn_kwargs=segmentation_fn_kwargs
    )

    labels = remove_overlapped_labels(
        labels,
        overlaps=overlaps,
        threshold=threshold,
        spatial_dims=spatial_dims
    )

    labels = merge_overlapped_tiles(
        labels,
        overlaps=overlaps,
        spatial_dims=spatial_dims
    )

    # Remove the pad added with prepare_input
    labels = labels[
        tuple([slice(None)] * (labels.ndim - spatial_dims)
              + [slice(0, s) for s in img.shape[-spatial_dims:]])
    ]

    return labels


def labels2geojson(labels: da.Array, overlaps: Union[int, List[int]] = 50,
                   threshold: float = 0.5,
                   spatial_dims: int = 2,
                   object_classes: Union[dict, None] = None,
                   pre_overlapped: bool = False) -> None:
    if isinstance(overlaps, int):
        overlaps = [overlaps] * spatial_dims

    if not pre_overlapped:
        labels = prepare_input(labels, overlaps=overlaps,
                               spatial_dims=spatial_dims)

    labels = remove_overlapped_labels(
        labels,
        overlaps=overlaps,
        threshold=threshold,
        spatial_dims=spatial_dims
    )

    if object_classes is None:
        classes_ids = range(labels.shape[:(labels.ndim - spatial_dims)][0]
                            if labels.ndim - spatial_dims > 0 else 1)
        object_classes = {class_id: "cell" for class_id in classes_ids}

    labels = annotate_labeled_tiles(
        labels,
        overlaps=overlaps,
        object_classes=object_classes,
        spatial_dims=spatial_dims
    )

    return labels


def image2geojson(img: da.Array, seg_fn: Callable,
                  overlaps: Union[int, List[int]] = 50,
                  threshold: float = 0.5,
                  spatial_dims: int = 2,
                  returns_classes: bool = False,
                  object_classes: Union[dict, None] = None,
                  segmentation_fn_kwargs: Union[dict, None] = None) -> None:
    if isinstance(overlaps, int):
        overlaps = [overlaps] * spatial_dims

    img_overlapped = prepare_input(img, overlaps=overlaps,
                                   spatial_dims=spatial_dims)

    labels = segment_overlapped_input(
        img_overlapped,
        seg_fn=seg_fn,
        spatial_dims=spatial_dims,
        returns_classes=returns_classes,
        segmentation_fn_kwargs=segmentation_fn_kwargs
    )

    labels = labels2geojson(
        labels,
        overlaps,
        threshold=threshold,
        spatial_dims=spatial_dims,
        object_classes=object_classes,
        pre_overlapped=True
    )

    return labels


def sort_label_indices(labels: da.Array, spatial_dims: int = 2) -> da.Array:
    """Sort the indices of all labeled objects in the dask array to be an
    uninterrupted sequence atarting at 1.

    This triggers the compute over the whole image and therefore should be used
    only when an uninterrupted sequence of label indices is exctiictly
    necessary.

    It is recommended to pre-compute the labels before sorting the indices,
    either by using labels.persist(), or saving them into a temporary file
    (e.g. a zarr array) and reopening them again as a dask array.
    """
    classes = None
    if labels.ndim > spatial_dims:
        labels_chunks = labels.chunks

        classes = labels[1:]
        labels = labels[0]

    unique_labels = da.unique(labels).compute().tolist()

    sorted_labels = da.map_blocks(
        chunkops.sort_indices,
        labels,
        unique_labels=unique_labels,
        dtype=labels.dtype,
        meta=np.empty((0,), dtype=labels.dtype)
    )

    if classes is not None:
        sorted_labels = da.concatenate((sorted_labels[None, ...],
                                        classes), axis=0)
        sorted_labels = sorted_labels.rechunk(labels_chunks)

    return sorted_labels
