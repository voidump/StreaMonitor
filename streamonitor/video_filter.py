import os

import parameters
import streamonitor.log as log


_logger = log.Logger("video_filter").get_logger()


def remove_undersized_videos(paths, logger=None):
    # Delete video files smaller than the min-video-size threshold. `paths` are
    # exact final files: either just created by a finished recording / temp
    # repair, or library files pre-screened by the media index scanner (which
    # excludes in-progress recordings and .tmp. files). Returns the list of
    # removed paths. Never raises; a threshold of 0 disables the filter. The
    # threshold is read via the getter so runtime settings changes apply.
    logger = logger or _logger
    min_mb = parameters.get_min_video_size_mb()
    if not min_mb or min_mb <= 0:
        return []

    threshold = int(min_mb * 1024 * 1024)
    removed = []
    for path in paths:
        try:
            if not path or not os.path.exists(path):
                continue
            size = os.path.getsize(path)
        except OSError:
            continue
        if size < threshold:
            name = os.path.basename(path)
            try:
                os.remove(path)
            except OSError as e:
                logger.warning(f'Failed to remove undersized video {name}: {e}')
                continue
            logger.info(
                f'Removed undersized video ({size / 1024 / 1024:.2f} MB < '
                f'{min_mb} MB): {name}')
            removed.append(path)
    return removed
