#!/usr/bin/env bash
# Host unit tests for the pure policy headers (no ESP-IDF needed):
#   jarvis_duplex_policy.h     full-duplex live session
#   jarvis_live_wake_policy.h  live from the wake word / the tap (fw 5.3)
# Each test is built as C and as C++ (main.cpp includes the headers as C++).
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
OUT="$HERE/build"
INC="$ROOT/components/jarvis_ws_audio/include"
mkdir -p "$OUT"

for t in test_duplex_policy test_live_wake_policy; do
    gcc -std=c99 -Wall -Wextra -Werror -I"$INC" "$HERE/$t.c" -o "$OUT/${t}_c"
    g++ -x c++ -std=c++17 -Wall -Wextra -Werror -I"$INC" "$HERE/$t.c" -o "$OUT/${t}_cpp"
done

for t in test_duplex_policy test_live_wake_policy; do
    echo "== $t (C) =="
    "$OUT/${t}_c"
    echo "== $t (C++) =="
    "$OUT/${t}_cpp"
done
