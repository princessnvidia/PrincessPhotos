#!/usr/bin/env bash
set -e
cd "/home/vio/Applications/PrincessPhotos"
source "/home/vio/Applications/PrincessPhotos/.venv/bin/activate"
exec python "/home/vio/Applications/PrincessPhotos/princess_photos.py" "$@"
