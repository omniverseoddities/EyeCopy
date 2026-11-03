#!/usr/bin/env bash
# Build the bundled media executable from the supplied, unmodified sources.
set -euo pipefail
: "${MEDIA_WORK:?Set MEDIA_WORK to the source/build directory}"
: "${MEDIA_PREFIX:?Set MEDIA_PREFIX to an empty installation directory}"
jobs="${MEDIA_JOBS:-8}"
script_dir="$(cd -- "$(dirname -- "$0")" && pwd)"
work="$(cygpath -u "$MEDIA_WORK")"
prefix="$(cygpath -m "$MEDIA_PREFIX")"
export PKG_CONFIG_PATH="$prefix/lib/pkgconfig"
export SOURCE_DATE_EPOCH=1727395200
common_cflags="-O3 -ffile-prefix-map=$prefix=/usr/local -ffile-prefix-map=$(cygpath -m "$work")=/usr/src/eyecopy-media"

if [[ "${MEDIA_RESUME:-0}" != 1 ]]; then
cd "$work/x264"
bash ./configure --host=x86_64-w64-mingw32 --cross-prefix=x86_64-w64-mingw32- \
  --prefix="$prefix" --enable-static --disable-cli --disable-opencl \
  --extra-cflags="$common_cflags" --extra-ldflags="-static"
make -j"$jobs"
make install

cd "$work/nv-codec-headers"
make PREFIX="$prefix" install

cd "$work/ffmpeg"
bash ./configure --prefix="$prefix" --target-os=mingw32 --arch=x86_64 \
  --cc=gcc --ar=ar --ranlib=ranlib --nm=nm \
  --disable-autodetect --enable-gpl --enable-version3 --enable-libx264 \
  --enable-ffnvcodec --enable-nvenc --enable-nvdec --enable-cuvid \
  --enable-static --disable-shared --disable-ffplay --disable-ffprobe \
  --disable-doc --disable-debug --disable-postproc \
  --disable-devices --enable-indev=lavfi \
  --extra-cflags="$common_cflags" --extra-ldflags="-static" --pkg-config-flags=--static
fi
cd "$work/ffmpeg"
"$(cygpath -u "$MEDIA_PYTHON")" "$(cygpath -m "$script_dir/prepare-generated.py")"
make -j"$jobs" V=1
make V=1 install
